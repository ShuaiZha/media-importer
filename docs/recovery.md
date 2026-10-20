# 迁移与恢复

[文档目录](README.md) · [配置参考](configuration.md)

迁移包含 **`.env`、配置、任务数据、完整缓存**。保留隐藏文件、卷标识、`.receipts/` 与修改时间；未归档缓存可能是唯一副本。源码克隆和更新时的配置快照都不能替代完整备份。

使用飞书更新 SMB 密码后，配置目录中的 `credentials/` 也必须保留，不能只复制 `settings.json`。程序备份和更新快照已包含这些凭据；普通参数与密码引用可随配置整体迁移。

## 原生实例

### 备份

1. 用 `media-importer status` 确认项目、配置、任务和缓存路径。
2. `media-importer stop`，确认后台停止。
3. 复制项目 `.env`、整个配置目录、整个任务目录、整个缓存目录，保留时间与隐藏文件；另存安装目录中的 `installation.json` 用于核对版本和路径。
4. 核对副本后，原电脑继续使用时执行 `media-importer start`。

任务目录整体复制，避免遗漏 SQLite 日志。备份含凭据，应私下保存；放到另一块磁盘才能应对原盘损坏。

### 更换电脑或目录

1. 完成备份，在旧实例执行普通卸载 `media-importer uninstall` 释放服务与数据归属，**不加 `--purge`**。
2. 从已释放归属的位置复制项目配置与三个数据目录到新位置；不要直接使用含旧部署登记的卸载前快照。
3. 修改新 `.env` 的路径，将 JSON 中 `enabled`、`delete_source_after_import`、`stop_after_current` 均设为 `false`。
4. 下载对应平台原生包，进入解压后的 `media-importer` 目录，执行 `./media-importer install --project /path/to/project`；Windows 使用 `./media-importer.exe`。
5. 检查 `status`、`check` 和飞书状态，再确认同步。

旧实例保持停止，同一份数据和飞书应用只运行一个实例。旧电脑不可用或登记未释放时，保留所有副本排查，不删除登记强行接管。

## Docker 实例

以下命令在项目目录执行，宿主机需 Python 3.9+ 与 Docker Compose；Windows 将 `python3` 换为 `py -3`。这些工具不能用于运行中的原生实例。

### 完整备份

```sh
python3 scripts/manage.py backup --output "/path/to/backups/backup-YYYYMMDD"
```

输出必须是尚不存在的新目录，不能位于缓存内。工具暂停两个服务，复制配置、凭据、SQLite 一致快照、源码与全部缓存，核验后恢复原运行状态。缓存复制不重新计算视频 SHA-256。

有效备份包含 `manifest.json`；中断留下的 `.incomplete-*` 不能恢复。完整复制需足够空间，备份中的凭据不能公开。

### 恢复

复制整个备份，在新电脑指定尚不存在且相互独立的项目和缓存目录：

```sh
python3 "/path/to/backup/project/scripts/manage.py" restore \
  --from "/path/to/backup" \
  --project "/path/to/media-importer" \
  --cache "/path/to/media-cache"
```

工具准备镜像、复制并核验数据、调整挂载路径；没有镜像时从备份源码构建，需要网络。保留任务 ID、目的地身份和归档路径；关闭同步与删除原片，取消旧控制请求与历史通知，不自动启动。

停止旧实例，在新项目 `docker compose up -d --no-build`，核对状态再启用同步。恢复目录不含 `.git`，不能直接使用 Git 在线更新器；保留对应源码与镜像信息，不用空项目替换运行数据。

## 完整缓存恢复

`.receipts/` 保存素材身份、原名、目的地和发布依据；清单持久化后才允许删除原片，归档及缓存清理结束后移除。后台约每 5 分钟向任务目录 `recovery/` 保存一致快照，保留最近三份；不含视频与凭据。

Docker 数据库不可用时，先停止服务，恢复到**新目录**：

```sh
docker compose stop
docker compose run --rm --no-deps backup recover-cache --output /data/recovered-YYYYMMDD
```

命令只读原缓存，不上传、不删除、不替换原数据库。检查 `recovery-report.json`：通过的保留原任务与发布依据，无法确认的进入 `rejected`，仍保留原文件。

启用结果前保留原数据，将 `.env` 的任务与配置目录指向恢复结果中的 `data/`、`config/`（宿主机绝对路径），缓存仍指向原目录。启动后核对状态；恢复的同步和删除原片均关闭。

仅提取素材可用以下命令，按设备平铺、同名加后缀，原缓存不动：

```sh
docker compose run --rm --no-deps backup export-cache --output /data/exported-YYYYMMDD
```

原生公开 CLI 暂不提供这两个恢复命令。原生故障先停止并备份全部数据，保留清单与快照；底层恢复在隔离副本中进行，不直接套用容器路径。

## 从 v0.9 迁移

仅限旧 Docker 数据格式。先停止服务，备份旧程序、镜像、配置、任务与完整缓存；取得新版源码后执行：

```sh
docker compose stop
python3 scripts/build.py --activate
docker compose run --rm -T --entrypoint python3 backup /app/migrate_v010.py
docker compose up -d --no-build
```

脚本保存旧配置与数据库，迁移到 JSON 4 / SQLite 5，保留任务和完整缓存，登记旧半文件清理，取消旧操作与通知；迁移后同步与删除原片关闭。

回滚必须使用匹配的程序、配置与数据库。新版已归档或删除素材后，不能直接覆盖回旧数据库。迁移与恢复不改动已有 SMB 归档。
