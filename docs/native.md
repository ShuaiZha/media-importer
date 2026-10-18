# 原生 CLI 部署

[文档目录](README.md) · [Docker Compose](deployment.md) · [配置参考](configuration.md)

原生版直接运行后台程序，包含 Python 与依赖，无需 Docker、系统 Python、Node.js 或额外 GUI App。它与 Docker 共用传输核心和数据格式。支持的平台及实机验证范围见[兼容性说明](compatibility.md)。

## 安装

先准备好 SMB 连接信息与[飞书应用凭据](feishu.md#1-创建应用与获取凭据)。安装向导会询问目录、连接和凭据，创建本机配置并注册后台服务。

macOS / Linux：

```sh
curl -fsSL https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.sh | sh
```

Windows x64，在 PowerShell 中执行：

```powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.ps1')))
```

脚本下载对应平台的最新正式版，核对安装包 SHA-256 后安装；该校验与视频传输无关。希望先检查脚本或离线安装时，从 [Releases](https://github.com/ShuaiZha/media-importer/releases/latest) 下载脚本或对应的 `media-importer-版本-平台.tar.gz`。安装包解压后，在内层 `media-importer` 目录执行 `./media-importer install`；Windows 使用 `./media-importer.exe install`。

新安装默认关闭同步、删除原片和自动传输通知。安装后继续完成[飞书订阅、菜单与发布](feishu.md#3-配置事件与卡片回调)，在机器人中确认设备并开启备份。重复执行安装不会覆盖已有配置，已有实例使用 `update`。

### 命令路径与数据位置

命令默认安装到 `~/.local/bin/media-importer`，Windows 为用户目录下的 `.local/bin/media-importer.cmd`。安装器不改写 Shell 配置；若终端找不到命令，使用安装结果给出的完整路径，或将该目录加入用户 PATH。macOS / Linux 可在当前终端执行：

```sh
export PATH="$HOME/.local/bin:$PATH"
media-importer status
```

| 平台 | 默认安装目录 |
| --- | --- |
| macOS | `~/Library/Application Support/MediaImporter` |
| Linux | `$XDG_DATA_HOME/media-importer`，默认 `~/.local/share/media-importer` |
| Windows | `%LOCALAPPDATA%/MediaImporter` |

全新安装的项目配置位于安装目录的 `instance/`；程序、日志、维护快照分别在 `releases/`、`logs/`、`backups/`。缓存可由向导指定到其他磁盘。`media-importer status` 显示实际配置、任务和缓存路径。

需要自定义时，安装包命令支持全局 `--home /path/to/install` 和 `install --bin-dir /path/to/bin`。运行目录与数据目录应专用于本项目，完整说明见[配置参考](configuration.md#文件与目录)。

### 后台与外接盘权限

后台服务随当前用户登录启动：macOS 使用 LaunchAgent，Linux 使用用户级 systemd，Windows 使用登录计划任务。电脑需保持唤醒；不支持登录前启动或自动唤醒。

macOS 首次运行或升级后可能要求允许 `media-importer` 访问可移动卷。允许访问实际缓存盘后再检查状态；服务可能在等待授权，启动检查超时不代表数据丢失。权限可在「系统设置 → 隐私与安全性 → 文件与文件夹」检查。缓存盘未挂载时等待原盘，不创建内置盘替代目录。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `media-importer status` | 安装版本、后台健康状态和数据路径 |
| `media-importer status --json` | 上述信息的 JSON 输出 |
| `media-importer check` | 检查配置、数据库、缓存与外部连接 |
| `media-importer logs` | 查看近期本机诊断日志 |
| `media-importer stop` | 停止整个后台；保留同步偏好和任务 |
| `media-importer start` | 按保存的设置启动后台 |
| `media-importer run` | 前台运行，供不使用系统服务的环境使用 |

飞书中的「停止同步」只停止传输和原片删除，机器人继续工作；CLI 的 `stop` 会同时停止机器人。两者的区别及排障见[日常使用](operations.md)。

## 更新

```sh
media-importer update
```

也可指定正式版本，例如 `media-importer update --version 0.10.21`。离线更新使用已解压的对应平台安装包：

```sh
media-importer update --bundle /path/to/package/media-importer
```

更新先下载并核验安装包、检查配置，再停止后台、保存配置与 SQLite 一致快照、切换程序，恢复原运行状态。配置、开关、任务和完整缓存保留；原来停止的服务保持停止，已是目标版本时不重启。服务重新启动后，相机需要再次确认。

快照不包含完整视频，不能替代[迁移备份](recovery.md)。更新失败时保留提示中的诊断与快照，排除原因后重试；不要将旧数据库直接覆盖到已经产生新任务的实例上。

## 与 Docker 切换

### 从 Docker 切到原生

在原项目目录停止两个 Compose 服务，再让原生安装器接管已有项目：

```sh
docker compose stop
curl -fsSL https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.sh | sh -s -- --project "$PWD"
```

Windows 在原项目停止 Docker 后执行：

```powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.ps1'))) -Project "$PWD"
```

安装器读取原 `.env` 中的三个目录，先保存配置和数据库快照，原地使用完整缓存；不覆盖凭据或开关。原生部署会登记数据归属，同一份任务和缓存不能由两个实例同时使用。旧版本 Docker 不一定识别该登记，必须保持旧容器停止。

### 从原生切回 Docker

执行普通卸载 `media-importer uninstall` 释放原生服务及数据归属，再在原项目运行 `docker compose up -d --no-build`。使用与当前配置、数据库格式兼容的镜像；不要使用 `--purge`。

## 卸载

| 命令 | 删除范围 | 保留内容 |
| --- | --- | --- |
| `media-importer uninstall` | 本实例的后台服务、命令入口、程序 | 配置、凭据、任务、缓存、日志和维护快照 |
| `media-importer uninstall --purge` | 上述程序以及登记的配置、凭据、任务、缓存、日志和程序维护快照 | 相机原片、SMB 归档及安装管理范围之外的文件 |

**彻底卸载会删除尚未上传的完整缓存。** 命令先列出实际路径，输入 `DELETE` 后执行；自动化调用需明确加上 `--yes`。普通卸载适合重新安装或切换部署，彻底卸载适合不再使用本地实例。

清理前核对原磁盘、目录归属及服务占用；失败时会报告残留，解决权限或磁盘问题后可重试。Windows 使用安装生成的 `media-importer.cmd`，退出当前进程后清理程序目录；普通卸载遇到占用也会列出残留路径。

Windows 彻底卸载在程序退出后等待文件占用释放，最多重试 30 秒。若仍无法删除，会返回失败并保留命令入口及旁边的 `.purge.json` 恢复记录。关闭占用安装目录的程序后，再执行 `media-importer uninstall --purge --yes` 完成剩余清理；即使部分程序文件已经删除，也无需重新安装。不要手动删除恢复记录。

此恢复机制已加入当前源码；已发布的 v0.10.21 安装包尚不包含修复，仍可能出现 `WinError 5` 或目录占用失败。Windows 后台服务和相机链路仍待实机验收。

独立源码仓库、另存的手工备份和安装包下载文件不属于安装管理范围，需自行处理。两个卸载命令均不删除相机素材、SMB 归档或 Docker 软件。

修改连接信息只需停止再启动后台；**移动配置、任务或缓存目录属于迁移**，需重新登记路径，步骤见[备份与迁移](recovery.md#原生实例备份与迁移)。
