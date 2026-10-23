# 使用文档

[项目首页](../README.md)

## 从这里开始

1. [确认兼容性](compatibility.md)：设备、系统和网络要求。
2. [准备飞书凭据](feishu.md#1-准备应用与凭据)。
3. 安装 [原生 CLI](native.md) 或 [Docker Compose](deployment.md)，二选一。
4. [完成飞书订阅与发布](feishu.md#3-订阅与菜单)，确认设备后开启备份。

## 按任务查找

| 任务 | 文档 |
| --- | --- |
| 安装、更新、卸载原生程序 | [原生 CLI](native.md) |
| 部署、更新和管理容器 | [Docker Compose](deployment.md) |
| 创建机器人、配置菜单和通知 | [飞书](feishu.md) |
| 修改连接、凭据、目录和偏好 | [配置参考](configuration.md) |
| 查看进度、理解传输与清理、排查问题 | [使用与排障](operations.md) |
| 理解备份、观看、拍摄及相机占用 | [业务流程](workflows.md) |
| 备份、更换电脑、恢复完整缓存 | [迁移与恢复](recovery.md) |
| 理解模块、开发测试、发布版本 | [架构](architecture.md) · [贡献指南](../CONTRIBUTING.md) |

安装命令获取最新正式版；`main` 的文档随源码更新，旧版说明请切换对应 Git 标签。v0.11.6 将「同步」改称「备份」，事件标识与旧文字命令不变。示例中的域名、路径与 ID 均需替换。MiB / GiB 使用 1024 进制。

## 版本说明

安装包与镜像清单见 [GitHub Releases](https://github.com/ShuaiZha/media-importer/releases)。

当前正式版为 v0.11.2，提供五个平台的原生安装包及双架构 Docker 镜像。

`main` 已包含 [v0.11.7 未发布改动](releases/v0.11.7.md)。安装命令和稳定镜像仍使用上述正式版；升级保留配置、未完成任务与完整缓存。

| 版本 | 主要变化 |
| --- | --- |
| [v0.11.7（未发布）](releases/v0.11.7.md) | 相机素材页选择文件并提交备份，复用任务与队列 |
| [v0.11.6（未发布）](releases/v0.11.6.md) | 统一相机调度、状态与飞书业务入口 |
| [v0.11.5（未发布）](releases/v0.11.5.md) | 数据库初始化与查询分离，完成结果与通知发送分离 |
| [v0.11.2](releases/v0.11.2.md) | 飞书命令与卡片重整、统一统计、独立连接相机菜单 |
| [v0.11.0](releases/v0.11.0.md) | Mac 双链路、拍摄参数、WebRTC 预览、缩略图与回放、统一相机页面 |
| [v0.10.26](releases/v0.10.26.md) | 相机状态、原片清理、单个 MP4 选择上传与卡片精简 |
| [v0.10.25](releases/v0.10.25.md) | 飞书完整参数配置、底部菜单与停止回执 |
| [v0.10.24](releases/v0.10.24.md) | 飞书参数表单与 SMB 归档位置配置 |
| [v0.10.23](releases/v0.10.23.md) | 缓存核验修复，恢复“一次一清” |
| [v0.10.22](releases/v0.10.22.md) | 设备适配边界，Windows 卸载恢复 |
| [v0.10.21](releases/v0.10.21.md) | 原生 CLI 彻底卸载 |
| [v0.10.20](releases/v0.10.20.md) | 原生 CLI 部署 |
