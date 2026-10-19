# Media Importer

**轻量的素材导入与归档工具，将设备中的素材收进自己的存储。**

[![Release](https://img.shields.io/github/v/release/ShuaiZha/media-importer)](https://github.com/ShuaiZha/media-importer/releases/latest)
[![Tests](https://github.com/ShuaiZha/media-importer/actions/workflows/ci.yml/badge.svg)](https://github.com/ShuaiZha/media-importer/actions/workflows/ci.yml)

通过「来源设备 → 本地缓存 → 归档目的地」完成传输，在飞书中确认设备、控制同步和接收通知。本地缓存可以放在内置盘或外接 SSD。

**当前适配：GoPro USB 来源、SMB 目的地、飞书单聊。** SMB 目的地可以是 NAS、电脑共享或其他 SMB 服务器；其他品牌与协议尚未接入，具体机型和实测范围见[兼容性说明](docs/compatibility.md)。

![并行时序：文件 A 完整缓存后开始上传，同时继续导入 B、C](docs/assets/parallel-transfer.svg)

**导入与上传并行：第一个文件完整缓存后即可上传，同时继续导入后续文件，无需等待整批导入完成。**

[快速开始](#快速开始) · [完整文档](docs/README.md) · [兼容性](docs/compatibility.md) · [问题反馈](https://github.com/ShuaiZha/media-importer/issues)

## 为什么使用

- **减少手动操作**：无需取出存储卡；通过飞书确认设备、启停同步、接收结果。
- **适合远程存储**：目的地离线时先缓存，来源设备断开后继续上传完整缓存。
- **保留原文件名**：按设备平铺归档，重名添加后缀，已有归档不覆盖。
- **按需释放空间**：已验证的设备可在整组素材完整导入后清理原片；归档成功后自动清理缓存。

## 快速开始

准备[已适配的来源设备](docs/compatibility.md)、USB 数据线、可读写的 SMB 共享，以及[飞书自建应用](docs/feishu.md)。设备需开机，电脑需保持唤醒并能访问设备、SMB 和飞书。

选择一种部署方式，两者共用传输核心、设备适配、配置和数据格式：

| 方式 | 适合的环境 | 入口 |
| --- | --- | --- |
| 原生 CLI | 希望直接安装命令和后台服务，无需 Docker 或系统 Python | [安装、更新、卸载](docs/native.md) |
| Docker Compose | 已使用 Docker，希望用容器管理服务 | [部署、更新](docs/deployment.md) |

### 原生 CLI

macOS / Linux：

```sh
curl -fsSL https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.sh | sh
```

安装向导会创建连接配置并注册后台服务。Windows PowerShell 命令、离线安装和 PATH 设置见[原生部署指南](docs/native.md)。也可直接下载 [Release 安装包](https://github.com/ShuaiZha/media-importer/releases/latest)。

### Docker Compose

需要 Git、Python 3.9+ 和已运行的 Docker Compose。在全新目录执行：

```sh
git clone https://github.com/ShuaiZha/media-importer.git
cd media-importer
python3 scripts/setup.py
docker compose up -d --no-build
```

向导创建本机配置、拉取稳定版镜像并检查连接。手动配置及 Windows 命令见[Docker 部署指南](docs/deployment.md)。

### 开始第一次备份

1. 按[飞书指南](docs/feishu.md)完成消息订阅、卡片回调、菜单和应用发布。
2. 连接并开启来源设备，在机器人确认卡中核对型号、序列号与原片策略。
3. 点击「确认并备份」，通过「状态」查看本地导入与远端归档的进度。

## 运行规则

- **新安装默认关闭同步、删除原片和自动传输通知**；在飞书确认备份后开启同步。自动通知在 JSON 中启用。
- **删除原片在完整导入本地后执行，不等待远端归档**。开启时需确认：远端上传完成前，缓存盘可能是唯一副本。
- **中断的文件从头重传**，已完成文件不重复传输；未归档的完整缓存始终保留。
- **新导入的文件在读取时计算 SHA-256**。上传再算一次，不一致就停止并保留缓存，不把归档文件从 SMB 读回来核对。
- **同一台已确认相机在服务重启后继续同步**。断开后重连、换相机或固件变化仍需在飞书重新确认；旧确认卡无效。
- 默认不归档能确认关联的 LRV、THM 预览文件，可在配置中开启。整轮完成后状态回到等待新素材；已上传的任务留在本机，用来认出同一份素材。

当前每个实例处理一个来源设备和一个归档目的地。设备适配、固件及各系统的实测范围统一记录在[兼容性说明](docs/compatibility.md)。

## 文档与参与

- [配置参考](docs/configuration.md)：连接、凭据、缓存目录与 JSON 设置。
- [日常使用与排障](docs/operations.md)：飞书操作、进度含义、更新与故障处理。
- [备份、迁移与恢复](docs/recovery.md)：哪些数据需要保留，如何恢复完整缓存。
- [架构](docs/architecture.md)：一个文件怎么走，模块各管什么，哪些记录不要合并。
- [贡献指南](CONTRIBUTING.md)：设备适配、开发测试和发布流程。

欢迎通过 [Issues](https://github.com/ShuaiZha/media-importer/issues) 反馈问题。请提供版本、系统和脱敏日志，勿附带凭据、数据库或私人素材。
