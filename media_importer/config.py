"""Runtime preferences and checked connection overrides; passwords stay in local files."""
import json
import math
from dataclasses import dataclass
from pathlib import Path
from .model import ConfigError
from .adapters import validate_source, validate_destination, destination_identity
from .media import MEDIA_SCOPES

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"重复配置项: {key}")
        result[key] = value
    return result



def _merge(raw, defaults, prefix=""):
    if not isinstance(raw, dict):
        raise ConfigError(f"{prefix or '配置'} 必须是对象")
    unknown = set(raw) - set(defaults)
    if unknown:
        raise ConfigError(f"未知配置项: {prefix}{', '.join(sorted(unknown))}")
    result = {}
    for key, default in defaults.items():
        value = raw.get(key, default)
        name = prefix + key
        if isinstance(default, dict):
            value = _merge(value, default, name + ".")
        elif isinstance(default, bool):
            if type(value) is not bool:
                raise ConfigError(f"{name} 必须是 true 或 false")
        elif isinstance(default, (int, float)):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ConfigError(f"{name} 必须是有限的非负数")
        elif not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
            raise ConfigError(f"{name} 必须是不含换行的字符串")
        result[key] = value
    return result



DEFAULTS = {
    "version": 4,
    "backup": {"enabled": False, "delete_source_after_import": False, "stop_after_current": False,
               "media_scope": "all", "source_transport": "usb", "archive_previews": False,
               "poll_seconds": 10, "retry_seconds": 30, "stability_seconds": 2},
    "limits": {"upload_mib_per_second": 0, "min_free_disk_gib": 10, "max_cache_gib": 50},
    "notifications": {"enabled": False, "connection_events": True, "progress_interval_seconds": 300, "keyword": ""},
}


def runtime_config(raw):
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 4:
        raise ConfigError("settings.json 必须使用 version: 4；请先停止服务并运行一次性迁移")
    values = _merge({k: v for k, v in raw.items() if k not in ("archive", "source_options")}, DEFAULTS)
    if "source_options" in raw:
        options = _merge(raw["source_options"],
                         {"url": "", "serial": "", "timeout_seconds": 20, "request_attempts": 3},
                         "source_options.")
        values["source_options"] = options
    if "archive" in raw:
        archive = raw["archive"]
        required = {"host", "port", "share", "directory", "id", "change_id"}
        optional = {"name": "", "username": "", "domain": "", "credential_id": ""}
        if (not isinstance(archive, dict) or not required.issubset(archive)
                or set(archive) - required - set(optional)):
            raise ConfigError("archive 格式无效，请通过飞书重新配置归档位置")
        archive = _merge(archive, {"host": "", "port": 445, "share": "", "directory": "", "id": "", "change_id": "",
                                  **{key: default for key, default in optional.items() if key in archive}}, "archive.")
        if len(archive["change_id"]) != 32 or any(c not in "0123456789abcdef" for c in archive["change_id"]):
            raise ConfigError("archive.change_id 无效")
        if not (archive["id"] and len(archive["id"]) <= 2048):
            raise ConfigError("archive.id 无效，请通过飞书重新配置归档位置")
        if any(ord(c) < 32 for value in archive.values() if isinstance(value, str) for c in value):
            raise ConfigError("归档位置不能包含控制字符")
        if "credential_id" in archive:
            from .credentials import credential_path
            credential_path(".", archive["credential_id"])
        values["archive"] = archive
    if values["backup"]["media_scope"] not in MEDIA_SCOPES:
        raise ConfigError("backup.media_scope 必须为 all、video 或 photo")
    if values["backup"]["source_transport"] != "usb":
        raise ConfigError("当前仅支持 USB；请将 backup.source_transport 设为 usb")
    for key in ("poll_seconds", "retry_seconds"):
        if values["backup"][key] < 1:
            raise ConfigError(f"backup.{key} 必须至少为 1")
    interval = values["notifications"]["progress_interval_seconds"]
    if type(interval) is not int or not 60 <= interval <= 86400:
        raise ConfigError("进度通知间隔必须为 60 到 86400 的整数秒数")
    keyword = values["notifications"]["keyword"]
    if len(keyword) > 100 or any(ord(c) < 32 for c in keyword):
        raise ConfigError("通知前缀最多 100 字符，不能包含控制字符")
    return values


def preferences(values):
    return {key: values[key] for key in (*DEFAULTS, "archive", "source_options") if key in values}


@dataclass(frozen=True)
class Config:
    values: dict

    @classmethod
    def load(cls, path):
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"无法读取 JSON 配置: {exc}") from exc
        return cls.from_dict(raw, Path(path).parent)

    @classmethod
    def from_dict(cls, raw, config_directory=None):
        from .deployment import connections
        data = {**runtime_config(raw), **connections()}
        if "archive" in data:
            data["destination"].update({k: data["archive"][k] for k in
                                       ("host", "port", "share", "directory", "name", "username", "domain")
                                       if k in data["archive"]})
            data["destination"]["archive_id"] = data["archive"]["id"]
            if "credential_id" in data["archive"]:
                from .credentials import credential_path
                if config_directory is None:
                    import os
                    config_directory = Path(os.environ.get("MEDIA_IMPORTER_CONFIG", "/config/settings.json")).parent
                data["destination"]["credential_file"] = str(credential_path(config_directory, data["archive"]["credential_id"]))
        if "source_options" in data:
            data["source"].update(data["source_options"])
        # Existing installations retain their .env source choice until the first
        # saved preference. Initial connection values remain in .env;
        # checked overrides and credential references live in settings.
        data["source"]["transport"] = raw.get("backup", {}).get("source_transport", data["source"]["transport"])
        return cls.from_values(data)

    @classmethod
    def from_values(cls, values):
        # Internal construction only; serialized settings exclude credentials.
        data = runtime_config(preferences(values))
        data.update(source=dict(values["source"]), destination=dict(values["destination"]))
        validate_source(data["source"])
        data["backup"]["source_transport"] = data["source"]["transport"]
        validate_destination(data["destination"])
        return cls(data)

    @property
    def source(self):
        return self.values["source"]

    @property
    def destination(self):
        return self.values["destination"]

    @property
    def backup(self):
        return self.values["backup"]

    @property
    def limits(self):
        return self.values["limits"]

    @property
    def destination_id(self):
        return self.destination.get('archive_id') or destination_identity(self.destination)
