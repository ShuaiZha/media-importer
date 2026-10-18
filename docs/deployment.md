# Docker Compose 部署

[文档目录](README.md) · [原生 CLI](native.md) · [配置参考](configuration.md)

Compose 运行两个服务：`backup` 负责导入与上传，`bot` 负责飞书交互。它们共享配置、任务与缓存，无需网页服务或对外开放端口。镜像通过 GHCR 公开分发，提供 Linux ARM64 / AMD64。

## 准备条件

- Git、运行中的 Docker，以及 `docker compose`。macOS / Windows 可用 Docker Desktop，Linux 使用 Docker Engine 和 Compose 插件，见[官方安装指南](https://docs.docker.com/compose/install/)。
- 使用配置与更新脚本时，宿主机需要 Python 3.9+；手动首次配置不需要。
- [已适配的 USB 来源设备](compatibility.md)、可访问的缓存盘、已创建的 SMB 共享与子目录；仅有 USB 接口不代表设备已适配。
- [飞书应用凭据](feishu.md#1-创建应用与获取凭据)：App ID、App Secret、本人在该应用中的 Open ID。

相机、SMB、飞书都必须能从容器中访问。宿主机 Finder、资源管理器或 VPN 连通，不等于容器已经连通。Windows 使用 Linux 容器；实际设备链路仍需验证。

## 首次部署

### 1. 获取项目

```sh
git clone https://github.com/ShuaiZha/media-importer.git
cd media-importer
```

以下两种配置方法任选其一。已有安装不要重新运行首次向导，也不要覆盖真实配置。

### 2. 创建配置

推荐向导，填写缓存位置、SMB 和飞书信息：

```sh
python3 scripts/setup.py
```

Windows PowerShell 使用 `py -3 scripts/setup.py`。向导创建 `.env`、JSON 和目录，下载稳定版镜像并检查连接；相机地址和序列号可留空。向导不启动正式同步或发送测试消息，遇到已存在的配置会拒绝覆盖。相机离线时先完成连接检查，再启动服务等待连接。

**手动配置**时，macOS / Linux 执行：

```sh
cp .env.example .env
mkdir -p config data/cache
cp config/settings.example.json config/settings.json
chmod 600 .env config/settings.json
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force config, data/cache
Copy-Item config/settings.example.json config/settings.json
```

编辑 `.env`，替换 SMB、飞书示例值，填写三个实际目录；使用外接盘时，提前创建并授权 Docker 访问这些目录。按[配置参考](configuration.md)核对路径及 JSON。默认同步、删除原片与自动传输通知关闭。

### 3. 拉取并启动

在项目目录运行：

```sh
docker compose config --quiet
docker compose pull
docker compose run --rm --no-deps backup validate
docker compose up -d --no-build
docker compose ps
```

`validate` 只检查配置格式；容器健康状态表示后台正常，不等于相机或 SMB 在线。进一步检查：

```sh
docker compose exec backup python3 -m media_importer check --json
docker compose logs --tail 100 bot
```

然后按[飞书指南](feishu.md#3-配置事件与卡片回调)配置订阅、菜单并发布应用。通过「查看状态」确认三端状态，连接相机并核对身份后再备份。诊断不会代替实际 SMB 写入验收；首次传输使用小素材并保留原片。

## 启停与日志

| 目的 | 命令 |
| --- | --- |
| 启动已有服务 | `docker compose up -d --no-build` |
| 查看健康状态 | `docker compose ps` |
| 查看任务详情 | `docker compose exec backup python3 -m media_importer status --json` |
| 查看近期日志 | `docker compose logs --tail 100 backup bot` |
| 停止全部后台 | `docker compose stop` |
| 删除容器与 Compose 网络 | `docker compose down` |

`stop`、`down` 均保留宿主机上的配置、任务和完整缓存。日常启停传输使用飞书，机器人可继续接收操作；停止后台后机器人也会离线。

## 更新稳定版

在 Git 克隆的项目目录执行：

```sh
python3 scripts/manage.py update
```

Windows 使用 `py -3 scripts/manage.py update`。指定版本示例：`python3 scripts/manage.py update --version 0.10.21`。

更新器核对正式发行版、Git 标签与镜像摘要，先下载镜像，再停止服务、保存配置与 SQLite 一致快照、快进源码，恢复原来运行的服务并等待健康。凭据、开关、未完成任务和完整缓存保留；相机在服务重启后需要重新确认。

注意以下边界：

- 下载失败不停止服务；停止后发生失败会报告恢复资料路径，不直接用旧数据库覆盖新状态。
- `backups/update-*/` 包含配置、数据库、源码及镜像身份，**不包含完整视频**，不是完整迁移备份。
- 本地源码改动、分叉提交、自定义 Compose 覆盖文件或不兼容数据会阻止更新；先处理原因，不要强制重置配置或任务。
- 工具生成的 `compose.override.yaml` 固定本机镜像版本，不应提交。删除它会回到仓库 `compose.yaml` 的版本。

v0.10.15 及更早版本需先用 `git pull --ff-only` 获取更新脚本，再运行更新；v0.9 的数据格式需要[一次性迁移](recovery.md#从-v09-迁移)。恢复备份生成的目录不含 `.git`，不能直接使用 Git 更新流程。

## 修改配置与迁移

`.env`、JSON、SQLite 和缓存各自的职责见[配置参考](configuration.md)。编辑连接值后执行 `docker compose up -d --force-recreate --no-build`；JSON 偏好会自动重读。

换盘、换电脑或恢复任务请使用[迁移指南](recovery.md)，保留完整缓存、卷标识、恢复清单和数据库。不要在宿主机直接打开运行中的容器数据库；查询使用容器内 CLI。

需要源码构建和镜像发布时，见[贡献指南](../CONTRIBUTING.md)。
