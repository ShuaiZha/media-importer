# 素材自动导入助手 v0.10.5

相机 → 本地完整缓存 → SMB 正式归档 → 清理对应缓存。导入和上传各处理一个文件，可以并行运行。目的地离线时继续在额度内缓存；相机拔走后仍可上传完整缓存。

日常操作在飞书应用机器人单聊中完成。没有网页、HTTP 管理服务或公网回调端口。默认关闭同步和删除原片；连接信息填写后，由用户在飞书中开启同步。

[首次部署](#首次部署) · [飞书配置](docs/feishu.md) · [日常升级](#日常升级) · [备份与迁移](#一键完整备份与迁移)

## 开始之前

当前实现是 **单 GoPro、单 SMB 目的地、飞书本人单聊控制**。已实测链路为 HERO9 → USB 网络 HTTP → Mac Docker Desktop → SMB（可经宿主机 Tailscale）。其他 GoPro 型号以及 Windows/Linux 的相机链路需自行验证，不能仅凭镜像支持该架构就认定设备兼容。

准备以下条件：

- Git，以及正在运行的 Docker。Mac/Windows 可安装 [Docker Desktop](https://docs.docker.com/compose/install/)，Linux 使用 Docker Engine 和 Compose 插件。本文使用 `docker compose` 命令。
- 相机开机，使用支持数据传输的 USB 线接到运行 Docker 的电脑，系统已识别 USB 网络接口。仅能充电或显示 MTP 文件传输并不代表 HTTP 接口可访问；本程序不使用 USB 设备直通，也不会自动开机。
- 一个已启用 SMB 的服务器，提前建好共享及目标子目录，账号有列目录、读、写、创建、改名和删除临时文件的权限。
- 可创建并发布企业自建应用的飞书账号。需要 App ID、App Secret 和当前应用中的本人 Open ID，详见 [飞书配置指南](docs/feishu.md)。不需要公网回调地址。
- 足够的缓存空间，电脑保持唤醒；容器能访问相机、SMB 和飞书。异地目的地可先用宿主机 Tailscale/VPN 接通，随后仍需确认容器连通性。

所有示例地址、序列号和凭据都需要换成自己的值。`192.0.2.10` 与 `storage.example.com` 是文档示例，不是可以直接连接的服务。

## 首次部署

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

后面的 Docker 命令在上述系统中相同，Windows 使用 Linux 容器。若仓库访问受限，需先取得仓库访问权限并通过自己的 GitHub 账号认证；不要使用他人的密钥。

### 2. 编辑 `.env`，确认三类连接与缓存位置

相机和 SMB 配置见下表。飞书先完成 [创建应用、权限和获取凭据](docs/feishu.md#1-创建应用并获取凭据)，再继续本节；长连接订阅等容器启动后配置。

| 配置 | 示例 | 填写方式 |
| --- | --- | --- |
| `SOURCE_TYPE` | `gopro` | 当前只实现 GoPro |
| `SOURCE_URL` | `http://192.0.2.10:8080` | 相机 HTTP 根地址，包含协议、端口，不加 API 路径 |
| `SOURCE_SERIAL` | `CEXAMPLE0000001` | 相机接口返回的完整序列号，只处理这台相机 |
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

**相机地址与序列号：** 在相机「偏好设置 → 关于 → 相机信息」或电池仓标签查看序列号。[GoPro 官方 HTTP 说明](https://gopro.github.io/OpenGoPro/http/)规定 USB 地址为 `172.2X.1YZ.51:8080`，其中 `XYZ` 为序列号最后三位；例如末三位 `789` 对应 `http://172.27.189.51:8080`。这是相机端地址，不是电脑 USB 网卡的 IP。相机也可通过 `_gopro-web` mDNS 服务发现，本程序仍需在 `.env` 手动填写地址。

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

确认示例 JSON 中 `backup.enabled`、`backup.delete_source_after_import` 和 `notifications.enabled` 均为 `false`，然后在仓库根目录执行：

```sh
docker compose config --quiet
docker compose build
docker compose run --rm --no-deps backup validate
docker compose up -d --no-build
docker compose ps
docker compose logs --tail 100 backup bot
```

`validate` 只检查 JSON 及相机、SMB 连接字段格式，不测试账号能否登录。镜像由源码在本机构建，`local/media-importer:0.10.5` 是本地标签；仓库没有可供 `docker pull` 的预构建镜像。首次构建需要访问 Docker Hub、Alpine 软件源和 Python 包源。

Compose 启动 `backup`（导入、上传和通知）与 `bot`（飞书长连接控制）两个服务，不映射端口，没有网页入口。此时保持同步关闭。`bot` 建立连接后，继续 [配置事件、回调、菜单和发布](docs/feishu.md#3-配置事件与卡片回调)。若提示“应用未建立长连接”，先检查 `bot` 凭据、容器状态与联网情况，再回后台保存。

### 4. 验证部署结果

完成飞书发布后执行：

```sh
docker compose ps
docker compose exec backup python3 -m media_importer status
docker compose exec backup python3 -m media_importer check --json
```

两项服务最终应显示 `healthy`。这表示进程/长连接正常，不代表所有外部设备均在线；`check` 会分别报告相机、缓存、SMB 与飞书认证结果，等待或失败时退出码为 `2`。它不会传输素材或发送测试消息。SMB 写入、改名和飞书收件人仍需实际验证。

在飞书单聊点击「状态 → 运行总览」，再点击卡片的「刷新」：应更新原消息，且同步仍关闭。也可从本机主动发送一张状态卡测试发送权限和接收人：

```sh
docker compose exec bot python3 -m media_importer bot-panel
```

首次传输验收使用仅含可丢弃小素材的测试卡和独立 SMB 子目录，保持「原片处理 → 保留原片」。**开启同步会处理绑定相机中所有待导入素材，没有“只选某个测试文件”的入口。** 如要验证通知，先在「设置 → 通知设置」开启自动通知，再在「同步 → 开启同步」开始；完成后停止同步，并在归档结果中检查实际文件位置。

## 配置与数据

| 文件 | 内容 |
| --- | --- |
| `.env` | 相机、SMB、飞书连接与凭据，以及宿主机挂载路径 |
| `config/settings.json` | 同步、导入后删除原片、传输限制与通知设置，版本 4 |
| `data/state.sqlite3` | 文件与素材组身份、任务、归档记录、待清理事项，数据库版本 5 |
| 配置的缓存目录 | 完整缓存、未完成下载、核验元数据和隐藏卷标识，迁移时作为整体保留 |

表中配置与数据路径是默认值；自定义挂载时以 `.env` 为准。`.env` 只读挂载进容器，不进入镜像或 JSON。修改后运行 `docker compose up -d --force-recreate`；**`docker compose restart` 不重新解析挂载配置**。值包含空格、`$`、`#` 时使用单引号，不用 `source .env`。不要把真实凭据发到飞书或提交仓库。

日常设置通过飞书保存到 JSON，校验后原子替换并自动应用。修改传输限制可能中断当前未完成文件，下次从头重传。默认缓存额度 50 GiB、磁盘预留 10 GiB、上传不限速；缓存额度或上传限速填 `0` 表示不限。自动通知默认关闭，开启后的进度间隔默认 300 秒。

同步设置会持久化，普通重启会沿用已保存的开关，并非每次启动都关闭。维护或升级前先停止同步。归档后清理对应完整缓存是固定行为，一次只运行一套使用同一数据目录和飞书应用的服务。

## 飞书菜单与日常操作

先按 [飞书配置指南](docs/feishu.md) 创建本人可用的应用、开通最小权限、订阅事件与回调、手工创建菜单并发布。部署容器不会自动创建飞书应用或菜单。

| 一级菜单 | 二级菜单 | 内容 |
| --- | --- | --- |
| 状态 | 运行总览 | 能否拔相机、是否需要处理、导入和归档两路进度 |
| 状态 | 设备与存储 | 相机身份与健康状态、缓存目录和磁盘空间、SMB 连接、启动自检 |
| 状态 | 归档结果 | 最近结果、实际归档路径、异常与清理结果 |
| 同步 | 开启同步 / 停止同步 | 同时控制导入与上传，停止后保留完整缓存 |
| 设置 | 传输设置 / 原片处理 / 通知设置 / 使用帮助 | 日常设置、删除确认及使用说明 |

菜单采用「可切换菜单」，默认展示菜单。所有子菜单响应动作选择「发送文字消息」，**每个叶子菜单必须使用表中完整名称**；「开启同步 / 停止同步」表示两个菜单，不能合并成一个名称。该动作需要飞书客户端 7.22 及以上，发布后可等待约 5 分钟再重新进入会话。参见 [飞书官方菜单说明](https://open.feishu.cn/document/client-docs/bot-v3/bot-customized-menu)。

三张查询卡只有「刷新」，点击后更新原消息；重新点击菜单会发送新快照。过期相机状态标明更新时间，不显示成当前正常。设置卡保留保存与必要确认；开启删除原片需要再次确认“缓存盘可能是唯一副本”。连接参数与密码只能改 `.env`。

可以切换回输入框发送上述菜单名称。兼容快捷指令：`状态` / `进度` → 运行总览，`设置` → 传输设置，`清理设置` → 原片处理，`帮助` → 使用帮助，`关闭同步` → 停止同步。旧控制卡失效时重新从菜单获取。

自动通知有四类：定时进度、导入完成可拔相机、归档完成、需要处理的失败。进度更新同一张卡片；完成和失败单独提醒。同一持续故障不重复刷屏，逐文件日志留在本机。发送失败自动重试，不影响传输；关闭自动通知仍可使用菜单和接收操作回复。

## 一致性与清理规则

导入先写本地临时文件；下载完整后核对大小和源身份，落盘、改名为完整缓存、保存记录。只有完整缓存进入上传队列。

上传使用唯一隐藏临时文件，写完核对大小与身份，以禁止覆盖方式改为正式名称，保存归档记录后清理对应缓存。正式目录为：

```text
目标子目录/设备名-序列号/原文件名
```

所有日期和 DCIM 子目录的素材在设备目录内平铺。不同素材同名或大小写冲突时添加 `_1`、`_2` 等后缀。已分配路径持久化。改名响应丢失时用操作记录和文件身份核验，不能仅凭同名同大小认领文件。不全量读回视频，不计算视频 SHA-256。

**不保留字节断点。** 停止、断网或重启后，未完成文件从零重传。已完整导入和已正式归档的文件不会重传。只删除确认归属的半文件；离线时登记待清理，恢复后处理。同步关闭时仍允许清理这些半文件，不清理完整缓存。已经完整上传、等待确认改名的文件进入归档恢复，不按半文件删除。

**开启删除原片后，一组素材完整导入缓存即可清理相机，不等 SMB 上传。此时缓存盘可能是唯一副本。** 删除前核对整组成员、源身份和本地完整文件；已经归档且缓存已清理的成员核对正式归档文件。删除期间保护对应缓存，只调用单文件删除接口并复扫。关联不明、身份变化或残留附属文件保留并报告。原片删除失败不阻塞上传，未归档完整缓存始终保留。

HERO9 固件 `HD9.01.01.72.00` 已通过短视频实机验证：MP4 与 LRV 完整导入后，删除 MP4 会联动移除对应 LRV，两份缓存随后继续归档。照片、连拍及其他固件的删除行为尚未完成实机验收；启用前应使用可删除的小文件验证。

## 本地检查与故障处理

```sh
docker compose exec backup python3 -m media_importer status
docker compose exec backup python3 -m media_importer status --json
docker compose exec backup python3 -m media_importer check --json
docker compose exec backup python3 -m media_importer sync off
docker compose exec bot python3 -m media_importer bot-panel
docker compose logs -f --tail 100
docker compose stop
```

SMB 离线只暂停上传；缓存额度或空间不足暂停新导入；缓存盘不可用时两路等待。认证、权限等问题按提示修改 `.env`，重建容器。源文件失败按文件重试，不误计为整批完成。

| 部署时的现象 | 处理方式 |
| --- | --- |
| `docker compose` 不存在 / 无法连接 Docker | 安装 Compose 插件或启动 Docker Desktop，确认 `docker compose version` 和 `docker info` 可用 |
| `bind source path does not exist` / 挂载被拒绝 | 确认 `.env` 是文件、三个宿主机目录已存在、外接盘已挂载且 Docker 有目录访问权限 |
| `pull access denied` / 找不到本地镜像 | 先运行 `docker compose build`；`local/media-importer` 不是远程镜像仓库 |
| 构建卡在下载基础镜像或依赖 | 检查 Docker 自身的网络/代理、Docker Hub 和依赖源访问，官方 Alpine 下载构建方式见下文 |
| 宿主机可访问相机或 SMB，容器不通 | 检查 Docker 网络与宿主机 VPN 路由；用容器内 `check --json` 验证，不能仅凭 Finder/资源管理器访问成功判断 |
| SMB 已认证但目录不可访问 | 核对共享名与相对目录，提前创建子目录；服务器内部路径不能用作共享名 |
| 飞书菜单不显示 / 按钮无回复 | 按 [飞书排障表](docs/feishu.md#常见问题) 分别检查发布、消息订阅、卡片回调与本人 Open ID |

## 日常升级

同一 v0.10 配置/数据库版本内更新代码，不重新复制示例文件，不运行旧版本迁移脚本：

```sh
docker compose exec backup python3 -m media_importer sync off
docker compose stop
git pull --ff-only
docker compose build
docker compose up -d --no-build
docker compose ps
```

升级前建议完成下文的完整备份。构建失败时保留现有目录与缓存，排除故障后再启动；不要清空数据库或覆盖本机配置。若发行说明要求迁移，则按对应版本步骤执行。飞书菜单存于应用后台，代码更新不会自动修改菜单；旧应用需要按新版菜单表调整并发布。

## 从 v0.9 升级

先停止两个服务并备份旧源代码、镜像、`.env`、配置、数据目录与缓存清单。完整缓存保留在原盘，不要清空。

```sh
docker compose stop
docker compose build
docker compose run --rm -T --entrypoint python3 backup /app/migrate_v010.py
docker compose up -d --no-build
```

迁移必须在 Docker 内、原缓存挂载可用时进行。脚本先保存 SQLite 完整快照、旧 JSON 与 `.env` 到 `data/pre-v0.10.0-*`，再执行一次性 v3/v4 → v4/v5 升级。保留目的地身份、任务 ID、归档路径、完整缓存与历史记录；旧半文件登记待清理，取消旧控制请求和待发通知，不补发历史完成提醒。升级后同步和删除原片关闭，其他有效设置保留。

只支持由 v0.9 迁移，不提供旧配置的运行时兼容。回滚先停止新版，再使用备份恢复对应版本的源代码、镜像、配置及数据；已清理的半文件无法恢复字节进度，旧版需重新确认并重传该文件。不要在新版已经归档或删除源素材后直接覆盖回旧数据库。

迁移电脑时复制 `.env`、配置、数据以及所有未归档完整缓存，保留缓存卷标识；修改宿主机挂载路径。新缓存凭据绑定素材、缓存卷、大小、修改时间和最多 320 KiB 内容抽样，容器重建导致的 inode 变化不会使已完成数量归零。抽样用于身份核验，不是全文件内容校验。迁移时需保留修改时间和缓存卷标识；不匹配的完整缓存保留并显示待核验，绝不覆盖重下。

## 缓存核验与结果查看

「状态 → 运行总览」说明是否可拔相机、是否需要处理和两路进度；「设备与存储」展示连接与缓存详情；「归档结果」展示最近结果和实际归档位置。“可以断开（同步已停）”只说明当前没有相机读写，不表示素材已收齐；只有本轮清单复核、所有已知文件完整缓存或归档后才显示“本轮素材已收齐”。未重新扫描显示“已记录素材”。正式路径显示实际重名后缀；没有成功归档时明确标为计划目的地。

旧缓存文件编号变化时，先停止传输服务，执行只读原片核验，再启动服务：

```sh
docker compose stop backup
docker compose run --rm --no-deps backup verify-cache
docker compose up -d backup
```

核验不会开启同步、上传文件或删除相机原片。旧记录须满足既有完整导入记录、大小和修改时间一致，并核对相机素材身份及最多五段 64 KiB 内容；未通过的原文件和记录保留待处理。不完整读回视频，不计算视频 SHA-256。相机不可用时不会仅凭同名同大小恢复完成数量。

运行中的 SQLite 只通过容器内命令访问；不要用宿主机 SQLite 工具打开正在被 Docker 使用的数据库。离线备份前先停止 Compose。

## 构建与验证

传输使用 Python SMB 客户端，不依赖 rclone。`docker compose build` 构建当前 Docker 平台的镜像。需要 ARM64、AMD64 两种镜像时执行 `python3 scripts/build_images.py --arch all`，跨架构构建需 Docker 提供相应模拟能力；`--cached-downloads` 只适用于已有校验和与依赖下载缓存的环境。该脚本从 Alpine 官方站点下载并校验基础系统，可在 Docker Hub 无法访问时使用，仍需访问 Alpine 与 Python 包源。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests
```

单元测试使用临时目录、小文件和模拟设备，不需要真实凭据。实机验收请使用独立的 SMB 测试目录和可删除的小文件。

## 飞书进度卡

状态查询和自动进度使用相同的两路字节进度条，并独立展示完整导入数量和正式归档数量。标题、结论和关键数字加粗，少量图标辅助识别。上传达到 100% 但尚未核对大小、正式改名时仍显示正在确认归档。

自动通知开启时，开启同步后会在下一次通知检查（通常 5 秒内，网络重试除外）立即显示进度卡，尚未扫描或等待相机时也会显示状态。开始扫描后沿用这张卡片，后续按 `progress_interval_seconds` 更新。消息 ID 按批次、目的地、应用和接收人持久化，重启后继续更新。停止同步、关闭通知和本轮归档完成会结束原卡片的运行显示；关闭时只更新已存在卡片，不新发进度。菜单「状态 → 运行总览」仍可随时查询最新快照。

更新失败重试原卡片，不因网络错误或权限错误反复新发。只有飞书明确返回卡片已撤回或超出更新期限时，下一次有效进度通知才创建替代卡片。完成和失败通知独立去重与重试；归档完成卡展示实际 SMB 目录。配置格式和传输任务记录不变，无需迁移或清空缓存。

## 启动自检

传输服务每次启动检查：配置格式、SQLite 数据库、缓存盘挂载与卷身份、一次微小的本地写入、磁盘预留、相机身份及就绪状态、SMB 认证与目录访问、飞书应用认证。不会传输素材、删除原片或发送测试消息。相机离线不妨碍已有完整缓存上传；SMB 离线不妨碍相机导入；数据库损坏会阻止传输。

结果保存在 SQLite，飞书「状态 → 设备与存储」显示最近启动自检的时间与结果，当前连接状态另行刷新。可随时重新检查：

```sh
docker compose exec backup python3 -m media_importer check --json
```

这项检查不冒充完整传输验收：SMB 写入和改名权限、飞书接收人和发送权限，仍以实际传输和通知结果为准。容器无法启动时先运行 `docker compose ps` 和 `docker compose logs --tail 100`，检查 Docker 与宿主机挂载。

## 一键完整备份与迁移

宿主机需要 Python 3.9+ 和 Docker Compose。执行一条命令，自动暂停两个服务、保存 SQLite 一致快照、配置、连接凭据、程序与所有缓存，核验后恢复原来正在运行的服务。原配置和缓存不变。备份目录包含明文凭据，请私下保管；默认不压缩视频，也不计算视频全量 SHA-256。配置和数据库使用校验和，媒体沿用大小、修改时间与少量内容抽样核验。

在项目目录执行，`--output` 必须指向新目录。建议选外接盘，不能放在当前缓存目录内：

```sh
python3 scripts/manage.py backup --output "/Volumes/MediaSSD/media-importer-backups/backup-YYYYMMDD"
```

备份完整缓存需要对应的可用空间。仅有 `manifest.json` 且核验通过的目录才是完成的备份；中断会留下 `.incomplete-*` 目录并恢复原服务，该目录不能用于恢复。外接盘同盘备份可用于迁移和回滚；需要防止整块盘损坏时，应把完成的备份复制到另一块磁盘。

将**整个备份目录**复制到新电脑。恢复只接受尚不存在的项目目录与缓存目录，避免覆盖现有资料；两者应相互独立。可直接使用备份内携带的脚本：

```sh
python3 "/path/to/backup/project/scripts/manage.py" restore \
  --from "/path/to/backup" \
  --project "/path/to/media-importer" \
  --cache "/path/to/media-cache"
```

脚本准备相应版本镜像（本机没有时从备份源代码构建，需要联网）、核验并复制配置、记录和缓存、调整 `.env` 三个挂载路径。其他连接参数与归档记录保持不变。恢复后的同步与删除原片强制关闭，旧控制卡和待执行命令失效，历史待发通知取消，**不自动启动服务**。完整缓存保留原修改时间和卷标识，继续按既有任务归档。

停止旧电脑的两个 Compose 服务，再进入新项目目录运行 `docker compose up -d`，查看启动自检与飞书状态后开启同步。若相机 USB 地址变化，在新 `.env` 调整 `SOURCE_URL`。不要同时运行指向同一目的地和同一飞书应用的两套实例。Windows 可使用 `D:/MediaImport/app`、`E:/MediaImport/cache` 等路径；实际 Windows 相机链路仍需实机验收。

## 仓库与本机数据

仓库只包含源码、测试、部署文件和配置示例。真实 `.env`、`config/settings.json`、SQLite、完整缓存、备份包和运行日志均不提交。首次安装从示例创建本机配置；迁移既有实例使用完整备份与恢复命令，单独克隆仓库不包含任务记录或未上传的素材。

如果将挂载目录改为仓库内的其他位置，先将该目录加入 `.gitignore`；默认忽略规则只覆盖约定的本机目录。提交前用 `git diff --cached --name-only` 核对文件，不用 `git add -f` 强制加入本机配置。`.dockerignore` 仅允许构建所需源码与依赖清单进入镜像，并排除 macOS 元数据文件。
