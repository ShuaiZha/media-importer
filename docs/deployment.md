# 部署与配置

[返回首页](../README.md) · [飞书配置](feishu.md) · [使用与维护](operations.md)

首次安装按本文完成相机、缓存和 SMB 配置，再按飞书指南接通机器人。已有实例更新请看[日常升级](operations.md#日常升级)，不要覆盖本机配置。

[准备条件](#开始之前) · [首次部署](#首次部署) · [配置与数据](#配置与数据)

## 开始之前

当前实现是 **单 GoPro、单 SMB 目的地、飞书本人单聊控制**。已实测链路为 HERO9 → USB 网络 HTTP → Mac Docker Desktop → SMB（可经宿主机 Tailscale）。Windows/Linux 的 USB 驱动与容器网络需实机验证，镜像支持该架构不代表设备链路已通过验收。

准备以下条件：

- Git，以及正在运行的 Docker。Mac/Windows 可安装 [Docker Desktop](https://docs.docker.com/compose/install/)，Linux 使用 Docker Engine 和 Compose 插件。本文使用 `docker compose` 命令。
- 相机开机，使用支持数据传输的 USB 线接到运行 Docker 的电脑，系统已识别 USB 网络接口。仅能充电或显示 MTP 文件传输并不代表 HTTP 接口可访问；本程序不使用 USB 设备直通，也不会自动开机。
- 一个已启用 SMB 的服务器，提前建好共享及目标子目录，账号有列目录、读、写、创建、改名和删除临时文件的权限。
- 可创建并发布企业自建应用的飞书账号。需要 App ID、App Secret 和当前应用中的本人 Open ID，详见 [飞书配置指南](feishu.md)。不需要公网回调地址。
- 足够的缓存空间，电脑保持唤醒；容器能访问相机、SMB 和飞书。异地目的地可先用宿主机 Tailscale/VPN 接通，随后仍需确认容器连通性。

SMB、飞书示例地址和凭据需要换成自己的值；相机地址和序列号可留空自动发现。`192.0.2.10` 与 `storage.example.com` 是文档示例，不是可以直接连接的服务。

## 相机兼容性

| 型号与固件 | 导入与归档 | 原片删除 |
| --- | --- | --- |
| HERO9 Black，`HD9.01.01.72.00` | Mac 上已实机验证视频、分段视频及附属文件 | MP4 与关联 LRV 已实测；照片、连拍仍待专项验收 |
| HERO9 其他固件；HERO10/11/12/13 Black、HERO11 Black Mini | 使用通用 GoPro HTTP 接口，允许确认后用小文件验证；尚未实机验收 | 当前强制保留原片，不改变用户保存的删除开关 |
| 其他型号或无法识别型号 | 只显示识别信息，暂不开放导入 | 不开放 |

相机名称和归档目录取实际设备信息。HERO9 的分段组合、首段删除后的 MP4 元数据补读保留为机型差异；不直接套用到其他型号。官方接口支持不代表本项目已实测。

每次新连接显示型号、序列号、固件和兼容状态，用户点击“确认并备份”后生效；暂不备份时不读取素材，可从状态卡重新确认。连续至少三次访问失败且持续不少于 20 秒后判定连接断开，短暂抖动不重复弹卡；超过 45 秒未取得身份观测则暂停相机操作。这里的连接是网络可访问性，不能精确捕捉每一次物理拔插。服务重启后也需要重新确认。

同一实例只使用一个活动相机。上一台仍有传输或清理事项时阻止切换；先处理原任务，已归档文件不迁移、不重新上传。待验证型号即使保存的删除开关开启，本次仍保留原片。

## 首次部署

推荐先准备飞书 App ID、App Secret 与本人 Open ID，克隆项目后运行 `python3 scripts/setup.py`（Python 3.9+；Windows 可用 `py -3 scripts/setup.py`）。向导可选填写相机地址提示、检查缓存目录写入，生成 `.env` 和 JSON，随后构建镜像并从容器检查相机、SMB 与飞书认证。密码不回显，已有配置或任务记录会拒绝覆盖。

向导不会启动后台服务、发送消息或开启同步。通过后执行 `docker compose up -d --no-build`，再完成飞书的事件订阅与发布。`--no-check` 只生成配置，之后仍需按第 3、4 步构建和验证。相机地址与序列号可以留空；外接磁盘未挂载时不会创建替代目录。

以下为无需向导的手动配置方式。

### 1. 克隆并创建本机文件

以下命令在 **Mac/Linux** 执行，仅用于全新安装；已有实例不要覆盖 `.env` 或 `settings.json`。

```sh
git clone https://github.com/ShuaiZha/media-importer.git
cd media-importer
cp .env.example .env
mkdir -p config data/cache
cp config/settings.example.json config/settings.json
chmod 600 .env config/settings.json
```

Windows PowerShell 对应初始化命令：

```powershell
git clone https://github.com/ShuaiZha/media-importer.git
Set-Location media-importer
Copy-Item .env.example .env
New-Item -ItemType Directory -Force config, data/cache
Copy-Item config/settings.example.json config/settings.json
```

后面的 Docker 命令在上述系统中相同，Windows 使用 Linux 容器。

### 2. 编辑 `.env`，确认三类连接与缓存位置

相机和 SMB 配置见下表。飞书先完成 [创建应用、权限和获取凭据](feishu.md#1-创建应用并获取凭据)，再继续本节；长连接订阅等容器启动后配置。

| 配置 | 示例 | 填写方式 |
| --- | --- | --- |
| `SOURCE_TYPE` | `gopro` | 当前只实现 GoPro |
| `SOURCE_URL` | 留空 | 可选优先探测地址；相机 HTTP 根地址，包含协议和端口 |
| `SOURCE_SERIAL` | 留空 | 可选地址推导提示；实际身份须在飞书确认，不是永久白名单 |
| `SOURCE_TIMEOUT_SECONDS` | `20` | 相机请求超时，单位秒 |
| `DESTINATION_TYPE` | `smb` | 当前只实现 SMB |
| `DESTINATION_NAME` | `SMB 存储` | 显示名称 |
| `DESTINATION_HOST` / `DESTINATION_PORT` | `storage.example.com` / `445` | IP 或域名；地址中不加 `smb://`、端口或路径 |
| `DESTINATION_SHARE` | `media` | SMB 共享名，不是服务器内部的 `/home/...` 路径 |
| `DESTINATION_DIRECTORY` | `imports` | 共享内相对目录，必须已存在；留空表示共享根目录 |
| `DESTINATION_USERNAME` / `DESTINATION_PASSWORD` | `backup` / `REPLACE_WITH_SMB_PASSWORD` | 有目标目录权限的账号与密码 |
| `DESTINATION_DOMAIN` | `WORKGROUP` | 按服务器认证要求填写 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | `cli_EXAMPLE00000000` / `REPLACE_WITH_APP_SECRET` | 自建应用的凭据，不使用 Webhook 或签名密钥 |
| `FEISHU_OWNER_OPEN_ID` | `ou_EXAMPLE00000000` | 上述应用中的本人 Open ID，不能用其他应用的 ID |

例如 SMB 配置对应 `smb://storage.example.com:445/media/imports`，归档文件会放入该目录下的 `HERO9-CEXAMPLE0000001/`。程序建立设备目录，但共享和 `imports` 需先在 SMB 服务端创建。

**相机地址与序列号：** 在相机「偏好设置 → 关于 → 相机信息」或电池仓标签查看序列号。[GoPro 官方 HTTP 说明](https://gopro.github.io/OpenGoPro/http/)规定 USB 地址为 `172.2X.1YZ.51:8080`，其中 `XYZ` 为序列号最后三位；例如末三位 `789` 对应 `http://172.27.189.51:8080`。这是相机端地址，不是电脑 USB 网卡的 IP。程序优先探测已确认设备和配置提示；不可访问时约每 60 秒探测一次官方规则中的 1,000 个 USB 地址，读到身份后再发送确认卡。扫描耗时另计，不扫描整个局域网，也不依赖额外宿主机 App。

在浏览器打开 `http://实际相机IP:8080/gp/gpControl/info`，应能看到 `info.serial_number`、`model_name` 等信息；以实际返回值核对序列号。若没有 USB 网络接口，先检查线缆、相机 USB 模式和系统驱动；先让宿主机能读取接口，再排查 Docker 网络。

**缓存位置：** 默认使用仓库下的 `data/cache`。如要使用外接盘，在 `.env` 修改：

```dotenv
MEDIA_IMPORTER_CONFIG_HOST_DIR='./config'
MEDIA_IMPORTER_DATA_HOST_DIR='./data'
MEDIA_IMPORTER_CACHE_HOST_DIR='/Volumes/MediaSSD/media-importer/cache'
```

先确认外接盘已挂载，再创建该目录；不要在未挂载的盘名下创建同名空目录。Windows 可使用 `D:/MediaImport/cache`，Linux 可使用实际挂载点下的绝对路径。Docker Desktop 必须获准访问这些目录。修改配置或数据目录时，也要把 `settings.json` 放入所选配置目录，并提前创建数据目录。

Compose 将以上三个目录分别挂载为 `/config`、`/data`、`/data/cache`，程序不会自动创建不存在的宿主机挂载目录。首次启动会登记缓存卷身份；之后卷标识不匹配时等待原盘，不切换到内置盘。

### 3. 校验、构建并启动

示例 JSON 中 `backup.enabled`、`backup.delete_source_after_import` 和 `notifications.enabled` 默认为 `false`，连接提醒 `notifications.connection_events` 为 `true`。服务启动后检测连接，在飞书点击“确认并备份”才读取相机素材。已有完整缓存仍按同步开关上传。然后在仓库根目录执行：

```sh
docker compose config --quiet
python3 scripts/build.py
docker compose run --rm --no-deps backup validate
docker compose up -d --no-build
docker compose ps
docker compose logs --tail 100 backup bot
```

`validate` 只检查 JSON 及相机、SMB 连接字段格式，不测试账号能否登录。镜像由源码在本机构建，`local/media-importer:0.10.15` 是本地标签；仓库没有可供 `docker pull` 的预构建镜像。首次构建需要访问 Docker Hub、Alpine 软件源和 Python 包源。`scripts/build.py` 只复制构建所需源码到临时目录，避免外接盘的 macOS 元数据导致构建失败，配置和素材不会进入构建目录。Windows 可用 `py -3` 替代 `python3`。

Compose 启动 `backup`（导入、上传和通知）与 `bot`（飞书长连接控制）两个服务，不映射端口，没有网页入口。同步按 JSON 中保存的开关运行。`bot` 建立连接后，继续 [配置事件、回调、菜单和发布](feishu.md#3-配置事件与卡片回调)。若提示“应用未建立长连接”，先检查 `bot` 凭据、容器状态与联网情况，再回后台保存。

### 4. 验证部署结果

完成飞书发布后执行：

```sh
docker compose ps
docker compose exec backup python3 -m media_importer status
docker compose exec backup python3 -m media_importer check --json
```

两项服务最终应显示 `healthy`。这表示进程/长连接正常，不代表所有外部设备均在线；`check` 会分别报告相机、缓存、SMB 与飞书认证结果，等待或失败时退出码为 `2`。首次检查会登记缓存卷身份，但不会传输素材或发送测试消息。SMB 写入、改名和飞书收件人仍需实际验证。

在飞书单聊点击「状态 → 查看状态」，再点击卡片的「刷新状态」：应更新原消息，同步状态应与 JSON 一致。可通过「同步 → 停止同步」停止传输；关闭状态会持久化，普通重启不会重新开启。也可从本机主动发送一张状态卡测试发送权限和接收人：

```sh
docker compose exec bot python3 -m media_importer bot-panel
```

首次传输验收前先停止同步，再准备仅含可丢弃小素材的测试卡和独立 SMB 子目录，保持「原片处理 → 保留原片」。**开启同步会处理所选类别的全部待导入素材及已有完整缓存，没有“只选某个测试文件”的入口。** 同步内容默认为全部素材，可在 JSON 的 `backup.media_scope` 中设置为 `video` 或 `photo`。如要验证通知，先在 JSON 设置 `notifications.enabled: true`，再在「同步 → 开启同步」开始；完成后停止同步，核对 SMB 实际文件位置。

## 配置与数据

| 文件 | 内容 |
| --- | --- |
| `.env` | 相机、SMB、飞书连接与凭据，以及宿主机挂载路径 |
| `config/settings.json` | 同步、素材类别、导入后删除原片、传输限制与通知设置，版本 4 |
| `data/state.sqlite3` | 当前设备选择、连接确认、未完成任务、待清理事项与必要去重及投递信息；整轮完成后清理明细，数据库版本 5 |
| 配置的缓存目录 | 完整缓存、未完成下载、核验元数据和隐藏卷标识，迁移时作为整体保留 |

表中配置与数据路径是默认值；自定义挂载时以 `.env` 为准。`.env` 只读挂载进容器，不进入镜像或 JSON。修改后运行 `docker compose up -d --force-recreate`；**`docker compose restart` 不重新解析挂载配置**。值包含空格、`$`、`#` 时使用单引号，不用 `source .env`。不要把真实凭据发到飞书或提交仓库。

飞书只修改同步和删除原片两个开关，原子保存后由后台应用。其余偏好直接编辑 `config/settings.json`，后台自动读取；JSON 无效时停止启动新任务并报告原因，不覆盖原配置。建议先停止同步再修改，避免当前文件因配置应用而中断。

| JSON 字段 | 用途与默认值 |
| --- | --- |
| `backup.enabled` | 同步开关，默认 `false` |
| `backup.delete_source_after_import` | 完整导入后删除原片，默认 `false` |
| `backup.media_scope` | `all` / `video` / `photo`，默认 `all` |
| `backup.archive_previews` | 归档确认关联的 LRV、THM，默认 `false` |
| `limits.upload_mib_per_second` | 上传限速，默认 `0`（不限） |
| `limits.max_cache_gib` | 缓存额度，默认 `50`；`0` 表示不限 |
| `limits.min_free_disk_gib` | 磁盘预留，默认 `10` |
| `notifications.enabled` | 自动进度与结果提醒，默认 `false` |
| `notifications.connection_events` | 断开提醒，默认 `true`；相机确认卡属于必要控制流程，始终发送 |
| `notifications.progress_interval_seconds` | 进度间隔，默认 `300`，范围 60–86400 秒 |

沿用版本 4 配置，现有配置、任务和完整缓存无需重建。`backup.source_transport` 固定为 `usb`；无线相关旧 `.env` 值可留存，但不再使用。其他已有重试参数仍有效；`backup.stop_after_current` 仅保留为文件中的维护选项，不再提供机器人入口。真实配置不会被升级覆盖。原 `SOURCE_URL`、`SOURCE_SERIAL` 原样保留，改为发现提示；确认后的设备选择保存在 SQLite，不回写凭据或改名已有归档。

同步和原片开关在普通重启后保持，但相机连接需重新确认；未确认时不导入、不删除。确认前完整缓存可继续上传。归档后清理对应完整缓存是固定行为；同一数据目录和飞书应用只运行一套服务。

部署遇到问题时，查看[故障排查](operations.md#故障排查)或[飞书常见问题](feishu.md#常见问题)。
