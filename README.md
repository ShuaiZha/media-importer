# Media Importer

**把 GoPro 素材自动导入自己的存储，在飞书里掌握进度。**

连接相机，在飞书开启同步。素材逐个收进电脑或外接盘，完整缓存随即上传到 NAS、电脑共享等 SMB 存储。相机导入完成后可以拔走，电脑继续上传。

```text
GoPro → A 完整缓存 ─┬─→ 继续导入 B、C… → 本地
                    └─→ 上传 A → SMB
```

**两路同时进行：每个文件完整缓存后即可上传，无需等待全部素材导完。**

[快速开始](#快速开始) · [部署指南](docs/deployment.md) · [飞书配置](docs/feishu.md) · [使用与维护](docs/operations.md)

## 核心能力

- **边导入，边上传**：目的地离线时继续缓存，相机断开后继续上传完整文件。
- **在飞书里操作**：查看两路进度、启停同步、调整设置，接收完成与失败提醒。
- **完整收集素材**：视频、照片和附属文件一起导入，按设备平铺归档，保留原名，重名自动加后缀。
- **按需释放相机空间**：可选择整组素材完整导入后删除原片，归档成功后自动清理对应缓存。

## 快速开始

准备好 **Docker Compose、可访问的 SMB 共享、飞书自建应用**，并用数据线连接已开机的相机。当前已实测 **GoPro HERO9 + macOS Docker Desktop**；其他机型与系统的相机链路仍需验证。详见[准备条件](docs/deployment.md#开始之前)。

### 1. 获取项目

以下命令用于 macOS / Linux 全新安装；Windows 命令见[部署指南](docs/deployment.md#1-克隆并创建本机文件)。

```sh
git clone https://github.com/ShuaiZha/media-importer.git
cd media-importer
cp .env.example .env
mkdir -p config data/cache
cp config/settings.example.json config/settings.json
chmod 600 .env config/settings.json
```

### 2. 填写连接信息

编辑 `.env`，填写相机地址与序列号、SMB 连接与凭据、飞书 App ID / App Secret / 本人 Open ID。将示例地址、序列号和凭据换成自己的值；外接缓存盘也在这里配置。

→ [相机、SMB 与缓存配置](docs/deployment.md#2-编辑-env确认三类连接与缓存位置) · [创建飞书应用并获取凭据](docs/feishu.md#1-创建应用并获取凭据)

### 3. 启动服务

```sh
docker compose build
docker compose run --rm --no-deps backup validate
docker compose up -d --no-build
docker compose ps
```

镜像从源码在本机构建。启动后，按[飞书指南](docs/feishu.md#3-配置事件与卡片回调)完成事件订阅、菜单和应用发布，再通过「状态 → 运行总览」查看连接结果。

**新安装默认关闭同步、自动通知和删除原片。** 确认连接正常后，在「同步 → 开启同步」开始导入；自动提醒在「设置 → 通知设置」中开启。

## 日常使用

| 飞书菜单 | 你可以做什么 |
| --- | --- |
| **状态** | 查看运行总览、设备与存储、最近归档结果及实际位置 |
| **同步** | 开启或停止导入与上传 |
| **设置** | 调整传输限制、原片处理、通知，查看使用帮助 |

看到**本轮素材已收齐**，就可以拔走相机；电脑和缓存盘保持可用，直到远端归档完成。进度更新同一张卡片，完成和失败单独提醒。

## 使用前了解

- 当前支持**单台 GoPro、单个 SMB 目的地、飞书本人单聊**。日常控制通过机器人完成，无需网页或公网回调端口。
- **中断的文件从头重传**，已完整导入或正式归档的文件不会重复传输。同步开关会保存，普通重启沿用上次设置。
- **删除原片发生在完整导入本地后，不等待远端归档。** 开启前请确认缓存盘可靠，此时它可能是唯一副本。

详细的[清理规则](docs/operations.md#传输与清理规则)、[故障排查](docs/operations.md#故障排查)、[升级](docs/operations.md#日常升级)与[备份迁移](docs/operations.md#备份与迁移)见维护文档。问题与建议欢迎提交 [Issue](https://github.com/ShuaiZha/media-importer/issues)，请勿附带真实凭据或私人素材。
