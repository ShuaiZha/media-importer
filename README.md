# Media Importer

**把 GoPro 素材自动导入自己的存储，在飞书里掌握进度。**

通过 USB 连接相机，在飞书确认识别结果后开始备份。素材逐个收进电脑或外接盘，完整缓存随即上传到 NAS、电脑共享等 SMB 存储。相机导入完成后可以断开，电脑继续上传。

```text
GoPro → A 完整缓存 ─┬─→ 继续导入 B、C… → 本地
                    └─→ 上传 A → SMB
```

**两路同时进行：每个文件完整缓存后即可上传，无需等待全部素材导入完成。**

[快速开始](#快速开始) · [部署指南](docs/deployment.md) · [飞书配置](docs/feishu.md) · [使用与维护](docs/operations.md)

## 核心能力

- **边导入，边上传**：目的地离线时继续缓存，相机断开后继续上传完整文件。
- **在飞书里操作**：查看两路进度、启停同步、切换原片删除，接收完成与失败提醒。
- **按需收集素材**：全部、仅视频或仅照片，附属文件随素材导入；按设备平铺归档，保留原名，重名自动加后缀。
- **按需释放相机空间**：可选择整组素材完整导入后删除原片，归档成功后自动清理对应缓存。

整轮完成后自动清理任务明细，只保留必要的去重信息；未完成任务和完整缓存继续保留。

## 快速开始

准备好 **可访问的 SMB 共享、飞书自建应用**，并用数据线连接已开机的相机。两种部署共用传输和飞书代码，选择一种运行：

| 部署 | 安装与维护 |
| --- | --- |
| **Mac 原生 CLI** | 包含 Python 与依赖，无需 Docker；[安装、更新、卸载](docs/native.md) |
| **Docker Compose** | ARM64 / AMD64 镜像；按下面步骤部署 |

Mac / Linux 原生命令安装：

```sh
curl -fsSL https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.sh | sh
```

安装后使用 `media-importer update` 更新。`media-importer uninstall` 卸载程序并保留数据；`media-importer uninstall --purge` 连同本机数据彻底清理，执行前列出范围并确认。Windows 命令、PATH 设置及 Docker 切换步骤见[原生部署说明](docs/native.md)。也可从 [Releases](https://github.com/ShuaiZha/media-importer/releases) 下载对应安装包。

Linux / Windows 已纳入原生构建和服务适配，设备链路仍待实机验收；目前可继续使用 Docker。GoPro 型号的验证范围见[兼容性说明](docs/deployment.md#相机兼容性)。以下为 Docker 部署。

### 1. 获取项目

以下命令用于 macOS / Linux 全新安装；Windows 命令见[部署指南](docs/deployment.md#1-克隆并创建本机文件)。

```sh
git clone https://github.com/ShuaiZha/media-importer.git
cd media-importer
```

### 2. 填写连接信息

使用 Python 3.9+ 运行首次配置向导，填写缓存盘、SMB 和飞书凭据；相机地址与序列号可留空自动发现。向导会生成配置、下载稳定版镜像并逐项检查连接，不覆盖现有安装：

```sh
python3 scripts/setup.py
```

也可按部署指南手动编辑 `.env`。凭据仅保存在本机，同步与删除原片开关由飞书保存到 JSON；其他偏好在 `settings.json` 中配置。

→ [相机、SMB 与缓存配置](docs/deployment.md#2-编辑-env确认三类连接与缓存位置) · [创建飞书应用并获取凭据](docs/feishu.md#1-创建应用并获取凭据)

### 3. 启动服务

```sh
docker compose up -d --no-build
docker compose ps
```

镜像通过 GitHub Container Registry 分发，支持 ARM64 / AMD64，无需本机构建或登录镜像仓库。启动后，按[飞书指南](docs/feishu.md#3-配置事件与卡片回调)完成事件订阅、菜单和应用发布，再通过「状态」查看连接结果。

**新安装默认关闭同步、传输通知和删除原片。** 插线并开启相机，机器人会发送型号与序列号，点击「确认并备份」开始。本次连接确认在服务重启或检测到断开后失效，已有完整缓存仍按同步开关上传。

已有安装更新到最新稳定版，在项目目录执行：

```sh
python3 scripts/manage.py update
```

Windows 可用 `py -3` 替代 `python3`。更新前保存配置及数据库快照，保留原开关、未完成任务与完整缓存；只更新正式发行版。旧版本首次接入及失败处理见[升级说明](docs/operations.md#日常升级)。

## 日常使用

| 飞书操作 | 作用 |
| --- | --- |
| **状态** | 两路进度、相机连接要求、设备与存储状态、问题和归档位置 |
| **开启同步 / 停止同步** | 同时控制导入和上传，停止后保留完整缓存 |
| **原片处理** | 保留原片或完整导入后删除；开启删除需确认 |

按状态卡中的连接要求断开相机；电脑和缓存盘保持可用，直到归档完成。启用自动通知后，进度更新同一张卡，完成和失败单独提醒。传输限制、素材类别、预览文件和通知偏好在 [`settings.json` 示例](config/settings.example.json)中设置，填写方法见[配置说明](docs/deployment.md#配置与数据)。

## 使用前了解

- 当前支持**单台 GoPro、单个 SMB 目的地、飞书本人单聊**。日常控制通过机器人完成，无需网页或公网回调端口。
- **中断的文件从头重传**。同一次已确认连接持续监听新素材；重连后重新确认，不改原有开关设置。
- 默认跳过已确认关联的 LRV、THM 预览文件；需要保留时，在 JSON 中设置 `backup.archive_previews: true`。
- **删除原片发生在完整导入本地后，不等待远端归档。** 开启前请确认缓存盘可靠，此时它可能是唯一副本。

详细的[清理规则](docs/operations.md#传输与清理规则)、[故障排查](docs/operations.md#故障排查)、[升级](docs/operations.md#日常升级)与[备份迁移](docs/operations.md#备份与迁移)见维护文档。问题与建议欢迎提交 [Issue](https://github.com/ShuaiZha/media-importer/issues)，请勿附带真实凭据或私人素材。
