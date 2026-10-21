# 架构

[文档目录](README.md) · [贡献指南](../CONTRIBUTING.md)

一个实例管理一个来源、一个缓存目录和一个目的地。原生 CLI 与 Docker 共用核心，只负责不同的进程管理方式。用户行为以[传输规则](operations.md#传输与清理规则)为准。

## 两条并行队列

![并行队列：完整缓存交接文件，SQLite 保存任务，JSON 控制两路传输](assets/architecture.svg)

实线为文件传输，虚线为记录与控制。两路各一次处理一个文件，以完整缓存和持久化任务交接。SMB 离线不阻止额度内导入，来源断开不阻止完整缓存上传。

## 设备、连接与拍摄

稳定身份为「适配器类型 + 完整序列号」，与地址和链路无关。连接会话包含实际端点、固件与可访问性，变化时使旧卡片失效。USB 与 Wi-Fi 使用相同媒体导入核心；蓝牙仅用于身份、热点与拍摄控制，SMB 不依赖相机会话。

Mac 无线工作放入可终止的子进程。凭据只在该进程中读取；连接检查有上限，失败按既有间隔重试。拍摄请求先持久化，导入线程取消当前临时下载后执行；上传线程继续。快门不自动重发，崩溃后只查询实际状态。控制结果短期保留，不新增历史查询体系。

这不是多设备调度框架：每个实例仍只有一台相机、一个目的地。拍摄控制按适配器能力显示；其他系统与实时预览未实现时明确禁用。

## 一个文件的完成边界

![文件完成边界：完整导入后可按策略清理原片，远端归档记录后再清理缓存](assets/file-lifecycle.svg)

原片清理与上传可并行；清理源文件期间保护该组缓存。归档改名结果未知时进入发布恢复，不按半文件清理。中断的传输从头开始，不重复处理已经完成的文件。

素材去重使用适配器提供的稳定身份。HERO9 的媒体接口创建时间可能随重启漂移；扫描和传输核验统一以 MP4 文件头的创建时间为准，同时检查 GUMI、大小及文件状态。关联预览文件继承该时间，不凭文件名或大小认领已有归档，也不重建任务 ID。

导入 SHA-256 与上传顺序读取的校验值比较；旧缓存没有该值时仍能处理。卷、大小、时间和内容抽样另作缓存身份依据。远端视频不完整读回，同一文件的 SMB 操作复用连接。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| [runtime](../media_importer/runtime.py) / [pipeline](../media_importer/pipeline.py) | 后台生命周期、两路调度与故障恢复 |
| [service](../media_importer/service.py) | 导入、归档与原片清理条件 |
| [contracts](../media_importer/contracts.py) / [adapters](../media_importer/adapters/) | 通用接口与 GoPro、SMB、飞书差异 |
| [connections](../media_importer/connections.py) / [source_connection](../media_importer/source_connection.py) | 连接策略、稳定身份、设备信任与旧会话失效 |
| [source_session](../media_importer/source_session.py) | 每次来源 IO 绑定当前 USB / Wi-Fi 会话，不重建 SMB 上传线程 |
| [radio](../media_importer/radio.py) / [capture](../media_importer/capture.py) | 有超时限制的原生无线进程、拍摄意图与实际结果 |
| [camera_operations](../media_importer/camera_operations.py) / [camera_cards](../media_importer/camera_cards.py) | 一次性相机查询、清理预览与确认回执；复用来源适配器和清理核验 |
| [selected_imports](../media_importer/selected_imports.py) / [selected_transfer](../media_importer/selected_transfer.py) / [selected_cards](../media_importer/selected_cards.py) | 固定所选素材范围，复用两路传输、停止恢复与同卡进度 |
| [store](../media_importer/store.py) / [storage](../media_importer/storage.py) | 任务、锁、缓存盘与副本核验 |
| [lifecycle](../media_importer/lifecycle.py) | 完成后收尾，保留必要去重与待发结果 |
| [bot](../media_importer/bot.py) / [notifications](../media_importer/notifications.py) | 本人交互、发送队列与失败重试 |
| [native](../media_importer/native/) | 安装、更新、卸载与系统服务 |

## 持久化与收尾

| 记录 | 用途 |
| --- | --- |
| `files`、`source_groups` | 未收尾任务、素材关联和删除事实 |
| `batches`、`transfer_runs` | 本轮完成条件与通知归属 |
| `source_index` | 保留原片时去重；完整扫描确认原片不存在后移除 |
| `destination_anchor` | 连接地址变化后核验逻辑目的地 |
| `.receipts/` | 数据库损坏时重建完整缓存任务 |
| `camera_operations` | 短期清理清单、确认与删除意图；结果不确定时保留待复查依据 |
| `capture_requests` | 短期拍摄意图、幂等标识与结果；已发命令不重放 |
| `selected_imports` | 本次选择、确认版本与短期结果；未完成的选定任务保留恢复范围 |

两路停止后，`settle` 在同一事务中冻结待发结果、保留去重与归档依据，再删除已完成明细。有完整缓存、待清理临时文件、清理错误或删除结果未知时不收尾。通知失败独立重试，不重新上传。

同一份数据仅一个传输实例。初始凭据在 `.env`，飞书更新的 SMB 密码存入本机配置目录的 `credentials/`；JSON 和控制队列只保存随机引用，连接检查通过后才原子切换。更新快照、备份与迁移包含凭据文件。素材身份决定去重，归档禁止覆盖，源清理、归档和缓存清理分别记录，不能相互替代。

手动相机操作使用现有导入线程的文件边界；同步关闭时由空闲后台执行，不新增常驻线程。预览只读核验并固定清单，确认绑定本人、连接会话和配置版本，执行时再次核验。服务中断后仅复查已发出的删除结果，未执行部分需要重新确认；与自动清理共用组缓存锁。

选定导入必须在全量同步停止后执行，独立记录本次范围但共用传输核心。每个主文件独立选择，直接附属文件按归档设置处理；清理边界仍由适配器的完整素材组决定。选定任务固定保留原片，停止和继续不扩大范围、不改变全量同步开关。完成后只收尾本次文件，未选任务与缓存保持原状；升级前已确认的分组任务仍按原清单恢复。
