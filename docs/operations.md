# 使用与维护

[返回首页](../README.md) · [部署与配置](deployment.md) · [飞书配置](feishu.md)

[日常操作](#日常操作) · [传输与清理](#传输与清理规则) · [故障排查](#故障排查) · [升级](#日常升级) · [备份与迁移](#备份与迁移)

## 日常操作

在飞书机器人单聊中使用三组菜单：

| 菜单 | 用途 |
| --- | --- |
| 状态 | 运行总览、设备与存储、归档结果 |
| 同步 | 开启同步、停止同步 |
| 设置 | 传输设置、原片处理、通知设置、使用帮助 |

相机连接并开机后，点击「同步 → 开启同步」。开启同步会处理绑定相机中所有待导入素材。只有「运行总览」显示**本轮素材已收齐**时，才表示本轮文件都已完整缓存或归档；相机可拔走，电脑和缓存盘继续保持可用。

三张状态卡的「刷新」会更新原消息，重新点击菜单会发送新快照。「可以断开（同步已停）」只说明当前没有相机读写，不表示素材已经收齐。未重新扫描时显示「已记录素材」，过期相机状态会标明更新时间。

自动通知默认关闭。在「设置 → 通知设置」开启后，开始同步通常会在 5 秒内显示进度卡，随后按设置间隔更新同一张卡片。两路字节进度、完整导入数量和正式归档数量分别统计；上传到 100% 后仍可能需要确认归档。导入完成、归档完成及需要处理的失败单独提醒，同一持续故障不重复刷屏。网络错误自动重试，普通逐文件日志保留在本机。具体菜单配置及通知验证见[飞书指南](feishu.md#6-验证交互与通知)。

设置通过飞书保存到 JSON；连接信息、凭据与挂载路径修改 `.env`。普通重启会沿用已保存的开关，维护前先停止同步。停止同步保留完整缓存，当前未完成文件下次从头传输。

## 传输与清理规则

导入先写本地临时文件；下载完整后核对大小和源身份，落盘、改名为完整缓存、保存记录。只有完整缓存进入上传队列。

上传使用唯一隐藏临时文件，写完核对大小与身份，以禁止覆盖方式改为正式名称，保存归档记录后清理对应缓存。正式目录为：

```text
目标子目录/设备名-序列号/原文件名
```

所有日期和 DCIM 子目录的素材在设备目录内平铺。不同素材同名或大小写冲突时添加 `_1`、`_2` 等后缀。已分配路径持久化。改名响应丢失时用操作记录和文件身份核验，不能仅凭同名同大小认领文件。不全量读回视频，不计算视频 SHA-256。

**不保留字节断点。** 停止、断网或重启后，未完成文件从零重传。已完整导入和已正式归档的文件不会重传。只删除确认归属的半文件；离线时登记待清理，恢复后处理。同步关闭时仍允许清理这些半文件，不清理完整缓存。已经完整上传、等待确认改名的文件进入归档恢复，不按半文件删除。

**开启删除原片后，一组素材完整导入缓存即可清理相机，不等 SMB 上传。此时缓存盘可能是唯一副本。** 删除前核对整组成员、源身份和本地完整文件；已经归档且缓存已清理的成员核对正式归档文件。删除期间保护对应缓存，只调用单文件删除接口并复扫。关联不明、身份变化或残留附属文件保留并报告。原片删除失败不阻塞上传，未归档完整缓存始终保留。

HERO9 固件 `HD9.01.01.72.00` 已通过短视频实机验证：MP4 与 LRV 完整导入后，删除 MP4 会联动移除对应 LRV，两份缓存随后继续归档。照片、连拍及其他固件的删除行为尚未完成实机验收；启用前应使用可删除的小文件验证。

## 启动自检

传输服务每次启动检查：配置格式、SQLite 数据库、缓存盘挂载与卷身份、一次微小的本地写入、磁盘预留、相机身份及就绪状态、SMB 认证与目录访问、飞书应用认证。不会传输素材、删除原片或发送测试消息。相机离线不妨碍已有完整缓存上传；SMB 离线不妨碍相机导入；数据库损坏会阻止传输。

结果保存在 SQLite，飞书「状态 → 设备与存储」显示最近启动自检的时间与结果，当前连接状态另行刷新。可随时重新检查：

```sh
docker compose exec backup python3 -m media_importer check --json
```

这项检查不冒充完整传输验收：SMB 写入和改名权限、飞书接收人和发送权限，仍以实际传输和通知结果为准。容器无法启动时先运行 `docker compose ps` 和 `docker compose logs --tail 100`，检查 Docker 与宿主机挂载。

## 缓存核验

完整缓存迁移后显示「待核验」时，先停止传输服务，执行只读原片核验，再启动服务：

```sh
docker compose stop backup
docker compose run --rm --no-deps backup verify-cache
docker compose up -d backup
```

核验不会开启同步、上传文件或删除相机原片。旧记录须满足既有完整导入记录、大小和修改时间一致，并核对相机素材身份及最多五段 64 KiB 内容；未通过的原文件和记录保留待处理。不完整读回视频，不计算视频 SHA-256。相机不可用时不会仅凭同名同大小恢复完成数量。

运行中的 SQLite 只通过容器内命令访问；不要用宿主机 SQLite 工具打开正在被 Docker 使用的数据库。离线备份前先停止 Compose。

## 故障排查

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
| 构建卡在下载基础镜像或依赖 | 检查 Docker 自身的网络/代理、Docker Hub 和依赖源访问，替代构建方式见[构建与测试](#构建与测试) |
| 宿主机可访问相机或 SMB，容器不通 | 检查 Docker 网络与宿主机 VPN 路由；用容器内 `check --json` 验证，不能仅凭 Finder/资源管理器访问成功判断 |
| SMB 已认证但目录不可访问 | 核对共享名与相对目录，提前创建子目录；服务器内部路径不能用作共享名 |
| 飞书菜单不显示 / 按钮无回复 | 按 [飞书排障表](feishu.md#常见问题) 分别检查发布、消息订阅、卡片回调与本人 Open ID |

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

升级前建议完成[完整备份](#备份与迁移)。构建失败时保留现有目录与缓存，排除故障后再启动；不要清空数据库或覆盖本机配置。若发行说明要求迁移，则按对应版本步骤执行。飞书菜单存于应用后台，代码更新不会自动修改菜单；旧应用需要按新版菜单表调整并发布。

## 备份与迁移

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

## 构建与测试

传输使用 Python SMB 客户端，不依赖 rclone。`docker compose build` 构建当前 Docker 平台的镜像。需要 ARM64、AMD64 两种镜像时执行 `python3 scripts/build_images.py --arch all`，跨架构构建需 Docker 提供相应模拟能力；`--cached-downloads` 只适用于已有校验和与依赖下载缓存的环境。该脚本从 Alpine 官方站点下载并校验基础系统，可在 Docker Hub 无法访问时使用，仍需访问 Alpine 与 Python 包源。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests
```

单元测试使用临时目录、小文件和模拟设备，不需要真实凭据。实机验收请使用独立的 SMB 测试目录和可删除的小文件。

## 仓库与本机数据

仓库只包含源码、测试、部署文件和配置示例。真实 `.env`、`config/settings.json`、SQLite、完整缓存、备份包和运行日志均不提交。首次安装从示例创建本机配置；迁移既有实例使用完整备份与恢复命令，单独克隆仓库不包含任务记录或未上传的素材。

如果将挂载目录改为仓库内的其他位置，先将该目录加入 `.gitignore`；默认忽略规则只覆盖约定的本机目录。提交前用 `git diff --cached --name-only` 核对文件，不用 `git add -f` 强制加入本机配置。`.dockerignore` 仅允许构建所需源码与依赖清单进入镜像，并排除 macOS 元数据文件。
