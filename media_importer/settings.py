"""Atomic, versioned settings shared by controls and transfer workers."""
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import Config, ConfigError, _unique_object, preferences, runtime_config
from .store import fsync_directory


class Conflict(ConfigError):
    pass


def revision(text):
    return uuid.uuid5(uuid.NAMESPACE_URL, text).hex


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".media-import-", dir=str(path.parent))
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Settings:
    def __init__(self, path, directory=None):
        self.path = Path(path)
        self.directory = Path(directory or os.environ.get('MEDIA_IMPORTER_DATA_DIR', '/data'))

    @contextmanager
    def lock(self):
        from .locking import file_lock
        with file_lock(self.path.with_suffix('.lock')):
            yield

    def read(self):
        text = self.path.read_text(encoding="utf-8")
        result = {"document": text, "revision": revision(text), "config": None, "error": None}
        try:
            result["config"] = Config.from_dict(json.loads(text, object_pairs_hook=_unique_object), self.path.parent).values
            result['source_hint'] = dict(result['config']['source'])
            from .source_connection import selected
            result['config']['source'] = selected(self.directory, result['config']['source'])
        except (ConfigError, ValueError) as exc:
            result["error"] = str(exc)
        return result

    @staticmethod
    def notification_credentials():
        from .deployment import notification_credentials
        return notification_credentials()

    def set_enabled(self, enabled):
        return self.update_preferences({"backup": {"enabled": enabled}})

    def update_preferences(self, changes, expected=None):
        if isinstance(changes, dict) and set(changes) == {'preview'}:
            from .preview_settings import save
            return save(self, changes['preview'], expected)
        allowed = {"backup": {"enabled", "delete_source_after_import", "stop_after_current", "media_scope", "source_transport", "archive_previews",
                              "poll_seconds", "retry_seconds", "stability_seconds"},
                   "limits": {"upload_mib_per_second", "min_free_disk_gib", "max_cache_gib"},
                   "notifications": {"enabled", "connection_events", "progress_interval_seconds", "keyword"},
                   "source_options": {"url", "serial", "timeout_seconds", "request_attempts"}}
        if not isinstance(changes, dict) or not changes:
            raise ConfigError("未提供要保存的设置")
        for group, updates in changes.items():
            if group not in allowed or not isinstance(updates, dict) or set(updates) - allowed[group]:
                raise ConfigError("此入口不支持该参数，请通过对应设置卡片操作")
        with self.lock():
            item = self.read()
            if item["error"]:
                raise ConfigError(item["error"])
            if expected is not None and item["revision"] != expected:
                raise Conflict("设置已经变化，请获取新的设置卡片后再操作")
            values = preferences(item["config"])
            if changes.get("backup", {}).get("stop_after_current") and not values["backup"]["enabled"]:
                raise ConfigError("同步已经停止，无需等待当前文件")
            if "source_options" in changes:
                from .archive_settings import _stopped
                from .store import Store
                if not (self.directory / 'state.sqlite3').is_file():
                    raise ConfigError("本机任务目录不可访问，请恢复原磁盘后再修改相机连接")
                store = Store(self.directory)
                try:
                    _stopped(store, item)
                finally:
                    store.close()
                values.setdefault("source_options", {key: item['source_hint'][key] for key in allowed["source_options"]})
            previous_transport = values["backup"]["source_transport"]
            for group, updates in changes.items():
                values[group].update(updates)
            if values["backup"]["source_transport"] != previous_transport and values["backup"]["enabled"]:
                raise ConfigError("请先从「同步 → 停止同步」停止传输，再切换连接方式；任务与完整缓存保留")
            if "enabled" in changes.get("backup", {}) or not values["backup"]["enabled"]:
                values["backup"]["stop_after_current"] = False
            checked = runtime_config(values)
            Config.from_dict(checked, self.path.parent)  # Validate adapter fields before saving.
            atomic_write(self.path, json.dumps(checked, ensure_ascii=False, indent=2) + "\n")
            return self.read()

    def finish_stop(self):
        """Finish a drain only if it has not been cancelled by a newer start."""
        with self.lock():
            item = self.read()
            if item["error"] or not item["config"]["backup"]["stop_after_current"]:
                return False
            values = preferences(item["config"])
            values["backup"].update(enabled=False, stop_after_current=False)
            atomic_write(self.path, json.dumps(runtime_config(values), ensure_ascii=False, indent=2) + "\n")
            return True

    def token(self):
        item = self.read()
        structural = json.loads(json.dumps(item["config"]))
        if structural:
            structural.pop("notifications", None)
            structural.pop("preview", None)
            for key in ("enabled", "delete_source_after_import", "stop_after_current"):
                structural["backup"].pop(key, None)
        return item, revision(json.dumps(structural, sort_keys=True))
