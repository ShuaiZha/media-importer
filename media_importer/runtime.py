import logging
import threading

from .adapters import create_source, create_destination, probe_source
from .config import Config
from .model import BackupError, Cancelled, now
from .settings import Settings
from .store import Store


class Reconfigure(BackupError):
    pass


class SyncDisabled(BackupError):
    pass


class Control:
    def __init__(self, settings, stop):
        self.settings, self.stop = settings, stop
        self.transfer_stop = threading.Event()
        self.changed = threading.Event()
        self.lock = threading.RLock()
        self.item, self.token, self.error = None, None, None
        self.applied_token, self.applied_revision = None, None
        self.refresh()

    def refresh(self):
        with self.lock:
            previous = (self.token, self.error, self.item["revision"] if self.item else None)
            try:
                self.item, self.token = self.settings.token()
                self.error = self.item["error"]
            except (OSError, ValueError) as exc:
                self.error = str(exc)
            if self.stop.is_set() or self.error or not self.enabled():
                self.transfer_stop.set()
            if self.token == self.applied_token and self.item:
                self.applied_revision = self.item["revision"]
            if previous != (self.token, self.error, self.item["revision"] if self.item else None):
                self.changed.set()

    def enabled(self):
        return bool(not self.error and self.item and self.item["config"] and self.item["config"]["backup"]["enabled"])

    def allowed(self, policy=None):
        self.refresh()
        with self.lock:
            return self.enabled() and (not policy or self.item["config"]["backup"][policy])

    def boundary(self):
        self.refresh()
        if self.error:
            raise SyncDisabled("配置无效，已停止同步与清理：" + self.error)
        if not self.enabled():
            raise SyncDisabled("同步已关闭，任务与缓存已保留")
        if self.token != self.applied_token:
            raise Reconfigure("正在应用已保存的配置")

    def adopt(self):
        self.refresh()
        with self.lock:
            if self.error:
                return None
            self.applied_token, self.applied_revision = self.token, self.item["revision"]
            if self.enabled() and not self.stop.is_set():
                self.transfer_stop.clear()
            self.changed.clear()
            return Config.from_values(self.item["config"])

    def state(self):
        with self.lock:
            return {"saved_revision": self.item["revision"] if self.item else None,
                    "applied_revision": self.applied_revision,
                    "pending": self.token != self.applied_token,
                    "error": self.error, "sync_enabled": self.enabled()}


def daemon(config_path, directory, stop):
    from .service import Service
    control = Control(Settings(config_path), stop)
    store = Store(directory)
    from .storage import initialize_cache
    initialize_cache(store)
    store.invalidate_inventory("服务重启后需重新核对相机清单")
    store.status(source_work=False)
    store.set_meta("service", {"running": True, "started_at": now()})
    def observe():
        db = Store(directory)
        try:
            while not stop.is_set():
                control.refresh()
                db.status(worker_heartbeat_at=now(), configuration=control.state())
                stop.wait(1)
        finally:
            control.transfer_stop.set()
            control.changed.set()
            db.close()
    watcher = threading.Thread(target=observe, daemon=True, name="settings-and-heartbeat")
    watcher.start()
    def notify():
        from .notifications import NotificationWorker
        try:
            NotificationWorker(directory, control.settings, stop).run()
        except Exception:
            # Notifications are optional: a failure must not end a transfer.
            # Do not log exception values, which could contain credentials.
            logging.error("通知工作线程暂不可用；传输服务继续运行")
    notifier = threading.Thread(target=notify, daemon=True, name="notifications")
    notifier.start()
    try:
        from .preflight import run as startup_check
        checked = startup_check(control.settings, store, stop)
        if checked["checks"]["database"]["state"] != "ok":
            raise BackupError("启动自检发现任务数据库异常，已停止传输；请保留数据并检查备份")
        while not stop.is_set():
            config = control.adopt()
            if config is None:
                store.status(phase="invalid_config", message="配置无效，已停止同步与清理", configuration=control.state())
                stop.wait(1)
                continue
            source, destination = create_source(config, control.transfer_stop), create_destination(config, control.transfer_stop)
            try:
                service = Service(config, store, source, destination, stop, control=control)
                store.status(configuration=control.state(), active_destination=config.destination_id,
                             active_serial=config.source["serial"], active_source_type=config.source["type"])
                service.recover_interrupted()
                with service.monitoring():
                    while not stop.is_set():
                        control.refresh()
                        if control.token != control.applied_token or control.error:
                            break
                        control.adopt()
                        service.clean_temporaries()
                        try:
                            result = service.safe_cycle()
                        except Cancelled:
                            if stop.is_set():
                                break
                            result = {"complete": False, "reason": "settings_changed"}
                            store.status(phase="paused", message="同步已停止，任务与缓存已保留",
                                         speed_bytes_per_second=0, eta_seconds=None)
                        control.refresh()
                        if control.token != control.applied_token or control.error:
                            break
                        delay = config.backup["retry_seconds"] if result.get("reason") not in (None, "paused") else config.backup["poll_seconds"]
                        control.changed.wait(delay)
                        if control.token != control.applied_token or control.error:
                            break
            finally:
                destination.close()
    finally:
        stop.set()
        control.changed.set()
        watcher.join(timeout=3)
        notifier.join(timeout=35)
        store.set_meta("service", {"running": False, "stopped_at": now()})
        store.status(phase="stopped", message="服务已停止，任务与缓存已保留", speed_bytes_per_second=0, eta_seconds=None)
        store.close()
