# 原生 CLI 部署

[首页](../README.md) · [Docker 部署与配置](deployment.md) · [飞书配置](feishu.md)

原生版在电脑直接运行，与 Docker 使用同一套导入、上传、飞书代码及数据格式。安装包包含 Python 和依赖，不安装 GUI App，不需要 Docker、系统 Python 或 Node.js。

## 系统范围

| 系统 | 后台启动 | 验证范围 |
| --- | --- | --- |
| macOS Apple Silicon | 当前用户 LaunchAgent，登录后启动 | 本机安装、启停、升级、卸载及 T7 / SMB 小文件验收 |
| macOS Intel | 同上；单独构建 amd64 包 | 构建与冒烟测试纳入 CI，待真机验证 |
| Linux amd64 / arm64 | `systemd --user` | 文件锁、服务定义、安装流程测试；待实际用户会话与 USB 验收 |
| Windows x64 | 当前用户登录任务，最低权限 | 原生打包、文件锁和任务定义已实现；待实机验收 |

后台服务依赖用户登录会话，电脑仍需保持唤醒。Linux 无 systemd 用户会话时可在终端运行 `media-importer run`，或继续使用 Docker。Windows ARM 原生包暂不提供。发行附件是否可下载，以 Releases 中实际通过构建的附件为准。

## 安装

Mac / Linux，在终端执行：

```sh
curl -fsSL https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.sh | sh
```

Windows x64，在 PowerShell 执行：

```powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.ps1')))
```

安装脚本按系统与架构选择正式发行包，核对程序包 SHA-256 后安装，不读取视频。若希望先查看脚本，可以从 [Releases](https://github.com/ShuaiZha/media-importer/releases) 下载 `install-native.sh` / `install-native.ps1`，查看后执行。也可下载对应系统的 `media-importer-版本-平台.tar.gz`，解压后进入内层 `media-importer` 目录：

```sh
./media-importer install
```

Windows PowerShell 使用 `./media-importer.exe install`。发行包自 v0.10.20 起提供；GitHub 下载不可用时，可以下载后离线安装或按下方说明从源码构建。

首次向导询问缓存目录、SMB 和飞书凭据，创建 `.env`、`config/settings.json` 与数据目录，再注册后台服务。相机地址可留空自动发现。同步、删除原片、传输通知默认关闭；识别相机后通过飞书确认备份。安装命令重复执行会提示更新，不覆盖已有配置。

命令默认安装到 `~/.local/bin/media-importer`（Windows 为用户目录下的 `.local/bin/media-importer.cmd`）。若终端找不到命令，使用安装结果显示的完整路径，或将该目录加入用户 PATH；不自动改写 Shell 配置。Mac/Linux 示例：

```sh
export PATH="$HOME/.local/bin:$PATH"
media-importer status
```

安装器默认目录：Mac `~/Library/Application Support/MediaImporter`，Linux `$XDG_DATA_HOME/media-importer`（默认 `~/.local/share/media-importer`），Windows `%LOCALAPPDATA%/MediaImporter`。全新安装的数据位于该目录的 `instance/`。程序版本位于 `releases/`，维护快照在 `backups/`，轮转日志在 `logs/`。可用全局参数 `--home /自定义安装目录` 和安装参数 `--bin-dir /命令目录` 指定位置。

**Mac 外接磁盘授权：** 原生后台首次启动或升级后，macOS 可能提示 `media-importer` 访问可移动卷。允许访问素材所在磁盘后再查看 `status`。安装器等待超时不代表文件丢失；服务可能仍在等待系统授权。这里只需要相关磁盘访问，不需要定位权限。授权被拒绝时检查系统设置中的「隐私与安全性 → 文件与文件夹」。缓存盘缺失或标识不符时等待原盘，不创建内置盘替代目录。

## 使用已有 Docker 配置和数据

先在原项目目录停止两个服务，再使用安装脚本接管该目录：

```sh
docker compose stop
curl -fsSL https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.sh | sh -s -- --project "$PWD"
```

已下载的安装包也可执行 `/安装包目录/media-importer install --project /原项目目录`。Windows 在原项目停止 Docker 后，在上述 PowerShell 安装命令末尾加 `-Project "$PWD"`。此过程不重新询问或覆盖已有凭据。

安装器读取原项目 `.env` 中的 `MEDIA_IMPORTER_CONFIG_HOST_DIR`、`MEDIA_IMPORTER_DATA_HOST_DIR` 和 `MEDIA_IMPORTER_CACHE_HOST_DIR`，相对路径以原项目为基准。保留原配置、开关、任务 ID、目的地身份、归档路径和完整缓存，启动前保存配置与 SQLite 一致快照。缓存原地使用，快照不复制整盘素材，也不处理已有 SMB 归档。

同一任务或缓存目录仍被 Docker 挂载时拒绝安装。原生安装在数据目录登记 `deployment.json`，本版及以后的 Docker 程序和更新器会识别此标记；旧版 Docker 不具备这个保护，务必保持旧容器停止。不要同时运行两种部署，也不要分别启动使用同一个飞书应用的两套机器人。

相机连接确认属于本次服务会话，切换后需要在飞书重新确认。已有完整缓存是否继续上传，仍由原来的同步开关决定。

## 常用命令

```sh
media-importer status          # 版本、服务、缓存位置
media-importer status --json
media-importer check           # 配置、数据库、缓存、相机、SMB、飞书认证
media-importer logs            # 最近的本机诊断日志
media-importer stop            # 停止后台，保留同步偏好和任务
media-importer start           # 按原设置恢复后台
media-importer update          # 最新正式版
media-importer uninstall       # 卸载程序，保留数据
```

日常设置仍在飞书操作，低频设置仍在 JSON；凭据仍在 `.env`。修改连接值后 `stop`、`start`；修改目录属于迁移，应先停止并卸载原生服务，将数据完整迁移、更新 `.env`，再以 `install --project` 登记，避免静默使用新空目录。

更新先下载并校验候选包、检查配置兼容性，再停止后台、保存 SQLite 与配置快照、切换程序并恢复原运行状态。原来停止的服务不会被更新擅自启动；已是目标版本时不重启。旧程序保留在 `releases/`，更新失败不自动倒退数据库。遇到问题先保持服务停止并保留快照，核对后恢复，不能直接把旧数据库覆盖到已有新任务上。

可指定正式版本：`media-importer update --version 0.10.20`。离线更新使用已解压的对应平台安装包：

```sh
media-importer update --bundle /安装包目录/media-importer
```

卸载仅停止并移除本实例的后台服务、命令入口和程序目录，保留 `.env`、JSON、SQLite、完整缓存、日志及维护快照；不提供自动清空素材的选项。Windows 若当前可执行文件被系统占用，会显示残留程序目录，命令退出后可手动移除该程序目录。卸载不删除 Docker，也不删除相机或 SMB 文件。

切回 Docker：先 `media-importer uninstall` 释放数据目录，确认原生服务已停止，然后在原项目执行 `docker compose up -d --no-build`。仅限配置和数据库格式兼容的镜像版本；本次格式仍为 JSON 4 / SQLite 5。不要同时启动。

## 从源码构建

开发构建需要 Python 3.12。每个安装包必须在目标系统和架构上构建；Mac 构建不能冒充 Windows/Linux 包。普通用户不需要以下依赖：

```sh
python3 -m venv .build/native-venv
.build/native-venv/bin/python -m pip install -r requirements-native-build.txt
.build/native-venv/bin/python scripts/build_native.py
```

Windows 使用 `py -3.12 -m venv .build/native-venv` 和 `.build/native-venv/Scripts/python.exe`。结果为 `dist/native/<平台>/media-importer/` 及同目录下的发行压缩包、校验清单。Mac 为控制台程序，没有额外 `.app`；当前没有 Developer ID 公证。不要关闭系统安全策略来运行来源不明的包，本机源码构建可用于开发验收。

```sh
.build/native-venv/bin/python scripts/smoke_native.py dist/native/macos-arm64/media-importer
dist/native/macos-arm64/media-importer/media-importer install --project "$PWD"
```

发布工作流分别构建 Mac ARM64/AMD64、Linux ARM64/AMD64、Windows AMD64，并对实际打包程序执行安装、同版本更新、卸载和数据保留冒烟测试。所有平台测试通过后才将原生附件与 Docker 镜像一起发布；CI 不连接真实相机、SMB 或飞书。
