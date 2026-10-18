# 文档

[项目首页](../README.md)

Media Importer 是轻量的素材导入与归档工具：从来源设备导入本地缓存，并行上传完整文件到归档目的地。日常操作在飞书中完成，连接与偏好保存在本机文件中。

当前已接入 GoPro USB 来源、SMB 目的地和飞书单聊；具体设备、固件与实机验证结果见[兼容性说明](compatibility.md)。本目录的部署与操作步骤对应这些已实现的适配。

## 按任务查找

| 你要做什么 | 阅读 |
| --- | --- |
| 确认设备、存储协议与系统是否适用 | [兼容性与运行条件](compatibility.md) |
| 不用 Docker，直接安装后台程序 | [原生 CLI 部署](native.md) |
| 使用容器部署 | [Docker Compose 部署](deployment.md) |
| 创建机器人、配置菜单与通知 | [飞书配置](feishu.md) |
| 修改连接、缓存位置或传输偏好 | [配置参考](configuration.md) |
| 查看进度、维护服务、排查故障 | [日常使用与维护](operations.md) |
| 更换电脑、备份或恢复缓存 | [备份、迁移与恢复](recovery.md) |
| 参与开发、运行测试或发布版本 | [贡献指南](../CONTRIBUTING.md) |

第一次部署建议按「兼容性 → 飞书凭据 → 选择部署方式 → 完成飞书订阅与发布」进行。原生 CLI 与 Docker 共用核心，但同一份任务和缓存只能由一个实例运行。

## 文档约定

- `main` 分支文档说明当前代码；使用旧版时，请查看对应 Git 标签下的文档。
- 命令块注明 Shell；未注明平台的 `sh` 命令用于 macOS / Linux。Windows 使用 PowerShell 示例。
- `storage.example.com`、`/path/to/...`、`/Volumes/MediaSSD/...` 和示例 ID 均需替换，不是真实连接信息。
- MiB / GiB 按 1024 进制计算。“导入完成”表示本地副本完整，“归档完成”表示目的地正式文件已核验并记录。
- 本仓库不包含运行配置、凭据、任务数据库、素材或本机备份。克隆代码不等于迁移实例。

## 版本说明

所有发行版与安装附件见 [Releases](https://github.com/ShuaiZha/media-importer/releases)。仓库中保留的版本说明：

- [v0.10.21：原生 CLI 彻底卸载](releases/v0.10.21.md)
- [v0.10.20：原生 CLI 部署](releases/v0.10.20.md)
