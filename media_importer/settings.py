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
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def lock(self):
        import fcntl
        with self.path.with_suffix(".lock").open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self):
        text = self.path.read_text(encoding="utf-8")
        result = {"document": text, "revision": revision(text), "config": None, "error": None}
        try:
            result["config"] = Config.from_dict(json.loads(text, object_pairs_hook=_unique_object)).values
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
        allowed = {"backup": {"enabled", "delete_source_after_import", "stop_after_current", "media_scope", "source_transport", "archive_previews"},
                   "limits": {"upload_mib_per_second", "min_free_disk_gib", "max_cache_gib"},
                   "notifications": {"enabled", "connection_events", "progress_interval_seconds", "keyword"}}
        if not isinstance(changes, dict) or not changes:
            raise ConfigError("未提供要保存的设置")
        for group, updates in changes.items():
            if group not in allowed or not isinstance(updates, dict) or set(updates) - allowed[group]:
                raise ConfigError("此入口只能修改日常设置；连接与凭据请编辑 .env")
        with self.lock():
            item = self.read()
            if item["error"]:
                raise ConfigError(item["error"])
            if expected is not None and item["revision"] != expected:
                raise Conflict("设置已经变化，请获取新的设置卡片后再操作")
            values = preferences(item["config"])
            previous_transport = values["backup"]["source_transport"]
            for group, updates in changes.items():
                values[group].update(updates)
            if values["backup"]["source_transport"] != previous_transport and values["backup"]["enabled"]:
                raise ConfigError("请先从「同步 → 停止同步」停止传输，再切换连接方式；任务与完整缓存保留")
            if "enabled" in changes.get("backup", {}) or not values["backup"]["enabled"]:
                values["backup"]["stop_after_current"] = False
            checked = runtime_config(values)
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
            for key in ("enabled", "delete_source_after_import", "stop_after_current"):
                structural["backup"].pop(key, None)
        return item, revision(json.dumps(structural, sort_keys=True))
