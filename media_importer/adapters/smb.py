"""SMB-only archive adapter. One client implementation, no subprocesses or byte resume."""
import errno
import json
import tempfile
import threading
import uuid
from pathlib import Path, PurePosixPath

from .smb_transport import SmbSession
from ..paths import safe_relative
from ..model import DestinationError


def defaults():
    return {"type": "smb", "name": "SMB 存储", "host": "", "port": 445, "share": "", "directory": "media",
            "username": "", "domain": "WORKGROUP"}


def validate(values):
    from ..config import ConfigError
    for key in ("host", "share", "username"):
        if not values.get(key):
            raise ConfigError(f"destination.{key} 不能为空")
    if "/" in values["host"] or "\\" in values["host"]:
        raise ConfigError("destination.host 只填写地址，不含协议或路径")
    if type(values["port"]) is not int or not 1 <= values["port"] <= 65535:
        raise ConfigError("destination.port 必须是 1–65535 的整数")
    values["share"] = safe_relative(values["share"])
    if "/" in values["share"]:
        raise ConfigError("destination.share 只填写共享名")
    values["directory"] = safe_relative(values["directory"], allow_empty=True)


def identity(values):
    # This encoding deliberately stays identical so existing archive records keep their identity.
    return json.dumps({k: values[k] for k in ("host", "port", "share", "directory", "username", "domain")}, sort_keys=True)


class SmbDestination:
    def __init__(self, config, stop=None):
        self.config, self.stop = config, stop
        self._bound = None

    def fork(self, stop=None):
        return type(self)(self.config, self.stop if stop is None else stop)

    def close(self):
        pass  # A bound session closes with the file; unbound calls close themselves.

    def _connect(self):
        from ..deployment import destination_password
        from ..model import ConfigError
        try:
            password = destination_password(self.config.destination)
        except ConfigError as exc:
            raise DestinationError(str(exc), code='authentication') from None
        session = SmbSession(self.config, password, self.stop)
        session.on_activity = getattr(self, 'on_activity', None)
        return session

    def session(self):
        if self._bound is not None:
            self._bound.on_activity = getattr(self, 'on_activity', None)
            self._bound.stop = self.stop
            return self._bound
        return self._connect()

    def bound(self):
        from contextlib import contextmanager
        @contextmanager
        def scope():
            if self._bound is not None:
                yield
                return
            session = self._connect()
            self._bound = session
            try:
                with session.hold():
                    yield
            finally:
                self._bound = None
        return scope()

    def redact(self, text):
        from ..deployment import destination_password
        from ..model import ConfigError
        try:
            return text.replace(destination_password(self.config.destination), "***")
        except ConfigError:
            return text

    def remote(self, relative=""):
        root = PurePosixPath(self.config.destination["share"]) / self.config.destination["directory"]
        return "smb://" + self.config.destination["host"] + ":" + str(self.config.destination["port"]) + "/" + str(root / safe_relative(relative, allow_empty=True))

    def stat(self, relative):
        return self.session().stat(relative)

    def file_identity(self, relative):
        item = self.stat(relative)
        return item["identity"] if item and not item["is_directory"] else None

    def list_names(self, relative):
        return self.session().list_names(relative)

    def check(self):
        item = self.stat("")
        if not item or not item["is_directory"]:
            raise DestinationError("SMB 目标共享或目录不存在，请检查路径与访问权限")
        return {"host": self.config.destination["host"], "target": self.remote(), "name": self.config.destination["name"]}

    def upload(self, local, temporary, progress, created, staged, content_sha256=None):
        return self.session().upload(local, temporary, progress, created, staged, content_sha256)

    def publish(self, temporary, final):
        return self.session().publish(temporary, final)

    def delete_owned(self, relative, identity):
        return self.session().delete_owned(relative, identity)

    def check_write(self):
        with self.bound():
            return self._check_write()

    def _check_write(self):
        result = self.check()
        token = uuid.uuid4().hex
        temporary, final = f".media-import-check-{token}.tmp", f".media-import-check-{token}.done"
        owned = []
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "check.data"
            local.write_bytes(b"Media importer SMB write check " + token.encode())
            try:
                self.upload(local, temporary, lambda *_: None, lambda value: owned.append(value), lambda *_: None)
                if self.stat(temporary)["size"] != local.stat().st_size:
                    raise DestinationError("SMB 测试文件大小不一致")
                self.publish(temporary, final)
                if self.file_identity(final) != owned[0]:
                    raise DestinationError("SMB 测试改名结果无法确认")
            finally:
                if owned:
                    for path in (temporary, final):
                        self.delete_owned(path, owned[0])
        return {**result, "writable": True, "rename": True, "test_files_removed": True}


def create(config, stop=None):
    return SmbDestination(config, stop)
