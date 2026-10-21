# 贡献指南

欢迎提交 [Issue](https://github.com/ShuaiZha/media-importer/issues) 或 Pull Request。新功能先说明实际场景，围绕素材导入与归档，保持功能精简。

## 反馈问题

提供版本、系统架构、部署方式、设备型号与固件、复现步骤、预期与实际结果，以及必要的脱敏日志。不要附带凭据、真实配置、数据库、缓存、备份包或私人素材。

设备结论需区分模拟测试、安装测试和实机验证，统一记录在[兼容性](docs/compatibility.md)。

## 本地开发

使用独立目录、小文件和模拟设备，不共用生产配置与数据。核心 CI 使用 Python 3.11 / 3.14；原生打包使用 Python 3.12。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python scripts/check_repository.py
```

Windows 用 `py -3 -m venv .venv` 创建环境，再用 `.venv/Scripts/python.exe` 执行安装和测试。单元测试不要求真实设备、SMB 或飞书凭据。

## 修改边界

先读[架构](docs/architecture.md)。通用流程保持设备无关，差异放在适配器，不为未接入的品牌或协议预建框架。

- 接口在 [contracts.py](media_importer/contracts.py)，注册在 [adapters](media_importer/adapters/__init__.py)。
- 发现、专用格式、素材关联、保活与删除联动由设备适配器负责。
- 配置与数据库变化需有迁移依据；原生与 Docker 保持共用核心。
- 不覆盖已归档文件，不删除无法确认归属的文件，不丢弃未归档完整缓存。
- 删除事实、归档完成与缓存清理分别记录；通知失败不改变传输结果。

传输改动覆盖中断、身份变化、重名竞争、发布响应丢失和恢复；清理改动覆盖素材组保护与未知结果。来源提供 `content_sha256` 时，上传顺序读取中必须核对；不额外完整读回视频。新增设备的原片删除能力须单独验收。

## 构建与发布

### Docker

```sh
python3 scripts/build.py --activate
docker compose up -d --no-build --pull never
```

在开发副本操作。`build.py` 使用干净上下文，排除外接盘元数据；双架构本地构建用 `python3 scripts/build_images.py --arch all`，不自动发布。

### 原生包

在对应系统与架构上构建。macOS / Linux：

```sh
python3.12 -m venv .build/native-venv
.build/native-venv/bin/python -m pip install -r requirements-native-build.txt
.build/native-venv/bin/python scripts/build_native.py
.build/native-venv/bin/python scripts/smoke_native.py dist/native/macos-arm64/media-importer
```

最后一行以 macOS ARM64 为例，替换为实际产物路径。Windows 使用 `py -3.12` 与 `.build/native-venv/Scripts/python.exe`。输出在 `dist/native/`，包含程序、压缩包和校验清单。冒烟测试在临时实例中检查安装、更新与卸载，不接管生产数据。

Mac 构建会下载并校验固定版本的 go2rtc，连同 MIT 许可放入包内 `components/`，运行时不下载组件。来源与校验值见 [`scripts/preview_bundle.py`](scripts/preview_bundle.py)；源码测试实时预览前先运行该脚本。不依赖系统 FFmpeg。其他平台不包含尚未适配的预览组件。

### 发布

1. 同步程序版本、Compose 镜像版本与 `docs/releases/vX.Y.Z.md`。
2. 测试与源码检查通过后，推送对应 `vX.Y.Z` 标签。
3. 等待 `Stable release` 工作流完成五平台原生测试和双架构 Docker 验证。
4. 检查 Release 附件、清单、镜像摘要、匿名拉取及正式安装和更新入口。

## 提交前

运行与改动相称的测试、`git diff --check` 和 `python scripts/check_repository.py`。人工复核暂存差异与文件清单；扫描工具不能替代敏感信息检查。

文档按“首页介绍、指南操作、参考参数”分工，同一规则只在一处详述，其他页面链接引用。命令注明平台，示例用占位值，图示需有文字说明；核对链接、默认值与实际命令。仅文档变化无需发布程序版本。
