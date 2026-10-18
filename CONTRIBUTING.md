# 贡献指南

欢迎通过 [Issues](https://github.com/ShuaiZha/media-importer/issues) 提交问题或建议，通过 Pull Request 提交改动。优先完善当前 USB → 本地缓存 → SMB 流程；新功能先说明要解决的具体场景，避免把配置与控制重新做成复杂平台。

## 问题反馈

请提供版本、操作系统与架构、部署方式、相机型号和固件、预期与实际行为、复现步骤，以及必要的脱敏日志。设备兼容结论需区分模拟测试、安装测试和真实素材验证。

不要上传 `.env`、真实 JSON、数据库、缓存、备份包、访问令牌或私人素材。与账户凭据有关的问题先删除敏感值，再描述现象。

## 本地开发

使用单独的开发目录和小型测试数据，不共用正在运行实例的配置、任务与缓存。核心测试在 Python 3.11 / 3.14 上运行；原生打包使用 Python 3.12。

macOS / Linux：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python scripts/check_repository.py
```

Windows PowerShell 可用 `py -3 -m venv .venv`，然后直接使用 `.venv/Scripts/python.exe` 执行同样的安装和测试命令。

测试使用临时目录、小文件和模拟设备，不要求真实相机、SMB 或飞书凭据。涉及传输与清理的改动应覆盖中断恢复、身份变化、禁止覆盖改名、失败重试和已完成文件不重复处理；实机验收使用独立测试目录和明确可删除的小素材。

## 代码与配置边界

- 传输与调度使用通用命名，GoPro、SMB、飞书差异留在各自适配器；不为尚未支持的协议预建框架。
- 连接和凭据属于 `.env`，偏好属于 JSON，未完成事项与必要去重属于 SQLite。
- 完整缓存与临时文件分开管理；未确认归属的源文件、临时文件或远端文件不能删除或覆盖。
- 原片删除、归档记录和缓存清理是不同事实，失败不能让已完成归档倒退。
- 保持原生与 Docker 共用核心；改变配置或数据库时提供迁移依据和对应测试。

## 构建

### Docker

在开发副本中构建本机镜像并生成本机覆盖文件：

```sh
python3 scripts/build.py --activate
docker compose up -d --no-build --pull never
```

`build.py` 使用干净的临时构建上下文，避开外接盘的 AppleDouble 元数据。双架构本地构建可用 `python3 scripts/build_images.py --arch all`，需 Docker 支持相应架构或模拟；这些命令不发布镜像。

### 原生包

每个包必须在对应系统和架构上构建。macOS / Linux：

```sh
python3.12 -m venv .build/native-venv
.build/native-venv/bin/python -m pip install -r requirements-native-build.txt
.build/native-venv/bin/python scripts/build_native.py
```

Windows 使用 `py -3.12 -m venv .build/native-venv` 和 `.build/native-venv/Scripts/python.exe`。输出位于 `dist/native/`，包括平台目录、压缩包和校验清单。

对实际打包程序运行隔离冒烟测试，以下为 macOS ARM64 示例：

```sh
.build/native-venv/bin/python scripts/smoke_native.py dist/native/macos-arm64/media-importer
```

该测试在临时实例中验证安装、更新、普通卸载和彻底卸载，不接管生产数据，也不访问真实设备。当前 macOS 包为控制台程序，没有额外 `.app`，尚未进行 Developer ID 公证。

## 提交前检查

1. 运行与改动相称的测试，检查 `git diff --check`。
2. 运行 `python scripts/check_repository.py`，再人工检查待提交文件与差异。脚本只识别部分敏感模式，不能替代复核。
3. 使用 `git diff --cached --name-only` 确认没有运行数据。自定义数据目录也要加入忽略规则，不使用 `git add -f` 强制提交。
4. 同步更新相关文档和示例，核对链接、命令、默认值及平台支持范围。

提交说明先写解决的问题与行为变化，再写必要的验证和限制；文档改动无需发布新版程序。

## 发布流程

此节面向有仓库发布权限的维护者。

1. 更新程序版本、`compose.yaml` 的镜像版本以及 `docs/releases/vX.Y.Z.md`，保持版本一致。
2. 运行测试、源码检查和必要的小文件验收；只提交代码、测试、部署文件及示例。
3. 推送匹配版本的 `vX.Y.Z` 标签，触发 `Stable release` 工作流。
4. 等待原生五平台测试及 Docker 双架构构建验证完成，检查 Release 附件、安装脚本、校验清单和镜像摘要。
5. 确认 GHCR 包可公开拉取，再验证正式安装与更新入口。

原生构建覆盖 macOS ARM64 / AMD64、Linux ARM64 / AMD64、Windows AMD64。CI 验证打包程序的安装维护行为，不连接真实相机、SMB 或飞书；硬件兼容结论记录在[兼容性文档](docs/compatibility.md)。

镜像发布使用 GitHub Actions 的 `GITHUB_TOKEN`。发行包与构建上下文不得包含个人凭据、运行配置、SQLite、缓存或备份。
