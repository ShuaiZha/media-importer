# Docker Compose

[文档目录](README.md) · [原生 CLI](native.md)

两个服务：`backup` 导入与上传，`bot` 接收飞书操作。共享配置与数据，不需要开放服务端口。正式镜像为 `ghcr.io/shuaizha/media-importer`，提供 Linux ARM64 / AMD64。

## 准备

- Git、Python 3.9+、运行中的 [Docker Compose](https://docs.docker.com/compose/install/)。Windows 使用 Linux 容器。
- [支持的 USB 设备](compatibility.md)、缓存目录、已存在且可读写的 SMB 共享与子目录。
- [飞书 App ID、App Secret、本人 Open ID](feishu.md#1-准备应用与凭据)。

Docker 仅支持 USB，不负责宿主机蓝牙与 Wi-Fi 切换；无线连接、拍摄控制、实时预览及整合的素材回放页面使用 [Mac 原生版](native.md#无线连接与相机控制)。USB 通过网络读取，无需设备直通。宿主机连通不等于容器连通，VPN / Tailscale 环境也需从容器检查。

## 首次部署

```sh
git clone --branch v0.11.0 https://github.com/ShuaiZha/media-importer.git
cd media-importer
python3 scripts/setup.py
docker compose up -d --no-build
docker compose ps
```

Windows 将 `python3` 替换为 `py -3`。向导生成 `.env`、JSON 和目录，拉取正式镜像并检查连接，不覆盖已有配置。相机地址与序列号可留空自动发现。

示例固定为 v0.11.0。选择其他版本时，先在 [Releases](https://github.com/ShuaiZha/media-importer/releases) 确认对应镜像已发布。`main` 的程序和 Compose 镜像版本为 v0.11.2 开发版，尚无正式镜像，须按[源码构建说明](../CONTRIBUTING.md#docker)操作；不要直接拉取未发布的标签。

配置示例须与程序版本对应。version 5 JSON 用于 v0.11.0+，不能直接套用到旧镜像；升级首次启动会[迁移旧配置](configuration.md#从旧配置升级)，保留原配置值、任务和缓存。

检查完成后，继续[配置飞书订阅、菜单并发布](feishu.md#3-订阅与菜单)。首次同步前核对身份，保留原片，用少量素材验证。

按所安装版本配置[飞书菜单事件](feishu.md#3-订阅与菜单)，无需更换凭据。v0.11.2 的「连接相机」在 Docker 中只检查 USB；自动蓝牙与 Wi-Fi 连接需 Mac 原生版。查询和确认相机不再开启同步。

### 手动配置

不使用向导时，复制 [`.env.example`](../.env.example) 为 `.env`、[`config/settings.example.json`](../config/settings.example.json) 为 `config/settings.json`。还需按[配置参考](configuration.md#从旧配置升级)创建 SMB 凭据引用文件；填写真实值，提前创建三个挂载目录并授权 Docker 访问。默认目录为 `config/`、`data/`、`data/cache/`。

```sh
docker compose config --quiet
docker compose pull
docker compose run --rm --no-deps backup validate
docker compose up -d --no-build
```

`validate` 检查格式；容器健康表示后台运行正常，不代表外部设备在线。完整连接诊断：

```sh
docker compose exec backup python3 -m media_importer check --json
```

## 管理服务

| 目的 | 命令 |
| --- | --- |
| 健康状态 | `docker compose ps` |
| 任务状态 | `docker compose exec backup python3 -m media_importer status --json` |
| 近期日志 | `docker compose logs --tail 100 backup bot` |
| 停止 / 启动后台 | `docker compose stop` / `docker compose up -d --no-build` |
| 移除容器和网络 | `docker compose down` |

`stop`、`down` 保留宿主机配置、任务与缓存，但机器人会离线。日常暂停传输使用飞书「同步 → 停止同步」；v0.11.2 运行卡片的停止选项提供「当前文件完成后停止」，旧文字命令仍可用。

## 更新稳定版

在 Git 克隆的项目目录执行：

```sh
python3 scripts/manage.py update
```

指定版本可加 `--version 0.11.0`。更新器核对 Release、标签和镜像摘要，下载镜像后停止服务，保存一致快照、快进源码，再恢复原来运行的服务。配置、开关、未完成任务与完整缓存保留。

- 本地源码修改、分叉或自定义 Compose 覆盖可能阻止更新，先按错误提示处理。
- `backups/update-*/` 不含完整视频；迁移应使用[完整备份](recovery.md)。
- 生成的 `compose.override.yaml` 固定本机镜像，不提交到 Git。拉取源码不会自动更新容器。
- v0.10.15 及更早版本先 `git pull --ff-only` 获取更新器；v0.9 使用[一次性迁移](recovery.md#从-v09-迁移)。

修改 `.env` 后执行 `docker compose up -d --force-recreate --no-build`；JSON 偏好自动重读。换盘、换电脑和恢复备份见[迁移与恢复](recovery.md)。运行中的数据库只通过容器内 CLI 查询，不在宿主机直接打开。

飞书「设置」提供传输、通知和 SMB 归档位置；v0.10.25 起也支持 SMB 账号密码、相机连接和运行间隔，无需重建容器。v0.11.0 起连接统一存 JSON，密码只从已挂载配置目录的凭据文件读取，旧 `.env` 业务字段不再生效。连接修改须先停止同步，SMB 检查后确认。Docker 宿主机挂载与换盘仍在本机处理。见[飞书参数设置](feishu.md#参数设置)；升级程序后还需在飞书后台[更新并发布菜单](feishu.md#3-订阅与菜单)。

源码构建与发布见[贡献指南](../CONTRIBUTING.md#构建与发布)。
