# 配置参考

[文档目录](README.md) · [飞书](feishu.md) · [迁移与恢复](recovery.md)

## 文件与目录

| 位置 | 内容 | 迁移时 |
| --- | --- | --- |
| 项目 `.env` | 初始连接、凭据、三个数据目录 | 保留并调整路径 |
| 配置目录 / `settings.json` | 同步、原片处理、传输与通知偏好、飞书保存的归档位置 | 保留 |
| 任务目录 | SQLite、部署登记、恢复快照 | 停止后台后整体复制 |
| 缓存目录 | 完整文件、临时文件、卷标识、`.receipts/` | 有未完成任务时完整保留 |

实际位置见原生 `media-importer status` 或项目 `.env`。相对路径以项目目录为基准；配置、任务和缓存使用专用目录，不能直接指定整块磁盘或用户主目录。目录变更属于[迁移](recovery.md)，不要只改路径。

## 连接与凭据

从 [`.env.example`](../.env.example) 复制为 `.env`。单引号保留空格、`$`、`#` 等字面字符；程序直接读取，不用 Shell `source`。

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
| `SOURCE_TYPE` | `gopro` | 当前唯一来源适配器 |
| `SOURCE_URL` | 空 | 优先探测地址，含 `http://` 和 `:8080` |
| `SOURCE_SERIAL` | 空 | 可选序列号提示 |
| `SOURCE_TIMEOUT_SECONDS` | `20` | 请求超时，秒 |
| `SOURCE_REQUEST_ATTEMPTS` | `3` | 请求尝试次数 |

地址和序列号可留空自动发现，不能代替飞书中的设备确认。机型能力及 USB 地址规则见[兼容性](compatibility.md#gopro)。

### SMB

| 变量 | 示例 | 说明 |
| --- | --- | --- |
| `DESTINATION_TYPE` | `smb` | 当前唯一目的地适配器 |
| `DESTINATION_NAME` | `SMB 存储` | 消息显示名称 |
| `DESTINATION_HOST` | `storage.example.com` | IP 或域名，不带协议、端口或路径 |
| `DESTINATION_PORT` | `445` | 端口 |
| `DESTINATION_SHARE` | `media` | 共享名，必填 |
| `DESTINATION_DIRECTORY` | `imports` | 共享内目录；显式留空使用共享根目录 |
| `DESTINATION_USERNAME` | `backup` | 读写账号 |
| `DESTINATION_PASSWORD` | `REPLACE_WITH_SMB_PASSWORD` | 密码，必填 |
| `DESTINATION_DOMAIN` | `WORKGROUP` | 按服务器认证要求填写 |

上例对应 `smb://storage.example.com/media/imports`。共享与子目录需提前存在；服务器内部的 `/home/user/...` 不是 SMB 共享名。归档布局示例：

```text
imports/
└── HERO9-EXAMPLE123456/
    ├── GH010001.MP4
    ├── GH010002.MP4
    └── GH010001_1.MP4
```

设备目录采用实际型号和序列号。重名加后缀，不覆盖、不按日期分层；去重与目的地身份规则见[使用说明](operations.md#传输与清理规则)。

在飞书「设置 → 归档位置」保存后，JSON 中会新增 `archive`。其中 `host`、`port`、`share`、`directory` 优先于 `.env` 的同名连接参数，账号、密码与域仍取自 `.env`。程序生成的 `id` 和 `change_id` 用于保持存储身份与恢复保存结果，迁移时一起保留，不手工重置。未使用该功能的现有配置保持不变。

### 飞书

| 变量 | 示例 |
| --- | --- |
| `FEISHU_APP_ID` | `cli_EXAMPLE00000000` |
| `FEISHU_APP_SECRET` | `REPLACE_WITH_APP_SECRET` |
| `FEISHU_OWNER_OPEN_ID` | `ou_EXAMPLE00000000` |

Open ID 必须属于本人在**当前应用**中的身份，不是群 ID 或其他应用的 ID。程序只接收此人的单聊和卡片操作。[获取步骤 →](feishu.md#1-准备应用与凭据)

## 日常偏好

完整文件见 [`settings.example.json`](../config/settings.example.json)。`version` 为 `4`；JSON 不支持注释，未知键、重复键或无效类型会报错。

### 同步与素材

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `backup.enabled` | `false` | 同步总开关 |
| `backup.delete_source_after_import` | `false` | 整组完整导入后删除原片，建议通过飞书确认开启 |
| `backup.media_scope` | `"all"` | `all` 全部 / `video` 视频 / `photo` 照片与 RAW |
| `backup.archive_previews` | `false` | 归档确认关联的 LRV、THM |
| `backup.source_transport` | `"usb"` | 当前固定为 USB |
| `backup.stop_after_current` | `false` | 两路当前文件结束后停止，不开始新文件 |
| `backup.poll_seconds` | `10` | 轮询间隔，秒 |
| `backup.retry_seconds` | `30` | 重试基础间隔，秒 |
| `backup.stability_seconds` | `2` | 清单稳定性复查间隔，秒 |

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
| `notifications.connection_events` | `true` | 连接断开提醒 |
| `notifications.progress_interval_seconds` | `300` | 进度更新间隔，60–86400 整数秒 |
| `notifications.keyword` | `""` | 消息前缀，最多 100 字符 |

关闭自动通知不影响手动查询、控制回执与设备确认。所有消息时间使用北京时间。

## 修改后生效

| 修改 | 原生 CLI | Docker |
| --- | --- | --- |
| JSON 偏好 | 自动重读；避免同时从飞书改同一设置 | 同左 |
| 飞书保存的归档位置 | 停止同步、连接检查、确认保存后自动应用 | 同左，无需修改只读 `.env` 挂载 |
| `.env` 连接与凭据 | `media-importer stop` 后 `media-importer start` | `docker compose up -d --force-recreate --no-build` |
| 三个数据目录 | 停止、迁移、重新登记 | 停止、迁移、修改挂载并重建 |

保存不代表连接成功，修改后用 `check` 或飞书状态核对。真实配置、记录、缓存与备份不提交到 Git；自定义仓库内目录需另加忽略规则。
