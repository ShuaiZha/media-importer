# Mac 无线连接

USB 和 Wi-Fi 共用导入、上传及归档记录，切换连接方式不会重复归档。默认仍是 USB。

无线模式需要**在 Mac 手动连接相机热点**。程序不会安装额外 `.app`、申请定位权限或替你切换网络。相机通过 HTTP 向 Docker 提供素材；Mac 另需有线网络或第二条网络，才能同时访问远程 SMB 和飞书。

## 开始使用

1. 在飞书停止同步，保持删除原片关闭。
2. HERO9 开启无线连接。首次使用可进入「连接设备 → Quik 应用」，退出手机 Quik；从相机信息查看热点名称和密码，在 Mac Wi-Fi 菜单连接。连接后让相机返回正常拍摄预览。
3. `.env` 同时保留 USB 地址、序列号及 SMB/飞书凭据。无线地址通常为以下值；仅地址需要修改时编辑 `.env` 并重建容器：

   ```dotenv
   SOURCE_WIRELESS_URL='http://10.5.5.9:8080'
   SOURCE_REQUEST_ATTEMPTS=3
   ```

4. 飞书「设置 → 传输设置」点击「切换为手动 Wi-Fi」。选择保存在 JSON 的 `backup.source_transport`，后台自动应用，不需要重启容器；此操作不会开启同步。
5. 收到“相机已连接 · 手动 Wi-Fi”提醒后，在「状态 → 设备与存储」核对相机身份与 SMB 连接，再从「同步 → 开启同步」开始。第一个文件完整导入后开始上传，后续导入与上传并行。

停止同步后，Mac 保持当前 Wi-Fi；需要恢复家用网络时也在系统 Wi-Fi 菜单操作。切回 USB 时，先停止同步，插线并开机，在「传输设置」点击「切换为 USB 直连」，原 `SOURCE_URL` 继续使用。连接与断开提醒独立于同步开关；停止同步后仍会检测所选链路。

## 可选：蓝牙连接与热点重试

已经手动连上可访问的相机热点时，不依赖蓝牙助手也能导入。需要程序自动寻找绑定相机并开启热点时，可运行项目中的 Python 脚本。它不传输素材、不打开任务数据库，也不读取 Wi-Fi 密码。Mac Docker Desktop 无法直接使用宿主机蓝牙，因此这一个脚本在 Mac 运行，Compose 仍只有原来的两个服务。

要求 macOS、Python 3.11+。Windows 尚未实机验收。

```sh
python3.11 -m venv .venv/wireless
.venv/wireless/bin/python -m pip install -r requirements-wireless.txt
.venv/wireless/bin/python scripts/wireless.py --env .env
```

最后一个命令常驻运行，保持终端开启。按 macOS 提示允许蓝牙；首次配对时相机进入 Quik 配对界面，配对成功后返回预览。助手只在同步开启且需要相机时连接；热点准备好后显示“请在 Mac 连接相机 Wi-Fi”。你连接完成后，程序自动核对完整序列号并继续。

可选参数：

```dotenv
SOURCE_BLUETOOTH_ID=''
SOURCE_BLUETOOTH_SCAN_SECONDS=10
SOURCE_BLUETOOTH_CONNECT_SECONDS=45
SOURCE_WIRELESS_RETRY_SECONDS=10
```

`SOURCE_BLUETOOTH_ID` 留空时按序列号末四位寻找广播，再核对完整序列号；有同尾号相机时填写本机看到的蓝牙 UUID（不是 MAC 地址）。更换 Mac 后不要沿用旧 UUID。修改配置后重启脚本。

## 重试与停止

- 蓝牙扫描和连接有超时，失败逐步退避，最长 60 秒。握手使用有总超时的子进程，避免底层蓝牙卡住后无法停止。
- 停止同步或 Docker 心跳超过 8 秒未更新，助手取消连接；不会切换 Mac 的网络。
- 相机只读 HTTP 请求默认最多尝试 3 次；文件下载中断仍从零重传，删除请求不自动重发。
- 导入期间每 3 秒保活；导入复核完成后不再为了远程上传持续保活。
- 同步关闭后不启动传输或删除原片，完整缓存与归档记录继续保留。

| 情况 | 处理 |
| --- | --- |
| 蓝牙助手未运行或状态过期 | 需要蓝牙时启动脚本；相机 Wi-Fi 已可访问时不影响导入 |
| 找不到蓝牙或连接超时 | 开启相机无线、靠近 Mac；首次进入 Quik 配对页，退出手机 Quik；程序会重试 |
| 提示连接 Wi-Fi | 在 Mac 菜单手动连接相机热点，保持相机开机 |
| 相机忙碌 | 退出配对、设置或信息界面，返回拍摄预览 |
| 相机能读、SMB 离线 | 检查独立网络出口和 VPN；缓存额度内仍可继续导入 |

协议依据：[GoPro 蓝牙配对与身份核对](https://gopro.github.io/OpenGoPro/docs/ble/protocol/ble_setup/)、[热点就绪状态](https://gopro.github.io/OpenGoPro/docs/ble/networking/)、[Bleak macOS 配对说明](https://bleak.readthedocs.io/en/latest/backends/macos.html)。
