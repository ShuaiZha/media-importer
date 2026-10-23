import logging
import sqlite3
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
        self.source_interrupt = threading.Event()
        self.changed = threading.Event()
        from .camera_library import Library
        self.library = Library(self.changed.set)
        self.lock = threading.RLock()
        self.work_guard = threading.Lock()
        from .camera_coordinator import CameraCoordinator
        self.camera = CameraCoordinator(stop)
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
    control = Control(Settings(config_path, directory), stop)
    store = Store(directory)
    # Old cards stay invalid. The same camera does not need a new approval after restart.
    from .source_connection import restore, run as discover_sources
    item = control.settings.read()
    restore(store, (item.get('config') or {}).get('source') or {})
    from .live_preview import restore as restore_preview
    restore_preview(store)
    from .storage import initialize_cache
    initialize_cache(store)
    store.invalidate_inventory("服务重启后需重新核对相机清单")
    store.status(source_work=False, preview_work=False)
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
        db = Store(directory)
        heartbeat_at = 0
        source_session_id = None
        try:
            while not stop.is_set():
                control.refresh()
                updates = {'configuration': control.state(), 'workers': workers.snapshot()}
                if time.monotonic() - heartbeat_at >= 5:
                    updates['worker_heartbeat_at'] = now()
                    heartbeat_at = time.monotonic()
                db.status(**updates)
                from .capture import mutating_pending as capture_pending
                from .capture import pending as any_capture_pending
                from .live_preview import requested as preview_requested
                connection = db.get_meta('source_connection', {})
                session_id = connection.get('id')
                if capture_pending(db.db) or preview_requested(db) or (source_session_id and session_id != source_session_id):
                    control.source_interrupt.set()
                    control.changed.set()
                source_session_id = session_id
                from .camera_operations import pending
                from .selected_imports import pending as selection_pending
                if pending(db.db) or selection_pending(db.db) or any_capture_pending(db.db):
                    control.changed.set()
                stop.wait(1)
        finally:
            control.transfer_stop.set()
            control.changed.set()
            db.close()
    watcher = workers.start('settings-and-heartbeat', observe)
    discoverer = workers.start('source-connections', lambda: discover_sources(directory, control.settings, stop))
    def notify():
        from .notifications import NotificationWorker
        NotificationWorker(directory, control.settings, stop).run()
    notifier = workers.start('notifications', notify, critical=False)
    from .operations import serve as maintenance
    maintainer = workers.start('maintenance', lambda: maintenance(directory, stop), critical=False)
    from .preview_access import serve as preview_gateway
    gateway = workers.start('preview-access', lambda: preview_gateway(directory, stop, control), critical=False)
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
            from .source_session import SourceSession
            source = SourceSession(config, directory, control.transfer_stop)
            destination = create_destination(config, control.transfer_stop)
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
                    try:
                        service.settle()
                    except (BackupError, OSError, sqlite3.Error):
                        logging.warning('任务收尾暂未完成，原任务记录保留')
                with service.monitoring():
                    while not stop.is_set():
                        control.refresh()
                        if control.token != control.applied_token or control.error:
                            break
                        control.adopt()
                        try:
                            with control.work_guard:
                                from .camera_coordinator import dispatch
                                from .selected_imports import active
                                selected = dispatch(service, control.settings).selection
                                if selected or active(store.db):
                                    result = {"complete": False, "reason": "selected_import"}
                                else:
                                    service.clean_temporaries()
                                    result = service.safe_cycle()
                                try:
                                    if not selected and not active(store.db):
                                        service.settle()
                                except (BackupError, OSError, sqlite3.Error):
                                    logging.warning('任务收尾暂未完成，原任务记录保留')
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
        control.camera.close()
        gateway.join(timeout=5)
        watcher.join(timeout=3)
        discoverer.join(timeout=15)
        notifier.join(timeout=35)
        maintainer.join(timeout=35)
        store.set_meta("service", {"running": False, "stopped_at": now()})
        store.status(phase="stopped", message="服务已停止，任务与缓存已保留", speed_bytes_per_second=0, eta_seconds=None)
        store.close()
    if failed.is_set():
        raise BackupError('关键后台线程持续失败，退出服务以便 Docker 自动重启；任务与完整缓存保留')
