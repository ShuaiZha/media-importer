import logging
import threading
import time
from pathlib import Path

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
        self.work_guard = threading.Lock()
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
            return self.enabled() and (not policy or (not self.draining() and self.item["config"]["backup"][policy]))

    def draining(self):
        return bool(self.enabled() and self.item["config"]["backup"].get("stop_after_current"))

    def finish_stop(self):
        changed = self.settings.finish_stop()
        self.refresh()
        return changed

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
                    "error": self.error, "sync_enabled": self.enabled(), "stop_after_current": self.draining()}


def daemon(config_path, directory, stop):
    from .service import Service
    control = Control(Settings(config_path), stop)
    store = Store(directory)
    from .storage import initialize_cache
    initialize_cache(store)
    store.invalidate_inventory("服务重启后需重新核对相机清单")
    store.status(source_work=False)
    store.set_meta("service", {"running": True, "started_at": now()})
    from .workers import Workers
    failed = threading.Event()
    def fatal():
        failed.set()
        stop.set()
        control.transfer_stop.set()
        control.changed.set()
    workers = Workers(stop, fatal)
    def observe():
        from .wireless_control import publish, connection_status
        db = Store(directory)
        heartbeat_at = 0
        try:
            while not stop.is_set():
                control.refresh()
                updates = {'configuration': control.state(), 'workers': workers.snapshot()}
                if time.monotonic() - heartbeat_at >= 5:
                    updates['worker_heartbeat_at'] = now()
                    heartbeat_at = time.monotonic()
                db.status(**updates)
                config = Config.from_values(control.item["config"]) if control.item and control.item.get("config") and not control.error else None
                try:
                    if config and config.source.get("transport") == "wifi":
                        publish(directory, config, control.enabled() and not stop.is_set(), db.get_meta("status", {}))
                        db.status(wireless=connection_status(directory, config.source["serial"]))
                    elif (Path(directory) / "wireless/control.json").exists():
                        publish(directory, config, False, {})
                except OSError:
                    # Let the lease expire; a radio helper must not keep acting
                    # when the original data disk becomes inaccessible.
                    logging.warning("无线控制状态暂不可写，请检查数据盘")
                stop.wait(1)
        finally:
            control.transfer_stop.set()
            control.changed.set()
            try:
                if (Path(directory) / "wireless/control.json").exists():
                    publish(directory, None, False, {})
            except OSError:
                pass
            db.close()
    watcher = workers.start('settings-and-heartbeat', observe)
    def notify():
        from .notifications import NotificationWorker
        NotificationWorker(directory, control.settings, stop).run()
    notifier = workers.start('notifications', notify, critical=False)
    from .operations import serve as maintenance
    maintainer = workers.start('maintenance', lambda: maintenance(directory, control, stop), critical=False)
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
                if store.get_meta("status", {}).get("active_transport", config.source["transport"]) != config.source["transport"]:
                    store.invalidate_inventory("连接方式已切换，需重新核对相机身份与清单")
                    store.status(source={"online": None, "transport": config.source["transport"]})
                store.status(configuration=control.state(), active_destination=service.destination_id,
                             active_serial=config.source["serial"], active_source_type=config.source["type"],
                             active_transport=config.source["transport"])
                with control.work_guard:
                    service.recover_interrupted()
                with service.monitoring():
                    while not stop.is_set():
                        control.refresh()
                        if control.token != control.applied_token or control.error:
                            break
                        control.adopt()
                        try:
                            with control.work_guard:
                                service.clean_temporaries()
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
        maintainer.join(timeout=35)
        store.set_meta("service", {"running": False, "stopped_at": now()})
        store.status(phase="stopped", message="服务已停止，任务与缓存已保留", speed_bytes_per_second=0, eta_seconds=None)
        store.close()
    if failed.is_set():
        raise BackupError('关键后台线程持续失败，退出服务以便 Docker 自动重启；任务与完整缓存保留')
