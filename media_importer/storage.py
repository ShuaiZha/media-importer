"""Cache volume checks. Never create a missing externally mounted cache directory."""
import os
import hashlib
import shutil
import uuid
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .model import CacheUnavailable, now


MARKER = ".media-importer-cache-id"


def requires_mount():
    return os.environ.get("MEDIA_IMPORTER_REQUIRE_CACHE_MOUNT") == "1"


def mounted(path):
    target = str(Path(path).resolve())
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            point = line.split()[4]
            for escaped, literal in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                point = point.replace(escaped, literal)
            if point == target:
                return True
    except OSError:
        return False
    return False


def native_volume_available():
    volume = os.environ.get('MEDIA_IMPORTER_CACHE_VOLUME')
    return not volume or Path(volume).is_mount()


def inspect_cache(path, identity=None):
    path = Path(path)
    result = {"available": False, "path": str(path), "checked_at": now(), "free_bytes": None,
              "reason": None, "action": "重新连接缓存盘并检查配置路径；Docker 部署还需检查目录挂载。"}
    try:
        if not path.is_dir():
            raise CacheUnavailable("缓存目录不可访问")
        if not native_volume_available():
            raise CacheUnavailable('原生缓存盘未挂载，等待原磁盘')
        if requires_mount() and not mounted(path):
            raise CacheUnavailable("缓存磁盘未挂载，已阻止使用容器内的替代目录")
        if identity:
            marker = path / MARKER
            if marker.is_symlink() or marker.read_text().strip() != identity:
                raise CacheUnavailable("缓存盘身份不符，等待原缓存盘")
        elif requires_mount() or os.environ.get('MEDIA_IMPORTER_RUNTIME') == 'native':
            raise CacheUnavailable("缓存盘尚未完成身份登记，等待后台检查")
        # A read-only probe; transfer writes still handle permission/IO failures.
        with os.scandir(path) as entries:
            next(entries, None)
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise CacheUnavailable("缓存目录没有读写权限")
        result.update(available=True, free_bytes=shutil.disk_usage(path).free, action=None)
    except (OSError, CacheUnavailable) as exc:
        result["reason"] = "缓存盘不可用：" + str(exc)
    return result


def initialize_cache(store):
    """Register only the first, mounted volume; an existing binding is never replaced."""
    native = os.environ.get('MEDIA_IMPORTER_RUNTIME') == 'native'
    if (requires_mount() or native) and not store.get_meta("cache_identity"):
        try:
            if (requires_mount() and not mounted(store.cache)) or not store.cache.is_dir() or not native_volume_available():
                raise CacheUnavailable("缓存磁盘未挂载")
            marker = store.cache / MARKER
            if marker.is_symlink():
                raise CacheUnavailable("缓存盘标识不能是符号链接")
            if marker.exists():
                identity = marker.read_text().strip()
                uuid.UUID(hex=identity)
            else:
                identity = uuid.uuid4().hex
                with marker.open("x") as handle:
                    handle.write(identity + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                from .store import fsync_directory
                fsync_directory(store.cache)
            store.set_meta("cache_identity", identity)
        except (OSError, ValueError, CacheUnavailable) as exc:
            store.status(cache={"available": False, "reason": str(exc), "checked_at": now()})
    status = inspect_cache(store.cache, store.get_meta("cache_identity"))
    store.status(cache=status)
    return status


def require_cache(store):
    status = inspect_cache(store.cache, store.get_meta("cache_identity"))
    from .status import age
    previous = store.get_meta('status', {}).get('cache', {})
    if (age(previous.get('checked_at')) >= 5 or
            any(previous.get(k) != status.get(k) for k in ('available', 'reason', 'path'))):
        store.status(cache=status)
    if not status["available"]:
        raise CacheUnavailable(status["reason"])
    return status


def file_identity(path):
    import stat
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode):
        raise CacheUnavailable("缓存不是普通文件")
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "device": info.st_dev, "inode": info.st_ino}


def sample_ranges(size):
    """Bounded identity evidence, not a whole-file integrity checksum."""
    length = min(size, 64 * 1024)
    return [(offset, length) for offset in sorted({(size - length) * n // 4 for n in range(5)})] if size else []


def cache_evidence(path, asset_id, volume_id):
    # Docker Desktop may remap st_dev/st_ino after recreating a bind mount.
    # Bind the durable import record to its volume, asset, size, mtime and small
    # content samples instead. No full video readback or SHA-256 is performed.
    before = file_identity(path)
    digest = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as handle:
        for offset, length in sample_ranges(before["size"]):
            handle.seek(offset)
            data = handle.read(length)
            if len(data) != length:
                raise CacheUnavailable("缓存核验期间文件长度变化")
            digest.update(offset.to_bytes(8, "big"))
            digest.update(data)
    if file_identity(path) != before:
        raise CacheUnavailable("缓存核验期间文件身份变化")
    return {"version": 2, "asset_id": asset_id, "volume_id": volume_id,
            "size": before["size"], "mtime_ns": before["mtime_ns"], "samples": digest.hexdigest()}


def inspect_cached_file(path, expected, asset_id, volume_id):
    """Shared by transfer, cleanup and status: one definition of a valid cache."""
    result = {"state": "unverified", "valid": False, "bytes": 0, "reason": "缓存缺少完整导入凭据"}
    try:
        actual = file_identity(path)
        result["bytes"] = actual["size"]
        if expected:
            if expected.get("version") == 2:
                # content_sha256 is checked while uploading, not by another sample pass.
                recorded = {key: value for key, value in expected.items() if key != "content_sha256"}
                valid = cache_evidence(path, asset_id, volume_id) == recorded
            else:
                valid = actual == expected
            if valid:
                return {"state": "verified", "valid": True, "bytes": actual["size"], "reason": None}
            metadata_same = all(actual[key] == expected.get(key) for key in ("size", "mtime_ns"))
            result["reason"] = ("旧缓存文件编号变化，需与原片核验后恢复" if metadata_same and not expected.get("version")
                                else "缓存内容或身份与导入记录不一致，已保留文件")
    except FileNotFoundError:
        result.update(state="missing", reason="完整缓存文件不在原位置")
    except (OSError, CacheUnavailable) as exc:
        result.update(state="unavailable", reason=str(exc))
    return result


_observations = OrderedDict()
_observation_lock = threading.Lock()


def observe_cached_file(path, expected, asset_id, volume_id):
    """Bounded read cache for display only; transfer/deletion always verify fresh.

    Metadata and volume changes invalidate immediately; sample evidence is read
    again after 30 seconds even when metadata is unchanged. Nothing is persisted.
    """
    import json
    try:
        stat = Path(path).lstat()
        key = (str(path), stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size,
               stat.st_mtime_ns, stat.st_ctime_ns, asset_id, volume_id, json.dumps(expected, sort_keys=True))
    except OSError:
        return inspect_cached_file(path, expected, asset_id, volume_id)
    stamp = time.monotonic()
    with _observation_lock:
        saved = _observations.get(key)
        if saved and stamp - saved[0] < 30:
            _observations.move_to_end(key)
            return dict(saved[1])
    result = inspect_cached_file(path, expected, asset_id, volume_id)
    with _observation_lock:
        _observations[key] = (stamp, dict(result))
        while len(_observations) > 512:
            _observations.popitem(last=False)
    return result


def is_appledouble(path):
    """Recognize metadata by both name and signature, never just a prefix."""
    path = Path(path)
    try:
        if path.name.startswith('._') and path.is_file() and not path.is_symlink():
            with path.open('rb') as handle:
                return handle.read(4) == b'\x00\x05\x16\x07'
    except OSError:
        pass
    return False


def unlink_cache(path):
    """Remove a known task file and only its verified AppleDouble metadata."""
    path = Path(path)
    path.unlink(missing_ok=True)
    metadata = path.with_name("._" + path.name)
    try:
        if metadata.is_file() and not metadata.is_symlink():
            with metadata.open("rb") as handle:
                apple_double = handle.read(4) == b"\x00\x05\x16\x07"
            if apple_double:
                metadata.unlink(missing_ok=True)
    except FileNotFoundError:
        # macOS may remove the sidecar asynchronously after the data file is unlinked.
        pass
