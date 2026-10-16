"""Bounded startup checks, without transferring media or sending messages."""
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from .adapters import create_source, create_destination
from .config import Config
from .deployment import destination_password
from .issues import describe
from .model import BackupError, ConfigError, now
from .storage import inspect_cache


def item(state, message):
    return {"state": state, "message": message, "checked_at": now()}


def run(settings, store, stop=None):
    """Offline peers are waiting conditions, never a global transfer gate."""
    stop = stop or threading.Event()
    result = {"started_at": now(), "state": "checking", "checks": {}}
    store.set_meta("preflight", result)
    checks = result["checks"]
    try:
        config = Config.from_values(settings.read()["config"])
        destination_password()
        checks["configuration"] = item("ok", "连接配置与日常设置有效")
    except (ConfigError, OSError, ValueError, TypeError):
        checks["configuration"] = item("error", "检查 .env 和 settings.json；配置内容不会写入日志")
        config = None
    try:
        healthy = [r[0] for r in store.db.execute("PRAGMA quick_check")] == ["ok"]
        checks["database"] = item("ok" if healthy else "error", "任务数据库检查通过" if healthy else "任务数据库异常，请保留原文件并从备份恢复")
    except Exception:
        checks["database"] = item("error", "任务数据库无法读取，请检查数据盘和数据库")
    cache = inspect_cache(store.cache, store.get_meta("cache_identity"))
    if cache["available"]:
        try:
            # Only this uniquely owned, tiny probe is removed. No media is touched.
            with tempfile.NamedTemporaryFile(prefix=".startup-check-", dir=store.cache) as probe:
                probe.write(b"media-importer\n")
                probe.flush()
                os.fsync(probe.fileno())
            reserve = (config.limits["min_free_disk_gib"] * 1024 ** 3) if config else 0
            checks["cache"] = item("waiting" if cache["free_bytes"] < reserve else "ok",
                "缓存盘可读写，但低于磁盘预留；暂停新导入，完整缓存仍可上传" if cache["free_bytes"] < reserve else "缓存盘身份、挂载与实际写入检查通过")
        except OSError:
            checks["cache"] = item("error", "缓存盘写入失败，请检查挂载、权限和可用空间；不会改用内置盘")
    else:
        checks["cache"] = item("waiting", cache["reason"])

    def source_check():
        source = create_source(config, stop)
        source.identify()  # The adapter verifies the bound serial number.
        if hasattr(source, "telemetry"):
            reason = source.idle_reason(source.telemetry())
            if reason:
                return item("waiting", "相机身份正确，等待相机就绪：" + reason)
        return item("ok", "相机可访问，绑定身份核对通过")

    def destination_check():
        remote = create_destination(config, stop)
        try:
            remote.check()
            return item("ok", "SMB 认证和目标目录访问通过；写入与改名权限在实际上传时确认")
        finally:
            remote.close()

    def notification_check():
        from .adapters.feishu import check_credentials
        check_credentials(settings.notification_credentials())
        return item("ok", "飞书应用认证通过；接收人和发消息权限以实际发送结果为准")

    operations = {"source": source_check, "destination": destination_check} if config else {}
    operations["notification"] = notification_check
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="startup-check") as executor:
        futures = {name: executor.submit(operation) for name, operation in operations.items()}
        for name, future in futures.items():
            try:
                checks[name] = future.result()
            except Exception as exc:
                if name == "notification":
                    from .notifications import _safe_failure
                    checks[name] = item("waiting" if getattr(exc, "code", "") == "notification_network" else "error", _safe_failure(exc)[1])
                elif name == "source":
                    connection = "Mac 是否已连接相机 Wi-Fi" if config and config.source.get("transport") == "wifi" else "USB 网络"
                    checks[name] = item("waiting", f"相机不可访问或身份不符；检查开机、{connection}与绑定序列号，完整缓存可继续上传")
                else:
                    issue = describe(exc, "upload")
                    checks[name] = item("waiting" if issue["code"] == "network" else "error", issue["action"])
    result.update(finished_at=now(), state="error" if any(c["state"] == "error" for c in checks.values()) else
                  "waiting" if any(c["state"] != "ok" for c in checks.values()) else "ok")
    store.set_meta("preflight", result)
    for name, check in checks.items():
        logging.info("启动自检 %s [%s]: %s", name, check["state"], check["message"])
    return result
