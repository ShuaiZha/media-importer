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
        allowed = {"backup": {"enabled", "delete_source_after_import"},
                   "limits": {"upload_mib_per_second", "min_free_disk_gib", "max_cache_gib"},
                   "notifications": {"enabled", "progress_interval_seconds", "keyword"}}
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
            for group, updates in changes.items():
                values[group].update(updates)
            checked = runtime_config(values)
            atomic_write(self.path, json.dumps(checked, ensure_ascii=False, indent=2) + "\n")
            return self.read()

    def token(self):
        item = self.read()
        structural = json.loads(json.dumps(item["config"]))
        if structural:
            structural.pop("notifications", None)
            for key in ("enabled", "delete_source_after_import"):
                structural["backup"].pop(key, None)
        return item, revision(json.dumps(structural, sort_keys=True))
