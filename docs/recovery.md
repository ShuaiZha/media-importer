# 备份、迁移与恢复

[文档目录](README.md) · [配置参考](configuration.md) · [日常维护](operations.md)

迁移需要的是**连接配置、JSON、任务数据与完整缓存**，不只是源码。未归档缓存可能已是素材唯一副本，复制时保留隐藏文件、卷标识、`.receipts/` 清单及文件修改时间。已有 SMB 归档不迁移、不删除。

本文区分原生与 Docker 工具；`scripts/manage.py backup/restore` 当前面向 Docker，不能作为运行中的原生实例备份命令。

## 原生实例备份与迁移

### 制作一致备份

1. 用 `media-importer status` 确认实际项目、配置、任务与缓存位置。
2. 执行 `media-importer stop`，确认后台已停止。
3. 将项目 `.env`、整个配置目录、整个任务目录、整个缓存目录复制到新的备份目录，保留修改时间与隐藏文件。另存安装目录中的 `installation.json`，用于核对路径和版本。
4. 核对副本齐全后，如仍在原电脑运行，执行 `media-importer start`。

停止后整体复制任务目录，避免只复制 SQLite 主文件而遗漏尚未合并的日志。备份包含明文连接凭据，私下保存。升级时自动生成的 `backups/` 快照只有配置和数据库，不能替代完整缓存备份。

### 更换电脑或目录

1. 先按上节备份，再通过普通卸载 `media-importer uninstall` 停止并移除旧服务、释放数据归属。**不要使用 `--purge`**。
2. 从已释放归属的原位置复制项目配置及三个数据目录到新位置；保持隐藏标识与文件修改时间。卸载前的回滚备份含旧部署登记，不能直接当作已释放归属的迁移包。
3. 修改新项目 `.env` 中的路径，核对缓存完整；将新 `settings.json` 中同步和删除原片设为 `false`，`stop_after_current` 设为 `false`。
4. 在新电脑下载对应平台原生包，进入解压后的 `media-importer` 目录，执行 `./media-importer install --project /path/to/project`；Windows 使用 `./media-importer.exe`。
5. 检查 `status`、`check` 与飞书状态，确认配置和任务后再开启同步。

旧服务必须保持停止，同一飞书应用不能同时运行两个实例。旧电脑不可用、登记未释放或数据核验失败时，先保留全部副本排查，不手工改数据库或删除部署登记强行接管。

## Docker 完整备份与迁移

宿主机需要 Python 3.9+、Git 项目文件和 Docker Compose。Windows 将下列 `python3` 替换为 `py -3`。

### 备份

在项目目录执行，输出必须是尚不存在的新目录，且不能位于当前缓存内：

```sh
python3 scripts/manage.py backup --output "/path/to/backups/backup-YYYYMMDD"
```

工具暂停两个服务，保存配置、凭据、SQLite 一致快照、程序和所有缓存，核验后恢复原来运行的服务。完整复制需要足够空间。备份复制缓存时不重新计算视频 SHA-256；传输时的整文件校验见[完整性与归档](operations.md#完整性与归档)。只有带有有效 `manifest.json` 的完整目录才能恢复，中断留下的 `.incomplete-*` 目录不能使用。

备份包含真实凭据。存到另一块磁盘才能覆盖原磁盘损坏的情况；同盘备份可用于迁移与操作回滚。

### 恢复到新位置

复制整个备份目录到新电脑，指定尚不存在且相互独立的项目目录与缓存目录：

```sh
python3 "/path/to/backup/project/scripts/manage.py" restore \
  --from "/path/to/backup" \
  --project "/path/to/media-importer" \
  --cache "/path/to/media-cache"
```

工具准备对应镜像，核验并复制配置、记录与缓存，调整 `.env` 挂载路径。本机没有镜像时从备份源码构建，需要网络。恢复后：

- 同步和删除原片关闭，不自动启动服务。
- 任务 ID、目的地身份、归档路径和完整缓存保留。
- 旧控制卡、待执行操作和历史待发通知取消，不补发旧完成通知。

停止旧实例，在新项目执行 `docker compose up -d --no-build`，检查状态后再开启同步。恢复目录不含 `.git`，统一在线更新命令暂不适用于该目录；保留备份中的对应源码和镜像信息，不以新空仓库替换任务数据。

## 完整缓存恢复

完整缓存的 `.receipts/` 清单保存素材身份、原名、目的地和发布依据。清单持久化后才允许自动删除原片；归档及缓存清理完成后一并移除。

后台约每 5 分钟保存 SQLite 一致快照到任务目录 `recovery/`，保留最近三份。这些快照包含未完成事项和必要去重信息，不含视频和 `.env`。

### Docker 恢复工具

数据库不可用时先停止两个服务，再输出到数据目录下的**新目录**：

```sh
docker compose stop
docker compose run --rm --no-deps backup recover-cache --output /data/recovered-YYYYMMDD
```

命令只读原缓存，不上传、不删除素材、不替换原数据库。检查生成的 `recovery-report.json`：通过核验的文件保留原任务 ID、归档路径与发布依据；无法确认的列入 `rejected` 并保留，不按同名同大小接管。

启用恢复结果时：

1. 保留原项目与数据作为回滚依据。
2. 将 `.env` 中任务与配置路径分别指向恢复目录下的 `data`、`config`，使用宿主机绝对路径。
3. 缓存路径仍指向原完整缓存；连接和凭据保持原值。
4. `docker compose up -d --no-build` 后检查状态，再开启同步。恢复后的同步、删除原片均默认关闭。

只需取出素材时，可将核验通过的文件复制到新目录，按设备平铺、同名加后缀，原缓存不动：

```sh
docker compose run --rm --no-deps backup export-cache --output /data/exported-YYYYMMDD
```

原生公开 CLI 暂未提供 `recover-cache` / `export-cache` 命令。原生实例发生此类故障时先停服务并备份全部数据，保留恢复清单与快照，不直接套用容器路径或让另一套服务接管。用于底层排查的核心工具应在隔离副本中运行。

## 从 v0.9 迁移

本节仅适用于旧版 Docker 数据格式，不是当前安装或常规更新步骤。

先停止服务，并保存旧源码、镜像、`.env`、配置、任务与完整缓存备份。取得新版源码后，在原缓存挂载可用的条件下执行：

```sh
docker compose stop
python3 scripts/build.py --activate
docker compose run --rm -T --entrypoint python3 backup /app/migrate_v010.py
docker compose up -d --no-build
```

一次性脚本先保存旧 SQLite、JSON 和 `.env`，再迁移到 JSON 4 / SQLite 5，保留任务 ID、归档路径和完整缓存。旧半文件登记清理；旧操作和待发通知取消。迁移后同步、删除原片关闭，其余有效设置保留。

回滚需停止新版，恢复相匹配的程序、配置与数据库；新版已经归档或删除素材后，不能直接覆盖回旧数据库。当前程序不保留两套旧配置运行逻辑。
