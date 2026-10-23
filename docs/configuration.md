# 配置参考

[文档目录](README.md) · [飞书](feishu.md) · [迁移与恢复](recovery.md)

## 文件与目录

| 位置 | 内容 | 迁移时 |
| --- | --- | --- |
| 项目 `.env` | 飞书首次连接凭据、本机目录与端口 | 保留并调整路径 |
| 配置目录 / `settings.json` | 唯一业务配置：相机、SMB、预览入口与日常偏好 | 保留 |
| 配置目录 / `credentials/` | SMB 密码；JSON 只保存随机引用 | 随配置目录整体保留 |
| 任务目录 | SQLite、部署登记、恢复快照 | 停止后台后整体复制 |
| 缓存目录 | 完整文件、临时文件、卷标识、`.receipts/` | 有未完成任务时完整保留 |

实际位置见原生 `media-importer status` 或项目 `.env`。相对路径以项目目录为基准；配置、任务和缓存使用专用目录，不能直接指定整块磁盘或用户主目录。目录变更属于[迁移](recovery.md)，不要只改路径。

## 连接与凭据

推荐使用安装向导同时生成配置和凭据。手动配置参考 [`.env.example`](../.env.example) 与 [`settings.example.json`](../config/settings.example.json)。单引号保留空格、`$`、`#` 等字面字符；程序直接读取，不用 Shell `source`。

### 目录

| 变量 | 示例 |
| --- | --- |
| `MEDIA_IMPORTER_CONFIG_HOST_DIR` | `./config` |
| `MEDIA_IMPORTER_DATA_HOST_DIR` | `./data` |
| `MEDIA_IMPORTER_CACHE_HOST_DIR` | `/Volumes/MediaSSD/media-importer/cache` |

Windows 缓存示例：`D:/MediaImport/cache`。示例文件默认 `./data/cache`，向导默认 `./cache`；均可修改。Docker 挂载目录需提前创建并允许访问。

### 来源设备

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `source_options.type` | `gopro` | 当前唯一来源适配器 |
| `source_options.url` | 空 | 优先探测地址，含 `http://` 和 `:8080` |
| `source_options.serial` | 空 | 可选序列号提示 |
| `source_options.timeout_seconds` | `20` | 请求超时，秒 |
| `source_options.request_attempts` | `3` | 请求尝试次数 |

地址和序列号可留空自动发现，不能代替飞书中的设备确认。机型能力及 USB 地址规则见[兼容性](compatibility.md#gopro)。

相机参数只从 JSON 的 `source_options` 读取，连接策略只从 `backup.source_transport` 读取。飞书「相机连接」编辑同一份 JSON。修改前须停止备份，不更改既有设备归属或任务。连接策略也在此卡选择；USB 地址提示单独保留，不会被无线地址覆盖。已安装的显式 `usb` 配置升级后保持不变。

### SMB

| 变量 | 示例 | 说明 |
| --- | --- | --- |
| `archive.type` | `smb` | 当前唯一目的地适配器 |
| `archive.name` | `SMB 存储` | 消息显示名称 |
| `archive.host` | `storage.example.com` | IP 或域名，不带协议、端口或路径 |
| `archive.port` | `445` | 端口 |
| `archive.share` | `media` | 共享名，必填 |
| `archive.directory` | `imports` | 共享内目录；显式留空使用共享根目录 |
| `archive.username` | `backup` | 读写账号 |
| `archive.domain` | `WORKGROUP` | 按服务器认证要求填写 |

上例对应 `smb://storage.example.com/media/imports`。共享与子目录需提前存在；服务器内部的 `/home/user/...` 不是 SMB 共享名。归档布局示例：

```text
imports/
└── HERO9-EXAMPLE123456/
    ├── GH010001.MP4
    ├── GH010002.MP4
    └── GH010001_1.MP4
```

设备目录采用实际型号和序列号。重名加后缀，不覆盖、不按日期分层；去重与目的地身份规则见[使用说明](operations.md#传输与清理规则)。

日常通过飞书「设置 → 归档存储」修改，无需编辑文件。连接参数只从 JSON 的 `archive` 读取，不再与 `.env` 叠加。

密码留空保持原值；新密码写入本机 `credentials/<随机 ID>.json`，`archive.credential_id` 只保存引用。检查连接和权限、确认保存后才启用，失败不会替换当前配置。原密码和输入内容不回显、不进入任务与日志；提交仍会经过飞书平台。当前使用的凭据始终保留，未引用草稿超过七天会在机器人启动时清理。

`id` 和 `change_id` 由程序管理，用于保持存储身份与恢复保存结果，迁移时一起保留，不手工重置。首次安装由向导生成这些标识；升级保留旧身份。

### 飞书

| 变量 | 示例 |
| --- | --- |
| `FEISHU_APP_ID` | `cli_EXAMPLE00000000` |
| `FEISHU_APP_SECRET` | `REPLACE_WITH_APP_SECRET` |
| `FEISHU_OWNER_OPEN_ID` | `ou_EXAMPLE00000000` |

Open ID 必须属于本人在**当前应用**中的身份，不是群 ID 或其他应用的 ID。程序只接收此人的单聊和卡片操作。[获取步骤 →](feishu.md#1-准备应用与凭据)

### 实时预览（v0.11.0 Mac 原生版）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PREVIEW_LOCAL_PORT` | `18765` | 本机播放网关端口，范围 1024–65535；仅监听 `127.0.0.1` |
| `PREVIEW_RTC_PORT` | `18555` | WebRTC 视频端口（TCP/UDP），仅绑定本机或本机 Tailscale 私有地址；不可用时回退兼容播放 |
| JSON `preview.public_url` | 空 | Tailscale HTTPS 基础地址，例如 `https://media-node.example.ts.net:8443`，由飞书保存 |

两个端口在 `.env` 配置，修改后重启。观看地址在飞书「设置 → 相机与画面 → 观看入口」配置；留空仅本机观看，非空须先在本机配置 Serve；「读取本机入口」只填写表单，检查并保存后生效，无需重启。修改前先结束预览，检查不会开启画面或改动系统网络。步骤见[远程预览](native.md#远程预览tailscale)。

## 日常偏好

完整文件见 [`settings.example.json`](../config/settings.example.json)。`version` 为 `5`（v0.11.0）；JSON 不支持注释，未知键、重复键或无效类型会报错。

### 备份与素材

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `backup.enabled` | `false` | 备份总开关 |
| `backup.delete_source_after_import` | `false` | 整组完整导入后删除原片，建议通过飞书确认开启 |
| `backup.media_scope` | `"all"` | `all` 全部 / `video` 视频 / `photo` 照片与 RAW |
| `backup.archive_previews` | `false` | 归档确认关联的 LRV、THM |
| `backup.source_transport` | `"auto"` | `auto` USB 优先 / `usb` 仅 USB / `wifi` 无线；无线仅 Mac 原生版 |
| `backup.stop_after_current` | `false` | 两路当前文件结束后停止，不开始新文件 |
| `backup.poll_seconds` | `10` | 轮询间隔，秒 |
| `backup.retry_seconds` | `30` | 重试基础间隔，秒 |
| `backup.stability_seconds` | `2` | 清单稳定性复查间隔，秒 |

自动与无线连接策略需要 v0.11.0+ 的 Mac 原生版。升级保留已有显式 `usb` 设置。

扫描、重试、清单复查间隔位于飞书底部「设置 → 传输与缓存」，与传输限制一起保存；扫描和重试允许 1–3600 秒，复查间隔允许 0–60 秒。相机请求超时在「设置 → 相机连接」中允许 1–300 秒，尝试次数允许 1–5 次。文字命令 `当前文件完成后停止` 控制本次停止方式。

LRV 为低分辨率预览视频，THM 为缩略图。关闭预览归档只排除确认关联的文件，关联不明的保守保留。开关不删除已有归档；改变素材范围不清空完整缓存。

**原片删除早于远端归档，缓存可能暂时是唯一副本。** 未通过删除验证的机型仍强制保留原片。具体保护条件见[原片删除](operations.md#原片删除)。

### 传输限制

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `limits.upload_mib_per_second` | `0` | 上传限速，MiB/s；`0` 不限速 |
| `limits.min_free_disk_gib` | `10` | 导入后需保留的磁盘空间，GiB |
| `limits.max_cache_gib` | `50` | 本实例缓存额度，GiB；`0` 不限额 |

导入前同时检查额度与实际剩余空间，不足时等待；上传继续释放缓存。归档成功后清理对应缓存是固定行为。

### 通知

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `notifications.enabled` | `false` | 自动进度、完成及异常提醒 |
| `notifications.connection_events` | `false` | 连接与断开提醒 |
| `notifications.progress_interval_seconds` | `300` | 进度更新间隔，60–86400 整数秒 |
| `notifications.keyword` | `""` | 消息前缀，最多 100 字符 |

关闭自动通知不影响手动查询、控制回执与设备确认。快照时间按 UTC+8 换算，事件回执使用飞书自带时间。

## 修改后生效

| 修改 | 原生 CLI | Docker |
| --- | --- | --- |
| JSON 偏好 | 自动重读；避免同时从飞书改同一设置 | 同左 |
| 飞书保存的 SMB 连接与凭据 | 停止备份、连接检查、确认保存后自动应用 | 同左，无需修改只读 `.env` 挂载 |
| 飞书保存的相机连接 | 停止备份后保存，自动应用 | 同左 |
| 飞书保存的预览入口 | 结束预览、检查并保存，下次预览采用新入口 | 暂不支持预览 |
| `.env` 飞书凭据与本机端口 | `media-importer stop` 后 `media-importer start` | `docker compose up -d --force-recreate --no-build` |
| 三个数据目录 | 停止、迁移、重新登记 | 停止、迁移、修改挂载并重建 |

保存不代表连接成功，修改后用 `check` 或飞书状态核对。真实配置、记录、缓存与备份不提交到 Git；自定义仓库内目录需另加忽略规则。

## 从旧配置升级

v0.11.0 首次启动将 version 4 一次性迁入 version 5：保存 `settings.json.v4-backup`，读取原来实际生效的值（包括旧 JSON 覆盖值），再原子保存完整 JSON。旧 SMB 密码转存到 `credentials/`；已有凭据引用、存储身份、开关、任务和缓存保留。不重新连接目的地，也不重分配归档路径。

迁移成功后，旧 `.env` 中的 `SOURCE_*`、`DESTINATION_*` 和 `PREVIEW_PUBLIC_URL` **不再参与运行配置**，可移除；端口、目录与飞书凭据保留。配置校验命令只读，不提前迁移正在运行的旧版。回滚旧程序时同时恢复更新前的 `.env`、JSON 与凭据快照，不能直接让旧版读取 version 5。

手动首次配置时，除填写两个示例文件，还须创建 `config/credentials/<archive.credential_id>.json`，内容为 `{"password":"REPLACE_WITH_SMB_PASSWORD"}`，并限制本机访问权限。不要把密码直接写进 `settings.json`。安装向导会完成这些步骤。
