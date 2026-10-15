import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone



class ConfigError(ValueError):
    pass


class BackupError(RuntimeError):
    pass


class SourceError(BackupError):
    pass


class SourceBlocked(SourceError):
    """The source is reachable, but does not currently permit media reads."""


class CacheUnavailable(BackupError):
    pass


class DestinationError(BackupError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class NameConflict(DestinationError):
    """The destination name was taken; the uploaded temporary file is intact."""


class LocalCacheChanged(DestinationError):
    pass


class Cancelled(BackupError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Asset:
    serial: str
    path: str
    size: int
    created: str
    modified: str
    identity_key: str
    device_name: str = "Camera"
    source_type: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.identity_key, str) or not self.identity_key:
            raise ValueError("素材必须提供稳定身份")

    @property
    def identity(self):
        return self.identity_key

    @property
    def device_directory(self):
        return device_directory(self.device_name, self.serial)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


def device_directory(device_name, serial):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", device_name).strip(". ") or "Camera"
    serial = re.sub(r'[^\w.-]', "_", serial).strip(". ")
    return name + "-" + serial
