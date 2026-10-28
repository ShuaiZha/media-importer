# Media Importer

**从相机导入素材，自动归档到自己的存储。**

[![Release](https://img.shields.io/github/v/release/ShuaiZha/media-importer)](https://github.com/ShuaiZha/media-importer/releases/latest)
[![Tests](https://github.com/ShuaiZha/media-importer/actions/workflows/ci.yml/badge.svg)](https://github.com/ShuaiZha/media-importer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[快速开始](#快速开始) · [使用文档](docs/README.md) · [兼容性](docs/compatibility.md) · [变更说明](docs/releases/v1.0.3.md)

当前源码为 **v1.0.3（待发布）**；以下安装命令获取已发布的正式版。

连接相机，在飞书中确认设备并开启备份。素材先完整导入本地缓存，再并行上传到 SMB 共享；目的地可以是 NAS，也可以是另一台电脑。

- **并行传输**：第一个文件导入完成即可上传，同时继续导入后续文件。
- **离线缓冲**：存储离线时先缓存，相机断开后仍可上传完整文件。
- **副本保护**：不覆盖已有文件；清理缓存前复核正式归档的大小与身份，无法核验时保留缓存。
- **飞书操作**：查看状态、配置参数、选择文件备份和核验清理，接收进度、完成与异常提醒。

![并行传输：文件 A 完整导入本地后开始上传，同时继续导入文件 B、C](docs/assets/parallel-transfer.svg)

目前接入 **GoPro、SMB、飞书单聊**，每个实例处理一台相机和一个目的地。主要实机验证组合为 **Mac Apple Silicon + HERO9 Black + SMB**；其他 GoPro 型号可确认后尝试导入，Windows / Linux 提供安装包与 USB 流程，硬件验证程度见[兼容性表](docs/compatibility.md)。

Mac 原生版另提供自动无线连接、拍摄控制、素材缩略图与回放，以及支持 Tailscale 的低延迟预览。相机页面适配电脑和手机；有声 RTMP 直播保留为[实验功能](docs/feishu.md#有声直播)。这些能力无需另装 App 或单独启动播放组件。

## 快速开始

准备可用的相机连接、可读写的 SMB 共享和[飞书应用凭据](docs/feishu.md#1-准备应用与凭据)。以下两种部署方式任选其一；无线连接与相机页面目前仅支持 Mac 原生版。

### 部署方式一：原生 CLI

直接在电脑上运行，无需 Docker 或另装 Python。

**macOS / Linux**

```sh
curl -fsSL https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.sh | sh
```

**Windows x64 · PowerShell**

```powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.ps1')))
```

命令下载最新正式包并引导填写缓存、SMB 和飞书配置。完成后用 `media-importer status` 检查后台；若找不到命令，按安装输出设置 PATH。后台在用户登录后运行，电脑需保持唤醒。

更新：`media-importer update` · 卸载：`media-importer uninstall` · [完整说明 →](docs/native.md)

原生更新支持跨平台[旧版本自动回收](docs/native.md#程序保留)：新版健康后保留当前版与上一健康版本，配置、任务与完整缓存保留。

### 部署方式二：Docker Compose

需要 Git、Python 3.9+ 和运行中的 Docker Compose。

```sh
git clone --branch v1.0.1 https://github.com/ShuaiZha/media-importer.git
cd media-importer
python3 scripts/setup.py
docker compose up -d --no-build
```

Docker 使用宿主机的 USB 网络连接，不需要 USB 直通。更新使用 `python3 scripts/manage.py update`；Windows 将 `python3` 换成 `py -3`。[完整说明 →](docs/deployment.md)

## 首次备份

1. 按[飞书指南](docs/feishu.md#3-订阅与菜单)完成事件、菜单和应用发布。
2. 连接已开机的相机，首次在飞书核对身份并「确认相机」。
3. 用「备份 → 选择文件备份」先验证一个小文件，再按需「开启自动备份」。

新安装默认**关闭备份、自动删除和自动通知**。原片删除是独立开关：开启后，整组素材完整导入本地并通过核验即可清理原片；远端归档前，本地缓存可能是唯一副本。手动清理默认只处理已归档素材，执行前显示范围并要求确认。

Mac 无线自动连接需要独立有线网络，相机使用独立供电并开启无线连接。首次配对、macOS 蓝牙和磁盘授权可能需要本人操作。远程画面通过 Tailscale 私有连接访问，见[网络与设备要求](docs/compatibility.md)。

## 文档与支持

| 我想… | 文档 |
| --- | --- |
| 安装、更新或卸载 | [原生 CLI](docs/native.md) · [Docker Compose](docs/deployment.md) |
| 配置飞书和日常操作 | [飞书指南](docs/feishu.md) · [配置参考](docs/configuration.md) |
| 理解备份、画面与拍摄的关系 | [业务流程](docs/workflows.md) |
| 处理故障、迁移或恢复缓存 | [使用与排障](docs/operations.md) · [迁移与恢复](docs/recovery.md) |
| 参与开发或查看发布内容 | [架构](docs/architecture.md) · [贡献指南](CONTRIBUTING.md) · [变更说明](docs/releases/v1.0.3.md) |

升级保留配置、未完成任务与完整缓存。问题请提交 [Issue](https://github.com/ShuaiZha/media-importer/issues)，附版本、系统及脱敏日志；不要上传凭据、任务数据库、缓存或私人素材。

项目源码采用 [MIT 许可](LICENSE)，内置第三方组件保留[各自许可](docs/licenses)。

## Star 趋势

<a href="https://www.star-history.com/?repos=ShuaiZha%2Fmedia-importer&type=date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=ShuaiZha/media-importer&type=date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=ShuaiZha/media-importer&type=date" />
    <img alt="Media Importer 的 GitHub Star 历史趋势" src="https://api.star-history.com/chart?repos=ShuaiZha/media-importer&type=date" width="800" />
  </picture>
</a>
