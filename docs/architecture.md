# 架构

[文档目录](README.md) · [贡献指南](../CONTRIBUTING.md)

一个实例管理一个来源、一个缓存目录和一个目的地。原生 CLI 与 Docker 共用核心，只负责不同的进程管理方式。用户行为以[传输规则](operations.md#传输与清理规则)为准。

## 两条并行队列

![并行队列：完整缓存交接文件，SQLite 保存任务，JSON 控制两路传输](assets/architecture.svg)

实线为文件传输，虚线为记录与控制。两路各一次处理一个文件，以完整缓存和持久化任务交接。SMB 离线不阻止额度内导入，来源断开不阻止完整缓存上传。

## 一个文件的完成边界

![文件完成边界：完整导入后可按策略清理原片，远端归档记录后再清理缓存](assets/file-lifecycle.svg)

原片清理与上传可并行；清理源文件期间保护该组缓存。归档改名结果未知时进入发布恢复，不按半文件清理。中断的传输从头开始，不重复处理已经完成的文件。

导入 SHA-256 与上传顺序读取的校验值比较；旧缓存没有该值时仍能处理。卷、大小、时间和内容抽样另作缓存身份依据。远端视频不完整读回，同一文件的 SMB 操作复用连接。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| [runtime](../media_importer/runtime.py) / [pipeline](../media_importer/pipeline.py) | 后台生命周期、两路调度与故障恢复 |
| [service](../media_importer/service.py) | 导入、归档与原片清理条件 |
| [contracts](../media_importer/contracts.py) / [adapters](../media_importer/adapters/) | 通用接口与 GoPro、SMB、飞书差异 |
| [source_connection](../media_importer/source_connection.py) | 身份观测、本次确认与旧卡失效 |
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

两路停止后，`settle` 在同一事务中冻结待发结果、保留去重与归档依据，再删除已完成明细。有完整缓存、待清理临时文件、清理错误或删除结果未知时不收尾。通知失败独立重试，不重新上传。

同一份数据仅一个传输实例。初始凭据在 `.env`，飞书更新的 SMB 密码存入本机配置目录的 `credentials/`；JSON 和控制队列只保存随机引用，连接检查通过后才原子切换。更新快照、备份与迁移包含凭据文件。素材身份决定去重，归档禁止覆盖，源清理、归档和缓存清理分别记录，不能相互替代。
