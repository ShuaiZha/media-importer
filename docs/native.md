# 原生 CLI

[文档目录](README.md) · [Docker 部署](deployment.md)

原生版自带 Python 与依赖，直接运行命令和后台服务，与 Docker 共用核心与数据格式。平台与实机范围见[兼容性](compatibility.md)。

## 安装

先准备 SMB 连接信息与[飞书凭据](feishu.md#1-准备应用与凭据)，再运行配置向导。

macOS / Linux：

```sh
curl -fsSL https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.sh | sh
```

Windows x64，PowerShell：

```powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.ps1')))
```

脚本自动下载本平台正式安装包，核对 SHA-256 并安装，无需手动下载。离线安装时，从 [Releases](https://github.com/ShuaiZha/media-importer/releases/latest) 下载对应 `.tar.gz`，解压后进入内层 `media-importer` 目录，执行 `./media-importer install`；Windows 使用 `./media-importer.exe install`。

向导创建配置并注册后台，新安装的同步、删除原片与自动通知均关闭。已有配置不会被覆盖。随后[配置飞书订阅并发布](feishu.md#3-订阅与菜单)。

### 命令与数据位置

命令入口为 `~/.local/bin/media-importer`，Windows 为用户目录中的 `.local/bin/media-importer.cmd`。若提示找不到命令，使用安装结果给出的完整路径，或将该目录加入用户 PATH。macOS / Linux 当前终端可执行：

```sh
export PATH="$HOME/.local/bin:$PATH"
```

| 平台 | 默认安装目录 | 后台方式 |
| --- | --- | --- |
| macOS | `~/Library/Application Support/MediaImporter` | LaunchAgent |
| Linux | `$XDG_DATA_HOME/media-importer`，默认 `~/.local/share/media-importer` | 用户级 systemd |
| Windows | `%LOCALAPPDATA%/MediaImporter` | 用户登录计划任务 |

新安装的项目在 `instance/`，程序、日志、维护快照在 `releases/`、`logs/`、`backups/`。缓存可指定到外接盘；`media-importer status` 显示实际路径。自定义安装位置使用全局 `--home`，命令目录使用 `install --bin-dir`。

后台在**当前用户登录后**启动，电脑需保持唤醒。macOS 首次运行或升级后可能要求访问已配置的文件夹或磁盘，需要本人允许；“下载”“文稿”等内置盘目录也受系统保护，换到内置盘不能保证免除授权提示。外接缓存盘未挂载时等待原盘，不改用其他磁盘。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `media-importer status` | 版本、后台健康和数据路径；支持 `--json` |
| `media-importer check` | 配置、数据库、缓存与连接诊断 |
| `media-importer logs` | 近期日志 |
| `media-importer stop` / `start` | 停止 / 启动整个后台，保留偏好与任务 |
| `media-importer run` | 前台运行，适用于不使用系统服务的环境 |

停止后台会让机器人离线；日常暂停传输请在飞书选择「同步 → 立即停止」（v0.11.0 开发版为「停止同步」）；需等待当前文件完成时发送文字命令 `当前文件完成后停止`。

飞书「设置」提供素材、传输、通知与归档配置。v0.10.25 起也可修改 SMB 账号密码、相机连接和运行间隔；连接修改前须停止同步，SMB 检查通过后确认保存。首次飞书凭据由安装向导填写，目录迁移仍在本机进行。见[参数设置](feishu.md#参数设置)与[配置来源](configuration.md#文件与目录)；升级程序后还需在飞书后台[更新并发布菜单](feishu.md#3-订阅与菜单)。

「相机状态」「选择素材」「清理原片」需要 [v0.10.26](releases/v0.10.26.md)。从旧版更新后补齐飞书菜单，无需新增飞书权限或更换凭据。

## 无线连接与相机控制

[v0.11.0 开发版](releases/v0.11.0.md)在 Mac 原生包中加入自动 Wi-Fi 连接和蓝牙拍摄控制；Linux / Windows 继续使用 USB。无需额外 App，但首次配对、蓝牙与目录访问权限可能需要本人授权。具体网络要求见[兼容性](compatibility.md#无线连接与拍摄控制)。

Mac 包同时内置 go2rtc 预览组件，随安装、更新和卸载统一管理，无需另装 FFmpeg。使用「相机 → 实时预览」按需打开和结束 HERO9 无线画面；支持本机观看，也可配置下方的 Tailscale 远程入口，见[使用说明](feishu.md#实时预览)。

## 更新

```sh
media-importer update
```

指定版本使用 `media-importer update --version 0.10.26`；离线更新使用 `media-importer update --bundle /path/to/package/media-importer`。

更新按“下载与核验 → 停止后台 → 配置与 SQLite 一致快照 → 切换程序 → 恢复运行状态”执行。配置、开关、未完成任务和完整缓存保留。已是目标版本则不替换、不重启；拉取源码也不会更新已安装程序。

在线安装与更新只获取已发布的正式包。若 `main` 的版本尚未出现在 Releases，需等待发布或按[贡献指南](../CONTRIBUTING.md#原生包)构建后使用 `--bundle` 更新。

v0.11.0 开发版首次启动会将旧配置迁入 version 5：连接和偏好统一存 JSON，SMB 密码单独保存，`.env` 保留飞书凭据、本机目录与端口。原配置值、任务和缓存保留；旧 `.env` 业务字段不再覆盖 JSON。升级与回滚规则见[配置迁移](configuration.md#从旧配置升级)。

快照不含视频，不能替代[完整备份](recovery.md)。失败时保留诊断和快照，不直接用旧数据库覆盖已产生的新任务。

## 切换部署方式

Docker → 原生：在原项目目录先 `docker compose stop`，再接管已有项目：

```sh
curl -fsSL https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.sh | sh -s -- --project "$PWD"
```

Windows：

```powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://github.com/ShuaiZha/media-importer/releases/latest/download/install-native.ps1'))) -Project "$PWD"
```

安装器保存快照，沿用原凭据、设置和缓存。原生 → Docker：普通卸载后，在原项目启动兼容当前数据格式的镜像。**同一份任务与缓存只能运行一个实例。**

## 卸载

| 命令 | 范围 |
| --- | --- |
| `media-importer uninstall` | 移除程序、命令和后台服务，保留所有本机数据 |
| `media-importer uninstall --purge` | 另删登记的凭据、配置、任务、缓存、日志和维护快照 |

彻底卸载先列出路径，输入 `DELETE` 确认；自动化调用需显式加 `--yes`。**未上传的完整缓存也会被删除。** 两种卸载均不操作相机原片、SMB 归档、Docker 或安装管理范围之外的源码与手工备份。

Windows 请使用安装生成的 `.cmd` 入口。彻底卸载等待文件占用释放，最多重试 30 秒；失败时保留入口及 `.purge.json` 恢复记录。解除占用后重试 `media-importer uninstall --purge --yes`，不要先删恢复记录。此恢复机制从 v0.10.22 起提供。

修改连接与凭据见[配置生效方式](configuration.md#修改后生效)；移动配置、任务或缓存目录见[迁移指南](recovery.md)，不能仅修改路径。

## 远程预览：Tailscale

本节仅适用于已集成预览的 v0.11.0 Mac 原生开发版。运行程序的 Mac 和观看设备均需连接 Tailscale，且访问策略允许该端口；Mac 仍需有独立的有线上网链路。

1. 在本机执行 `tailscale status` 确认登录，找到这台 Mac 的 MagicDNS 名称。
2. 本机播放端口默认 `18765`，低延迟视频端口默认 `18555` TCP/UDP；仅需修改端口时，在 `.env` 设置 `PREVIEW_LOCAL_PORT` / `PREVIEW_RTC_PORT` 并重启后台。
3. 在本机创建专用私有映射（如已有相同端口用途，请选择空闲端口）：

   ```sh
   tailscale serve --bg --https=8443 http://127.0.0.1:18765
   ```

   macOS 的 CLI 不在 PATH 时使用 `/Applications/Tailscale.app/Contents/MacOS/Tailscale`。首次使用按 Tailscale 提示启用 HTTPS。这里使用私有 Serve，不使用 Funnel。

4. 飞书「设置 → 相机连接 → 预览设置」，点击「读取本机入口」，或填写 `https://mac.example.ts.net:8443`。点击「检查并保存」。地址只保存到 JSON，不再写 `.env`，也不需要重启。
5. 「检查当前连接」核验本机播放服务、Tailscale 登录、Serve 映射和 HTTPS 握手；不会更改系统配置或开启预览。本机检查通过后，仍需在实际观看端验证网络和访问策略。
6. 从「相机 → 实时预览」打开画面。默认优先 WebRTC：页面与协商经过 HTTPS，视频经 Tailscale 私有地址的 `18555` TCP/UDP 端口。访问策略也需允许观看设备访问该端口；无需路由器公网端口转发。

取消映射使用 `tailscale serve --https=8443 off`，不要使用整体 reset。关闭预览立即撤销会话链接，但保留映射供下次使用。播放器仅开放当前会话的只读播放接口，go2rtc 管理接口仍在本机内部。连接被拒绝时检查程序后台、Serve、Tailscale 和端口；页面打开但无画面时检查相机链路和客户端播放支持。

低延迟通道约 10 秒未出画时，自动回退到兼容播放：Apple 客户端使用 MPEG-TS HLS，其他客户端使用 MSE，均通过原来的 HTTPS 入口。页面显示实际模式，也可主动选择「兼容播放」或「播放／重新连接」重试。HLS 保留相机真实编码参数，不额外转码，但通常需要更多播放缓冲。WebRTC 不使用外部 STUN/TURN 服务，也不请求手机摄像头或麦克风权限。

停止预览撤销链接并回收媒体进程。若电脑代理使用 Fake-IP，可仅为预览域名添加 DNS／直连例外；无需关闭全部代理。
