"""Bounded whole-file writes and identity-checked SMB operations; never read media back."""
import errno
from .smb_errors import SmbError
import logging
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..paths import safe_relative
from ..model import BackupError, Cancelled, DestinationError, NameConflict, LocalCacheChanged


def local_identity(info):
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "device": info.st_dev, "inode": info.st_ino}


def remote_identity(info):
    if not stat.S_ISREG(info.st_mode) or info.st_reparse_tag or not info.st_ino:
        raise DestinationError("SMB 临时文件不是可确认身份的普通文件")
    # SMB st_ctime is creation time, not POSIX metadata-change time.
    return {"volume": info.st_dev, "inode": info.st_ino, "created_ns": info.st_ctime_ns}


class SmbSession:
    block_size = 1024 * 1024
    operation_timeout = 30
    pause_timeout = 3

    def __init__(self, config, password, stop=None, client=None):
        if client is None:
            try:
                import smbclient
            except ImportError as exc:
                raise SmbError("缺少 SMB 客户端依赖，请更新 Docker 镜像或安装 requirements.txt") from exc
            client = smbclient
            for name in ("smbprotocol", "smbclient", "spnego"):
                logging.getLogger(name).setLevel(logging.WARNING)
        self.client, self.config, self.stop = client, config, stop
        destination = config.destination
        user = destination["username"]
        if destination["domain"] and "\\" not in user and "@" not in user:
            user = destination["domain"] + "\\" + user
        self.connections = {}
        self.options = {"username": user, "password": password, "port": destination["port"],
                        "connection_timeout": 10, "connection_cache": self.connections,
                        "auth_protocol": "ntlm"}
        self.call_started = None
        self.timed_out = False
        self.finished = threading.Event()

    def path(self, relative):
        parts = PurePosixPath(self.config.destination["share"]) / self.config.destination["directory"] / safe_relative(relative, allow_empty=True)
        return "\\\\" + self.config.destination["host"] + "\\" + str(parts).replace("/", "\\")

    def check_stop(self):
        if self.stop and self.stop.is_set():
            raise Cancelled("同步已暂停，未完成上传将清理，完整缓存保留")

    def disconnect(self):
        for connection in list(self.connections.values()):
            try:
                # No network logoff: this also releases a blocked write on pause/timeout.
                connection.disconnect(close=False)
            except Exception:
                pass

    def watchdog(self):
        paused_at = None
        while not self.finished.wait(.1):
            current = time.monotonic()
            if self.stop and self.stop.is_set():
                paused_at = paused_at or current
            if self.call_started is not None and current - self.call_started > self.operation_timeout:
                self.timed_out = True
                self.disconnect()
            elif paused_at is not None and current - paused_at > self.pause_timeout:
                self.disconnect()

    def call(self, function, *args, **kwargs):
        self.call_started = time.monotonic()
        try:
            result = function(*args, **kwargs)
            if self.timed_out:
                raise SmbError("SMB 操作超时")
            if getattr(self, 'on_activity', None):
                self.on_activity()
            return result
        finally:
            self.call_started = None

    @contextmanager
    def operation(self):
        self.check_stop()
        watcher = threading.Thread(target=self.watchdog, name="smb-write-watchdog", daemon=True)
        watcher.start()
        try:
            yield
        except BackupError:
            raise
        except Exception as exc:
            self.check_stop()
            if isinstance(exc, DestinationError):
                raise
            # Avoid exception text from auth libraries, which can include credentials.
            code = getattr(exc, "ntstatus", None)
            suffix = f" (NTSTATUS {code:#x})" if isinstance(code, int) else ""
            raise SmbError("SMB 操作失败，完整缓存已保留：" + type(exc).__name__ + suffix) from exc
        finally:
            self.finished.set()
            self.disconnect()
            watcher.join(timeout=3)
            self.options.pop("password", None)

    def upload(self, local, temporary, progress, created, staged):
        with self.operation():
            self._upload(Path(local), self.path(temporary), progress, created, staged)

    def publish(self, temporary, final):
        with self.operation():
            try:
                # smbclient.rename sends ReplaceIfExists=False; never use replace().
                self.call(self.client.rename, self.path(temporary), self.path(final), **self.options)
            except OSError as exc:
                if exc.errno == errno.EEXIST or getattr(exc, "ntstatus", None) == 0xC0000035:
                    raise NameConflict("归档文件名已被占用，将添加后缀后重试") from exc
                raise

    def stat(self, relative):
        with self.operation():
            try:
                info = self.call(self.client.stat, self.path(relative), follow_symlinks=False, **self.options)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return None
                raise
            directory = stat.S_ISDIR(info.st_mode)
            return {"size": info.st_size, "is_directory": directory,
                    "identity": None if directory else remote_identity(info)}

    def list_names(self, relative):
        with self.operation():
            try:
                names = self.call(self.client.listdir, self.path(relative), **self.options)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return []
                raise
            if any(not isinstance(n, str) or not n or "/" in n or "\\" in n or n in (".", "..") for n in names):
                raise DestinationError("SMB 返回无效目录项")
            return names

    def delete_owned(self, relative, expected):
        if not expected:
            raise DestinationError("缺少临时文件身份，保留文件等待核对", code="temporary_identity")
        from smbprotocol.open import CreateOptions, FilePipePrinterAccessMask
        from smbprotocol.file_info import FileDispositionInformation
        from smbclient._io import SMBFileTransaction, set_info
        with self.operation():
            try:
                # Deny writers and replacement while validating and deleting this very handle.
                handle = self.call(self.client.open_file, self.path(relative), mode="rb", buffering=0,
                    share_access="r", desired_access=FilePipePrinterAccessMask.DELETE | FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                    create_options=CreateOptions.FILE_NON_DIRECTORY_FILE | CreateOptions.FILE_OPEN_REPARSE_POINT,
                    **self.options)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return
                raise
            try:
                info = self.call(self.client.stat, self.path(relative), follow_symlinks=False, **self.options)
                if remote_identity(info) != expected:
                    raise DestinationError("临时文件身份已变化，未删除", code="temporary_identity")
                def dispose():
                    with SMBFileTransaction(handle) as transaction:
                        value = FileDispositionInformation()
                        value["delete_pending"] = True
                        set_info(transaction, value)
                self.call(dispose)
            finally:
                self.call(handle.close)

    def _upload(self, local, target, progress, created, staged):
        with local.open("rb") as source:
            original = local_identity(os.fstat(source.fileno()))
            total = original["size"]
            self.call(self.client.makedirs, target.rsplit("\\", 1)[0], exist_ok=True, **self.options)
            output = self.call(self.client.open_file, target, mode="xb", buffering=0, share_access="r", **self.options)
            try:
                remote = remote_identity(self.call(self.client.stat, target, follow_symlinks=False, **self.options))
                created(remote)  # Durable ownership before sending the first media byte.
                progress(0, total)
                done, started = 0, time.monotonic()
                rate = self.config.limits["upload_mib_per_second"] * 1024 * 1024
                while done < total:
                    self.check_stop()
                    block = source.read(min(self.block_size, total - done))
                    if not block:
                        raise LocalCacheChanged("本地缓存被截断，停止上传")
                    view = memoryview(block)
                    while view:
                        self.check_stop()
                        written = self.call(output.write, view)
                        if not isinstance(written, int) or not 0 < written <= len(view):
                            raise DestinationError("SMB 返回无效写入长度")
                        view = view[written:]
                        done += written
                    progress(done, total)
                    if rate:
                        deadline = started + done / rate
                        while time.monotonic() < deadline:
                            self.check_stop()
                            time.sleep(min(.1, max(0, deadline - time.monotonic())))
                self.check_stop()
                self.call(output.flush)
                info = self.call(self.client.stat, target, follow_symlinks=False, **self.options)
                if local_identity(os.fstat(source.fileno())) != original:
                    raise LocalCacheChanged("上传过程中本地缓存已变化，保留缓存并报告")
                if remote_identity(info) != remote or info.st_size != total:
                    raise DestinationError("SMB 临时文件身份或大小不一致")
                staged(remote)  # Complete-file record, not a resumable byte checkpoint.
            finally:
                self.call(output.close)
