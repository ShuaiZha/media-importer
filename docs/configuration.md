# 配置参考

[文档目录](README.md) · [飞书配置](feishu.md) · [备份与迁移](recovery.md)

连接、凭据与目录放在 `.env`；日常偏好放在 `settings.json`；任务与必要的去重信息保存在 SQLite。原生 CLI 与 Docker 使用相同格式。

## 文件与目录

| 文件或目录 | 内容 | 是否需要迁移 |
| --- | --- | --- |
| 项目 `.env` | 连接、凭据、配置 / 任务 / 缓存路径 | 是 |
| 配置目录中的 `settings.json` | 同步、删除原片、限制、通知偏好 | 是 |
| 任务目录 | `state.sqlite3`、部署登记、恢复快照等 | 是，停止服务后整体备份 |
| 缓存目录 | 完整文件、未完成文件、卷标识与 `.receipts/` 恢复清单 | 有未完成任务时必须完整保留 |

实际路径以原生 `media-importer status` 或项目 `.env` 为准。配置、任务、缓存目录都应专用于本项目；缓存可位于任务目录内，不能直接使用整块磁盘或用户主目录作为专用目录。

[`.env.example`](../.env.example) 使用 `./config`、`./data`、`./data/cache`；首次向导的缓存默认值为 `./cache`，也可选择外接盘。相对路径以**项目目录**为基准：Docker 是 `compose.yaml` 所在目录，原生是安装时登记的项目目录。完整缓存、卷标识和恢复清单应一起移动，不要只复制视频。

## `.env`：连接与凭据

从 [`.env.example`](../.env.example) 开始，不要把真实值填回示例文件。单引号可保留空格、`$`、`#` 等字面字符；`.env` 由程序读取，不需要通过 Shell `source` 执行。

### 数据位置

| 变量 | 示例 | 用途 |
| --- | --- | --- |
| `MEDIA_IMPORTER_CONFIG_HOST_DIR` | `./config` | `settings.json` 所在目录 |
| `MEDIA_IMPORTER_DATA_HOST_DIR` | `./data` | SQLite 与运行记录 |
| `MEDIA_IMPORTER_CACHE_HOST_DIR` | `/Volumes/MediaSSD/media-importer/cache` | 本地素材缓存；Windows 可用 `D:/MediaImport/cache` |

Docker 手动部署前需创建目录并允许 Docker 访问；挂载不自动创建缺失路径。原生部署会登记目录与缓存磁盘，换盘或改路径应按[迁移步骤](recovery.md)重新登记，不能只修改 `.env` 后继续运行。

### 来源设备

`SOURCE_TYPE` 选择已实现的来源适配器。以下参数对应当前 GoPro USB 适配，不能填写其他品牌名称来直接启用尚未接入的设备。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SOURCE_TYPE` | `gopro` | 当前可用的来源适配类型 |
| `SOURCE_URL` | 空 | 可选优先探测地址，包含 `http://` 和 `:8080` |
| `SOURCE_SERIAL` | 空 | 可选序列号提示，不代替飞书本次设备确认 |
| `SOURCE_TIMEOUT_SECONDS` | `20` | 单次相机请求超时，秒 |
| `SOURCE_REQUEST_ATTEMPTS` | `3` | 请求尝试次数 |

地址与序列号可留空自动发现，实际设备始终以读取到的身份及本次确认结果为准。支持的机型和 USB 地址说明见[兼容性](compatibility.md#gopro)。

### 归档目的地

`DESTINATION_TYPE` 当前可用值为 `smb`，以下为 SMB 连接参数；目的地不限定为 NAS。

| 变量 | 示例 | 说明 |
| --- | --- | --- |
| `DESTINATION_TYPE` | `smb` | 当前可用的目的地适配类型 |
| `DESTINATION_NAME` | `SMB 存储` | 消息中的显示名称 |
| `DESTINATION_HOST` | `storage.example.com` | IP 或域名，不带 `smb://`、端口、路径 |
| `DESTINATION_PORT` | `445` | SMB 端口 |
| `DESTINATION_SHARE` | `media` | SMB 共享名，必填 |
| `DESTINATION_DIRECTORY` | `imports` | 共享内相对目录；显式留空表示共享根目录 |
| `DESTINATION_USERNAME` | `backup` | 有读写权限的账号 |
| `DESTINATION_PASSWORD` | `REPLACE_WITH_SMB_PASSWORD` | 账号密码，必填 |
| `DESTINATION_DOMAIN` | `WORKGROUP` | 按服务器的认证要求填写 |

上例对应 `smb://storage.example.com/media/imports`，共享和子目录需提前存在；`/home/user/...` 等服务器内部路径不是共享名。程序按实际设备名和序列号创建设备目录，平铺保留原文件名。以下为 GoPro 归档示例：

```text
imports/
└── HERO9-EXAMPLE123456/
    ├── GH010001.MP4
    ├── GH010002.MP4
    └── GH010001_1.MP4
```

不同素材重名时追加 `_1`、`_2`，不区分大小写，不覆盖已有文件。相同素材通过身份和记录去重，不仅按文件名或大小判断。域名/IP 变化与更换存储是两回事，见[目的地身份](operations.md#目的地身份)。

### 飞书

| 变量 | 示例 | 用途 |
| --- | --- | --- |
| `FEISHU_APP_ID` | `cli_EXAMPLE00000000` | 企业自建应用 ID |
| `FEISHU_APP_SECRET` | `REPLACE_WITH_APP_SECRET` | 应用密钥 |
| `FEISHU_OWNER_OPEN_ID` | `ou_EXAMPLE00000000` | 本人在**该应用**中的 Open ID |

只接收此人的单聊和卡片操作，通知也发送给此人。不是群机器人 Webhook，不使用群 `chat_id` 或其他应用中的 Open ID。[获取方式](feishu.md#1-创建应用与获取凭据)。

## `settings.json`：日常偏好

完整示例见 [`config/settings.example.json`](../config/settings.example.json)。JSON 不支持注释；未知键、重复键和无效类型会报错，不会静默换用另一套规则。当前 `version` 必须为 `4`。

### 同步与素材

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `backup.enabled` | `false` | 同步总开关，通常通过飞书操作 |
| `backup.delete_source_after_import` | `false` | 整组完整导入后删除相机原片；建议通过飞书确认后开启 |
| `backup.media_scope` | `"all"` | `all` 全部、`video` 视频、`photo` 照片及 RAW |
| `backup.archive_previews` | `false` | 是否归档能确认关联的 LRV、THM 预览文件 |
| `backup.source_transport` | `"usb"` | 当前固定使用 USB |
| `backup.stop_after_current` | `false` | 结束两路当前文件后停止，不开始新文件；通常保持默认 |
| `backup.poll_seconds` | `10` | 轮询间隔，秒，至少 1 |
| `backup.retry_seconds` | `30` | 重试基础间隔，秒，至少 1 |
| `backup.stability_seconds` | `2` | 素材清单稳定性复查间隔，秒 |

LRV 是低分辨率预览视频，THM 是缩略图。关闭预览归档只排除**能够确认关联**的文件；关联不明的文件保守保留。删除关联预览文件也要满足所属素材组的副本保护条件。已归档文件不因修改开关被删除。

**删除原片发生在完整导入本地后，早于远端归档。** 开启后，远端上传结束前缓存盘可能是唯一副本。待验证机型仍强制保留原片，详见[兼容性](compatibility.md#gopro)。归档成功后清理对应完整缓存是固定行为，无单独开关。

### 传输限制

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `limits.upload_mib_per_second` | `0` | 上传限速，MiB/s；`0` 不限速 |
| `limits.min_free_disk_gib` | `10` | 导入后仍需保留的缓存盘空间，GiB |
| `limits.max_cache_gib` | `50` | 本实例缓存额度，GiB；`0` 不限额 |

两路各一次处理一个文件。导入前同时检查额度与磁盘剩余空间，任一不足都会等待；上传可继续释放缓存。修改素材类别只影响相应导入范围，已有完整缓存不会被当作无用文件清空。

### 通知

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `notifications.enabled` | `false` | 自动进度、完成和异常通知 |
| `notifications.connection_events` | `true` | 连接断开提醒 |
| `notifications.progress_interval_seconds` | `300` | 进度卡更新间隔，整数秒，范围 60–86400 |
| `notifications.keyword` | `""` | 可选消息前缀，最多 100 字符 |

关闭自动通知不影响手动查询、控制回执和设备确认卡。消息时间使用北京时间，进度更新同一张卡，完成及需要处理的失败单独提醒。

## 修改后如何生效

| 修改内容 | 原生 CLI | Docker Compose |
| --- | --- | --- |
| JSON 偏好 | 自动重读；避免同时从飞书修改同一设置 | 同左 |
| `.env` 连接与凭据 | `media-importer stop`，再 `media-importer start` | `docker compose up -d --force-recreate --no-build` |
| 配置、任务或缓存路径 | 停止、迁移、重新安装登记路径 | 停止、迁移、修改挂载后重建容器 |

保存配置不代表外部连接已成功。修改后通过飞书状态或 CLI `check` 核对结果；目录迁移按[迁移指南](recovery.md)执行。

真实 `.env`、JSON、数据库、缓存、日志及备份都不应提交。自定义到仓库内的新目录需加入本机忽略规则；默认 `.gitignore` 不可能覆盖任意自定义路径。
