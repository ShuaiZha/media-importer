import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import PurePosixPath

from .model import (BackupError, SourceError, SourceBlocked, CacheUnavailable, Cancelled,
                    DestinationError, NameConflict, now)
from .store import Store, fsync_directory
from .runtime import Reconfigure, SyncDisabled
from .storage import (require_cache, file_identity, unlink_cache, cache_evidence,
                      inspect_cached_file, sample_ranges)
from .issues import describe
from .contracts import MediaSource, ArchiveDestination
from .media import MEDIA_SCOPES, select_groups, selected_file_ids


LOG = logging.getLogger("media_importer")


class CacheFull(BackupError):
    pass


class Service:
    def __init__(self, config, store, source: MediaSource, destination: ArchiveDestination, stop, control=None, stage=None):
        self.config, self.store, self.source, self.destination, self.stop = config, store, source, destination, stop
        self.destination_id = config.destination_id
        self._progress_at = 0
        self._progress_log_at = 0
        self._last_notice = None
        self.control = control
        self._samples = deque()
        self.stage = stage
        self.pipeline_stop = None
        self.cache_guard = threading.RLock()

    def transfer_status(self, **updates):
        if self.stage:
            self.store.stage_status(self.stage, **updates)
        else:
            self.store.status(**updates)

    def allowed(self, policy=None):
        if self.control:
            return self.control.allowed(policy)
        return self.config.backup["enabled"] and (not policy or self.config.backup[policy])

    def boundary(self):
        self.check_stop()
        if self.control:
            self.control.boundary()
        elif not self.allowed():
            raise SyncDisabled("同步已关闭，任务与缓存已保留")

    def observe_source(self, source=None):
        source = source or self.source
        try:
            identity = source.identify()
            result = {"online": True, "checked_at": now(), **identity}
            if hasattr(source, "telemetry"):
                try:
                    result["telemetry"] = source.telemetry()
                except SourceError as exc:
                    result["telemetry"] = {"available": False, "error": str(exc), "observed_at": now()}
            self.store.status(source=result)
            return result
        except SourceError as exc:
            self.store.status(source={"online": False, "checked_at": now(), "reason": str(exc),
                                     "transport": self.config.source.get("transport", "usb")})
            self.store.invalidate_inventory("相机连接或身份变化，需重新核对清单")
            raise

    def observe_destination(self):
        try:
            result = self.destination.check()
            self.store.status(destination={"online": True, "checked_at": now(), **result})
            return result
        except DestinationError as exc:
            issue = describe(exc, "upload")
            self.store.status(destination={"online": False, "checked_at": now(), "reason": str(exc), "issue": issue})
            raise

    def rows(self):
        return [row for row in self.store.files(destination=self.destination_id)
                if self.store.asset(row).serial == self.config.source["serial"]
                and self.store.asset(row).source_type == self.config.source["type"]]

    @contextmanager
    def monitoring(self):
        finished = threading.Event()
        def monitor():
            store = Store(self.store.directory)
            source = self.source.fork(self.stop) if hasattr(self.source, "fork") else self.source
            # Connection monitoring must remain responsive even when the normal
            # download client has long timeouts and retries.
            if source is not self.source:
                if hasattr(source, "timeout"):
                    source.timeout = min(source.timeout, 3)
                if hasattr(source, "attempts"):
                    source.attempts = 1
            try:
                while not finished.is_set() and not self.stop.is_set():
                    keep_awake = self.allowed() and bool(store.get_meta("status", {}).get("source_work"))
                    try:
                        identity = source.identify()
                        result = {"online": True, "checked_at": now(), **identity,
                                  "transport": self.config.source.get("transport", "usb")}
                        if hasattr(source, "telemetry"):
                            try:
                                result["telemetry"] = source.telemetry()
                            except SourceError as exc:
                                result["telemetry"] = {"available": False, "error": str(exc), "observed_at": now()}
                        store.status(source=result)
                        telemetry = result.get("telemetry")
                        if telemetry:
                            if not telemetry.get("available") or source.idle_reason(telemetry):
                                store.invalidate_inventory("相机状态变化，需重新核对清单")
                    except SourceError as exc:
                        store.status(source={"online": False, "checked_at": now(), "reason": str(exc),
                                             "transport": self.config.source.get("transport", "usb")})
                        # A released Wi-Fi camera may sleep while durable caches
                        # upload. Keep the completed import evidence, but report
                        # its current connection honestly.
                        released = (self.config.source.get("transport") == "wifi" and not keep_awake
                                    and store.get_meta("status", {}).get("inventory", {}).get("verified"))
                        if not released:
                            store.invalidate_inventory("相机断开，连接后需重新核对清单")
                    except Cancelled:
                        break
                    finished.wait(min(5, self.config.backup["poll_seconds"]) if keep_awake else self.config.backup["poll_seconds"])
            finally:
                store.close()
        def keep_alive():
            if not hasattr(self.source, "keep_alive"):
                return
            db = Store(self.store.directory)
            source = self.source.fork(self.stop) if hasattr(self.source, "fork") else self.source
            # A stuck status read must not delay the recommended three-second keepalive.
            if hasattr(source, "timeout"):
                source.timeout = min(source.timeout, 2)
            try:
                while not finished.is_set() and not self.stop.is_set():
                    started = time.monotonic()
                    if self.allowed() and db.get_meta("status", {}).get("source_work"):
                        try:
                            source.keep_alive()
                            db.status(source_keep_alive={"ok": True})
                        except (SourceError, Cancelled) as exc:
                            db.status(source_keep_alive={"ok": False, "reason": str(exc)})
                    finished.wait(max(.1, 3 - (time.monotonic() - started)))
            finally:
                db.close()
        def monitor_destination():
            if not hasattr(self.destination, "fork"):
                return
            store, remote = Store(self.store.directory), self.destination.fork()
            remote.stop = self.stop
            observer = Service(self.config, store, self.source, remote, self.stop)
            try:
                while not finished.is_set() and not self.stop.is_set():
                    try:
                        observer.observe_destination()
                    except (BackupError, OSError):
                        pass
                    finished.wait(self.config.backup["retry_seconds"])
            finally:
                remote.close()
                store.close()
        threads = [threading.Thread(target=target, name=name, daemon=True)
                   for target, name in ((monitor, "source-monitor"), (monitor_destination, "destination-monitor"), (keep_alive, "source-keepalive"))]
        for thread in threads:
            thread.start()
        try:
            yield
        finally:
            finished.set()
            for thread in threads:
                thread.join(timeout=max(65, self.config.source["timeout_seconds"] + 2))

    def report(self, phase, message, **kwargs):
        quiet = kwargs.pop("quiet", False)
        if phase in ("uploading", "downloading"):
            kwargs.setdefault("issue", None)
            self._samples.clear()
            self._samples.append((time.monotonic(), kwargs.get("current_bytes", 0)))
            self._progress_at = 0
            kwargs.update(transfer_id=uuid.uuid4().hex, progress_updated_at=now(), speed_bytes_per_second=0, eta_seconds=None)
        else:
            kwargs.update(speed_bytes_per_second=0, eta_seconds=None)
        self.transfer_status(phase=phase, message=message, **kwargs)
        if not quiet and self._last_notice != (phase, message):
            LOG.info("%s: %s", phase, message)
            self._last_notice = (phase, message)

    def progress(self, done, total):
        if self.control and self.control.transfer_stop.is_set():
            raise Cancelled("同步已停止，保留未完成任务")
        self.check_stop()
        current = time.monotonic()
        if self._samples and done < self._samples[-1][1]:
            self._samples.clear()
        self._samples.append((current, done))
        while len(self._samples) > 2 and current - self._samples[0][0] > 10:
            self._samples.popleft()
        elapsed = current - self._samples[0][0]
        speed = max(0, (done - self._samples[0][1]) / elapsed) if elapsed > 0.25 else 0
        if current - self._progress_at >= 1 or done == total:
            require_cache(self.store)
            if self.stage == "import":
                from .status import age
                telemetry = self.store.get_meta("status", {}).get("source", {}).get("telemetry", {})
                if telemetry.get("available") and age(telemetry.get("observed_at")) < 30:
                    reason = self.source.idle_reason(telemetry)
                    if reason:
                        raise SourceBlocked(reason)
            self.transfer_status(current_bytes=done, current_total_bytes=total, progress_updated_at=now(),
                              speed_bytes_per_second=speed, eta_seconds=max(0, total - done) / speed if speed else None)
            self._progress_at = current
        if total and current - self._progress_log_at >= 30:
            LOG.info("%s进度: %.1f%% (%s / %s bytes)", "导入" if self.stage == "import" else "上传" if self.stage == "upload" else "传输", min(100, done * 100 / total), done, total)
            self._progress_log_at = current

    def check_stop(self):
        if self.stop.is_set() or self.pipeline_stop and self.pipeline_stop.is_set():
            raise Cancelled("正在停止，任务记录和缓存已保留")
        if self.control and not self.control.allowed():
            raise SyncDisabled("同步已关闭或配置无效，任务与缓存已保留")

    @staticmethod
    def valid_remote(item, size):
        return item is not None and not item.get("is_directory", False) and item.get("size") == size

    def cache_valid(self, row):
        expected = json.loads(row["cache_identity"] or "null")
        return inspect_cached_file(self.store.cached_path(row), expected, row["asset_id"],
                                   self.store.get_meta("cache_identity"))["valid"]

    def verify_caches(self, cancelled=None):
        """Explicit read-only source verification; never transfer, publish or delete.

        Legacy records are adopted only after matching their prior size/mtime,
        source identity and bounded source content, or their original inode.
        An unverified complete file is never overwritten on a later retry.
        """
        require_cache(self.store)
        result = {"verified": [], "pending": []}
        volume = self.store.get_meta("cache_identity")
        inventory = None
        for row in self.rows():
            if self.stop.is_set() or cancelled and cancelled():
                raise Cancelled("缓存核验已停止；文件与原片保留")
            path = self.store.cached_path(row)
            if not path.exists():
                continue
            asset = self.store.asset(row)
            expected = json.loads(row["cache_identity"] or "null")
            try:
                before = file_identity(path)
                if not expected or before["size"] != asset.size:
                    raise CacheUnavailable("缺少完整导入凭据或大小不符，保留缓存待人工核验")
                if expected.get("version") == 2:
                    actual = cache_evidence(path, row["asset_id"], volume)
                    if actual == expected:
                        result["verified"].append(asset.path)
                        continue
                    # Recover only timestamp drift, and only after a fresh
                    # identity and sample comparison against the original.
                    if any(actual.get(key) != value for key, value in expected.items() if key != "mtime_ns"):
                        raise CacheUnavailable("缓存与导入凭据不符，保留缓存待人工核验")
                    verify_source = True
                else:
                    verify_source = before != expected
                if verify_source:
                    if not expected.get("version") and any(before[key] != expected.get(key) for key in ("size", "mtime_ns")):
                        raise CacheUnavailable("缓存大小或修改时间变化，保留缓存待人工核验")
                    if not hasattr(self.source, "read_range"):
                        raise CacheUnavailable("设备不支持小范围核验，保留原缓存")
                    if inventory is None:
                        inventory = {a.identity: a for a in self.source.snapshot()}
                    if asset.identity not in inventory:
                        raise SourceError("原相机素材身份不匹配，等待连接原相机和存储卡")
                    with path.open("rb") as handle:
                        for offset, length in sample_ranges(asset.size):
                            if self.stop.is_set() or cancelled and cancelled():
                                raise Cancelled("缓存核验已停止")
                            handle.seek(offset)
                            if handle.read(length) != self.source.read_range(asset, offset, length):
                                raise CacheUnavailable("缓存抽样内容与原片不同，保留文件待人工核验")
                    # Reconfirm identity after source reads, including associated files.
                    if asset.identity not in {a.identity for a in self.source.snapshot()}:
                        raise SourceError("核验期间原片身份变化，保留缓存")
                evidence = cache_evidence(path, row["asset_id"], volume)
                if file_identity(path) != before:
                    raise CacheUnavailable("核验期间缓存变化，未更新凭据")
                # Preserve task timestamps, archive paths and all other business fields.
                with self.store.db:
                    self.store.db.execute("UPDATE files SET cache_identity=? WHERE id=?",
                                          (json.dumps(evidence), row["id"]))
                result["verified"].append(asset.path)
            except Cancelled:
                raise
            except (BackupError, OSError, ValueError) as exc:
                result["pending"].append({"path": asset.path, "reason": str(exc)})
        return result

    def clean_cache(self, row):
        if row["state"] != "uploaded" or not self.allowed():
            return
        with self.cache_guard:
            try:
                require_cache(self.store)
                if not self.allowed():
                    return
                path = self.store.cached_path(row)
                if path.exists():
                    if not self.cache_valid(row):
                        raise CacheUnavailable("缓存身份变化，未清理该文件")
                unlink_cache(path)
                fsync_directory(self.store.cache)
                self.store.update(row["id"], cache_cleanup_error=None)
            except (OSError, CacheUnavailable) as exc:
                self.store.update(row["id"], cache_cleanup_error=str(exc))
                LOG.warning("已归档，缓存待清理: %s", self.store.asset(row).path)

    def clean_temporaries(self, kind=None):
        remote = self.destination.fork(stop=self.stop) if hasattr(self.destination, "fork") else self.destination
        try:
            for item in self.store.temporary_items(self.destination_id):
                if self.stop.is_set():
                    return
                if item["state"] == "blocked" or kind and item["kind"] != kind:
                    continue
                try:
                    if item["kind"] == "local":
                        require_cache(self.store)
                        row = self.store.get(item["file_id"])
                        path = self.store.cached_path(row).with_suffix(".download")
                        if path.name != item["path"] or path.is_symlink():
                            raise DestinationError("本地临时文件归属无法确认", code="temporary_identity")
                        unlink_cache(path)
                        fsync_directory(self.store.cache)
                    else:
                        remote.delete_owned(item["path"], json.loads(item["identity"] or "null"))
                    self.store.temporary_result(item["id"])
                except (BackupError, OSError, ValueError) as exc:
                    self.store.temporary_result(item["id"], remote.redact(str(exc)),
                                                blocked=getattr(exc, "code", None) == "temporary_identity")
        finally:
            if remote is not self.destination:
                remote.close()

    def recover_interrupted(self):
        """Recover local commit boundaries and queue only interrupted attempts for cleanup."""
        for row in self.rows():
            partial = self.store.cached_path(row).with_suffix(".download")
            if row["state"] in ("pending", "missing") and row["cache_identity"]:
                try:
                    require_cache(self.store)
                    expected = json.loads(row["cache_identity"])
                    cache = self.store.cached_path(row)
                    if not cache.exists() and partial.exists() and inspect_cached_file(
                            partial, expected, row["asset_id"], self.store.get_meta("cache_identity"))["valid"]:
                        os.replace(partial, cache)
                        fsync_directory(self.store.cache)
                    if self.cache_valid(row):
                        self.store.update(row["id"], state="cached", error=None, error_stage=None)
                except (OSError, CacheUnavailable):
                    pass
            if partial.exists():
                self.store.queue_temporary(row, "local", partial.name)
            if row["state"] == "cached" and row["temp_path"]:
                self.store.queue_temporary(row, "remote", row["temp_path"], json.loads(row["remote_identity"] or "null"))
                self.store.update(row["id"], temp_path=None, remote_identity=None)
        for stage in ("import", "upload"):
            self.store.stage_status(stage, phase="idle", current_bytes=0, current_total_bytes=0,
                                    current_file=None, current_file_id=None, speed_bytes_per_second=0, eta_seconds=None)

    def allocate(self, row, force_alternative=False):
        asset = self.store.asset(row)
        base = PurePosixPath(asset.device_directory) / PurePosixPath(asset.path).name
        occupied = self.store.reserved_paths(self.destination_id, row["id"])
        occupied.update(str(base.parent / name).casefold() for name in self.destination.list_names(str(base.parent)))
        if force_alternative and row["remote_path"]:
            occupied.add(row["remote_path"].casefold())
        number = 0
        while True:
            self.check_stop()
            candidate = base if not number else base.with_name(f"{base.stem}_{number}{base.suffix}")
            if str(candidate).casefold() not in occupied:
                return str(candidate)
            number += 1

    def confirmed_final(self, row, item):
        if not self.valid_remote(item, self.store.asset(row).size):
            return False
        # A lost rename acknowledgement must not adopt an unrelated equal-size file.
        if row["remote_identity"] and hasattr(self.destination, "file_identity"):
            return self.destination.file_identity(row["remote_path"]) == json.loads(row["remote_identity"])
        return False

    def commit_uploaded(self, row):
        self.store.update(row["id"], state="uploaded", error=None, error_stage=None)
        row = self.store.get(row["id"])
        self.clean_cache(row)
        LOG.info("上传完成: %s -> %s", self.store.asset(row).path, row["remote_path"])

    def upload_cached(self, row):
        self.check_stop()
        require_cache(self.store)
        if not self.allowed():
            raise SyncDisabled("同步已关闭")
        row = self.store.get(row["id"])
        asset = self.store.asset(row)
        cache = self.store.cached_path(row)
        if row["state"] == "uploaded":
            self.clean_cache(row)
            return
        if not row["remote_path"]:
            remote = self.allocate(row)
            self.store.update(row["id"], remote_path=remote)
            row = self.store.get(row["id"])

        if row["state"] == "publishing":
            temporary = self.destination.stat(row["temp_path"])
            final = self.destination.stat(row["remote_path"])
            if temporary is None and self.confirmed_final(row, final):
                # The publish intent is durable before rename: recover an acknowledged-lost rename.
                self.commit_uploaded(row)
                return
            if self.valid_remote(temporary, asset.size) and self.destination.file_identity(row["temp_path"]) == json.loads(row["remote_identity"] or "null"):
                self.store.update(row["id"], state="staged")
            else:
                self.store.update(row["id"], state="cached")
            row = self.store.get(row["id"])

        if row["state"] == "staged" and (not self.valid_remote(self.destination.stat(row["temp_path"]), asset.size)
                or not row["remote_identity"] or self.destination.file_identity(row["temp_path"]) != json.loads(row["remote_identity"])):
            raise DestinationError("待归档文件身份或大小已变化，保留完整缓存并等待核对")

        if row["state"] != "staged":
            if not self.cache_valid(row):
                if row["cleanup"] in ("deleted", "absent"):
                    self.store.update(row["id"], error="完整缓存丢失且相机原片已清理，需要人工恢复数据", error_stage="import")
                else:
                    self.store.update(row["id"], state="pending", error="完整缓存待核验，已保留；请核验缓存后继续", error_stage="import")
                raise CacheUnavailable("完整缓存不可用，请检查原缓存盘和文件；未删除任何完整文件")
            self.upload_file(row, cache, asset)
            row = self.store.get(row["id"])
            self.report("publishing", "上传结束，正在核对大小并归档")
            if not self.valid_remote(self.destination.stat(row["temp_path"]), asset.size):
                raise DestinationError("目的地临时文件大小不一致，完整本地缓存保留")
            self.store.update(row["id"], state="staged", error=None)
            row = self.store.get(row["id"])

        while True:
            self.check_stop()
            taken = self.store.reserved(self.destination_id, row["remote_path"], row["id"]) or any(
                name.casefold() == PurePosixPath(row["remote_path"]).name.casefold()
                for name in self.destination.list_names(str(PurePosixPath(row["remote_path"]).parent)))
            if taken:
                self.store.update(row["id"], remote_path=self.allocate(row, force_alternative=True))
                row = self.store.get(row["id"])
            self.store.update(row["id"], state="publishing")
            try:
                self.destination.publish(row["temp_path"], row["remote_path"])
                break
            except NameConflict:
                # The server rejected the rename without modifying either file.
                self.store.update(row["id"], state="staged", remote_path=self.allocate(row, force_alternative=True))
                row = self.store.get(row["id"])
        if not self.confirmed_final(row, self.destination.stat(row["remote_path"])) or self.destination.stat(row["temp_path"]) is not None:
            raise DestinationError("目的地 尚未确认正式文件发布完成")
        self.commit_uploaded(row)

    def upload_file(self, row, cache, asset):
        if row["temp_path"]:
            self.store.queue_temporary(row, "remote", row["temp_path"], json.loads(row["remote_identity"] or "null"))
            self.clean_temporaries("remote")
        temporary = str(PurePosixPath(row["remote_path"]).parent /
                        (".media-import-" + row["id"] + "-" + uuid.uuid4().hex + ".partial"))
        self.store.update(row["id"], temp_path=temporary, remote_identity=None, error=None, error_stage=None)
        self.report("uploading", "正在整文件上传；中断后从头重传", current_file=asset.path,
                    current_file_id=row["id"], current_bytes=0, current_total_bytes=asset.size)
        def created(identity):
            self.store.update(row["id"], remote_identity=json.dumps(identity))
        def staged(identity):
            self.store.update(row["id"], state="staged", remote_identity=json.dumps(identity), error=None, error_stage=None)
        try:
            self.destination.upload(cache, temporary, self.progress, created, staged)
        except BaseException:
            saved = self.store.get(row["id"])
            if saved["state"] == "cached":
                self.store.queue_temporary(saved, "remote", temporary, json.loads(saved["remote_identity"] or "null"))
                self.store.update(row["id"], temp_path=None, remote_identity=None)
                self.transfer_status(current_bytes=0, speed_bytes_per_second=0, eta_seconds=None)
                self.clean_temporaries("remote")
            raise

    def import_asset(self, row):
        self.check_stop()
        require_cache(self.store)
        if not self.allowed():
            raise SyncDisabled("同步已关闭")
        asset = self.store.asset(row)
        cache = self.store.cached_path(row)
        partial = cache.with_suffix(".download")
        if self.cache_valid(row):
            self.store.update(row["id"], state="cached", error=None, error_stage=None)
            return
        if cache.exists() or cache.is_symlink():
            raise CacheUnavailable("已有完整缓存待核验，未覆盖或重新下载；请核验缓存后继续")
        # Only discard our own unfinished download, never a complete unuploaded cache.
        if partial.exists():
            self.store.queue_temporary(row, "local", partial.name)
            self.clean_temporaries("local")
            if partial.exists():
                raise CacheUnavailable("未完成下载尚未清理，请检查缓存盘")
        available = shutil.disk_usage(self.store.cache).free
        reserve = int(self.config.limits["min_free_disk_gib"] * 1024 ** 3)
        used = self.store.cache_bytes()
        quota = int(self.config.limits["max_cache_gib"] * 1024 ** 3)
        self.store.status(disk={"free_bytes": available, "reserve_bytes": reserve, "next_file_bytes": asset.size,
                                "next_file": asset.path, "extra_bytes_needed": max(0, reserve + asset.size - available),
                                "cache_used_bytes": used, "quota_bytes": quota})
        if available < reserve + asset.size:
            raise CacheFull(f"缓存盘空间不足：导入后需要保留 {self.config.limits['min_free_disk_gib']} GiB，等待释放空间")
        if quota and used + asset.size > quota:
            raise CacheFull(f"缓存额度不足：上限 {self.config.limits['max_cache_gib']} GiB，等待已归档缓存清理或调整上限")
        self.store.update(row["id"], error=None, error_stage=None)
        self.report("downloading", "正在从相机导入", current_file=asset.path, current_file_id=row["id"], current_bytes=0, current_total_bytes=asset.size)
        self.store.status(source_work=True)
        committed = False
        try:
            self.source.download(asset, partial, self.progress)
            require_cache(self.store)
            # Explicit whole-second metadata avoids delayed write timestamps
            # on external FAT-family volumes shared by Docker Desktop.
            committed_mtime = time.time_ns() // 2_000_000_000 * 2_000_000_000
            os.utime(partial, ns=(committed_mtime, committed_mtime))
            # Reopen and flush after the writer closes. Docker Desktop's
            # external-volume metadata can settle only at this boundary;
            # keep the subsequent identity checks strict.
            with partial.open("rb") as imported:
                os.fsync(imported.fileno())
            identity = cache_evidence(partial, row["asset_id"], self.store.get_meta("cache_identity"))
            if identity["size"] != asset.size:
                raise SourceError("导入大小不一致")
            # This intent makes a crash between rename and SQLite commit recoverable.
            self.store.update(row["id"], cache_identity=json.dumps(identity))
            os.replace(partial, cache)
            os.utime(cache, ns=(committed_mtime, committed_mtime))
            fsync_directory(self.store.cache)
            with cache.open("rb") as imported:
                os.fsync(imported.fileno())
            final_identity = cache_evidence(cache, row["asset_id"], self.store.get_meta("cache_identity"))
            if any(final_identity[key] != identity[key] for key in ("asset_id", "volume_id", "size", "samples")):
                raise CacheUnavailable("正式缓存与导入文件不一致，已保留完整文件和相机原片")
            # A rename can refresh stale external-volume timestamps. Persist
            # the final path's evidence, after confirming identical content.
            self.store.update(row["id"], state="cached", cache_identity=json.dumps(final_identity),
                              error=None, error_stage=None)
            committed = True
        finally:
            if not committed:
                self.store.queue_temporary(row, "local", partial.name)
                self.transfer_status(current_bytes=0, speed_bytes_per_second=0, eta_seconds=None)
                self.clean_temporaries("local")

    def source_snapshot(self):
        fresh = self.source.snapshot()
        if hasattr(self.source, "reconcile"):
            fresh = self.source.reconcile(fresh, self.rows())
        return fresh

    def cleanup_groups(self, attempted):
        if not self.allowed("delete_source_after_import"):
            return
        for group in self.store.groups(self.destination_id):
            if group["id"] in attempted:
                continue
            rows = [self.store.get(key) for key in json.loads(group["members"])]
            if not rows or any(not r or r["group_id"] != group["id"] or
                               r["state"] not in ("cached", "staged", "publishing", "uploaded") for r in rows):
                continue
            members = [self.store.asset(r) for r in rows]
            if (any(a.serial != self.config.source["serial"] or a.source_type != self.config.source["type"] for a in members)
                    or not select_groups([{"members": members}], self.config.backup["media_scope"])):
                continue
            if all(r["cleanup"] in ("deleted", "absent") for r in rows):
                continue
            attempted.add(group["id"])
            if not group["safe"]:
                for row in rows:
                    self.store.update(row["id"], cleanup="blocked", source_cleanup_error=group["reason"])
                continue
            try:
                with self.cache_guard:
                    self.boundary()
                    if not self.allowed("delete_source_after_import"):
                        return
                    rows = [self.store.get(row["id"]) for row in rows]
                    require_cache(self.store)
                    self.store.status(source_work=True)
                    fresh = self.source_snapshot()
                    by_path = {a.path: a for a in fresh}
                    members = [self.store.asset(r) for r in rows]
                    # Recompute associations: a newly appeared sidecar postpones this group's deletion.
                    group_ids = {a.identity for a in members}
                    def verify_members(snapshot):
                        for group_now in self.source.groups(snapshot):
                            identities = {a.identity for a in group_now["members"]}
                            if group_ids & identities and (identities - group_ids or not group_now["safe"]):
                                raise SourceError("素材组出现新增成员或关联变化，核对并导入完成后再清理")
                    verify_members(fresh)
                    for row, asset in zip(rows, members):
                        actual = by_path.get(asset.path)
                        if actual and actual.identity != asset.identity:
                            raise SourceError("同路径素材身份已变化，保留相机文件")
                        if not self.cache_valid(row):
                            if row["state"] != "uploaded" or not row["remote_identity"] or not self.confirmed_final(row, self.destination.stat(row["remote_path"])):
                                raise CacheUnavailable("素材组缺少可确认的完整副本，保留相机原片")
                    candidates = self.source.deletion_candidates(members)
                    for row, asset in zip(rows, members):
                        self.boundary()
                        if not self.allowed("delete_source_after_import"):
                            return
                        latest = self.source_snapshot()
                        current = {a.path: a for a in latest}
                        if asset.path not in current:
                            self.store.update(row["id"], cleanup="deleted" if row["cleanup"] == "deleting" else "absent", source_cleanup_error=None)
                            continue
                        if current[asset.path].identity != asset.identity:
                            raise SourceError("删除前素材身份变化，未执行删除")
                        verify_members(latest)
                        if candidates.get(asset.identity, "不支持安全删除"):
                            self.store.update(row["id"], cleanup="blocked", source_cleanup_error=candidates.get(asset.identity))
                            continue
                        self.store.update(row["id"], cleanup="deleting", source_cleanup_error=None)
                        try:
                            self.source.delete(asset)
                        except SourceError:
                            # The response may be lost; re-enumeration decides the outcome.
                            pass
                        after = {a.path: a for a in self.source_snapshot()}
                        for member_row, member in zip(rows, members):
                            if member.path not in after:
                                self.store.update(member_row["id"], cleanup="deleted", source_cleanup_error=None)
                        if asset.path in after:
                            self.store.update(row["id"], cleanup="pending", source_cleanup_error="相机文件仍存在，稍后重新核对")
            except (BackupError, OSError) as exc:
                if isinstance(exc, (Cancelled, SyncDisabled, Reconfigure)):
                    raise
                for row in rows:
                    current = self.store.get(row["id"])
                    if current["cleanup"] not in ("deleted", "absent"):
                        self.store.update(row["id"], source_cleanup_error=str(exc))
            finally:
                self.store.status(source_work=False)

    def prepare_imports(self):
        self.boundary()
        scope = self.config.backup["media_scope"]
        previous_scope = self.store.get_meta("status", {}).get("inventory", {}).get("media_scope", "all")
        self.store.status(inventory={"scanned": False, "verified": False}, source_work=True)
        self.observe_source()
        self.report("scanning", "正在核对相机完整素材清单", issue=None, current_file=None, current_file_id=None)
        self.store.status(source_work=True)
        assets = self.source_snapshot()
        groups = select_groups(self.source.groups(assets), scope)
        selected = [a for group in groups for a in group["members"]]
        known = self.rows()
        eligible = selected_file_ids(known, scope)
        available = {a.identity for a in assets}
        paths = {a.path for a in assets}
        for row in known:
            if row["id"] not in eligible:
                continue
            if row["state"] in ("pending", "missing"):
                if row["asset_id"] not in available:
                    self.store.update(row["id"], state="missing", error="相机当前没有这份素材，等待再次连接原卡", error_stage="import")
                elif row["state"] == "missing":
                    self.store.update(row["id"], state="pending", error=None, error_stage=None)
            elif row["state"] == "uploaded" and self.store.asset(row).path not in paths:
                self.store.update(row["id"], cleanup="absent")
        known_ids = {r["asset_id"] for r in known}
        additions = [a for a in selected if a.identity not in known_ids]
        batch_id = self.store.get_meta("last_batch")
        prior_batch = self.store.files(batch_id=batch_id) if batch_id else []
        if previous_scope != scope or any(r["id"] not in eligible for r in prior_batch):
            # A scope change creates a view of the remaining selected work, not
            # a mutation of previous mixed batches or their file identities.
            archived = {r["asset_id"] for r in known if r["state"] == "uploaded"}
            remaining = [a for a in selected if a.identity not in archived]
            batch_id = self.store.batch(self.destination_id, remaining, scope) if remaining else None
        elif additions:
            batch_id = self.store.batch(self.destination_id, additions, scope)
        elif batch_id and not any(r["destination"] == self.destination_id and self.store.asset(r).serial == self.config.source["serial"]
                                 for r in self.store.files(batch_id=batch_id)):
            batch_id = None
        self.store.status(batch_id=batch_id, inventory={"scanned": True, "verified": False,
                          "batch_id": batch_id, "serial": self.config.source["serial"], "destination": self.destination_id,
                          "checked_at": now(), "source_files": len(assets), "new_files": len(additions),
                          "media_scope": scope, "selected_files": len(selected), "excluded_files": len(assets) - len(selected)})
        self.store.save_groups(self.destination_id, groups)
        self.store.reporting_run(self.config)
        return {"assets": selected, "batch_id": batch_id}

    def verify_imports(self, inventory):
        self.boundary()
        require_cache(self.store)
        rows = {r["asset_id"]: r for r in self.rows()}
        for asset in inventory["assets"]:
            row = rows.get(asset.identity)
            if not row or row["state"] not in ("cached", "staged", "publishing", "uploaded"):
                self.report("waiting_retry", "仍有素材未完整导入；其他完整缓存可继续上传", current_file=None,
                            current_file_id=None, current_bytes=0, current_total_bytes=0)
                return False
            if row["state"] != "uploaded":
                # Upload may have committed and cleaned this file concurrently.
                complete = self.cache_valid(row) or self.store.get(row["id"])["state"] == "uploaded"
                if not complete:
                    self.report("waiting_retry", "完整缓存缺失，等待重新导入", issue=None)
                    return False
        self.report("verifying", "导入结束，正在复查相机清单", issue=None, current_file=None, current_file_id=None)
        self.store.status(source_work=True)
        self.boundary()
        fresh = self.source_snapshot()
        fresh_selected = [a for group in select_groups(self.source.groups(fresh), self.config.backup["media_scope"])
                          for a in group["members"]]
        expected = {a.identity for a in inventory["assets"]
                    if rows[a.identity]["cleanup"] not in ("deleted", "absent")}
        same = {a.identity for a in fresh_selected} == expected
        state = self.store.get_meta("status", {}).get("inventory", {})
        state.update(verified=same, verified_at=now() if same else None,
                     changed_files=len({a.identity for a in fresh_selected} ^ expected),
                     excluded_files=len(fresh) - len(fresh_selected))
        self.store.status(inventory=state, source_work=False)
        complete_message = "本轮相机导入完成，已复查清单" if self.config.backup["media_scope"] == "all" else (
            MEDIA_SCOPES[self.config.backup["media_scope"]] + "导入完成，其他类别保留在相机中")
        self.report("complete" if same else "waiting_scan", complete_message if same else
                    "复查发现新增或变更素材，将在下一轮处理，请保持相机连接", current_file=None,
                    current_file_id=None, current_bytes=0, current_total_bytes=0, issue=None)
        if same:
            self.store.import_completed(inventory["batch_id"])
        return same

    def cycle(self):
        if self.stop.is_set():
            raise Cancelled("正在停止")
        if not self.store.get_meta("cache_identity"):
            from .storage import initialize_cache
            initialize_cache(self.store)
        self.store.status(source_work=False, sync_enabled=self.allowed(),
                          active_destination=self.destination_id, active_serial=self.config.source["serial"],
                          active_source_type=self.config.source["type"])
        self.store.reporting_run(self.config)
        if not self.allowed():
            # Monitoring uses separate read-only clients whose stop events are not the transfer switches.
            self.report("paused", "同步已关闭，只检测连接状态")
            for stage in ("import", "upload"):
                self.store.stage_status(stage, phase="paused", message="同步关闭，任务与完整缓存保留",
                                        speed_bytes_per_second=0, eta_seconds=None)
            return {"complete": False, "reason": "paused"}
        self.boundary()
        from .pipeline import transfer
        transfer(self)
        if self.control and self.control.draining():
            self.control.finish_stop()
        self.boundary()
        state = self.store.get_meta("status", {})
        batch_id = state.get("batch_id")
        batch_rows = self.store.files(batch_id=batch_id) if batch_id else []
        rows = self.rows()
        eligible = selected_file_ids(rows, self.config.backup["media_scope"])
        pending = [r for r in rows if r["state"] != "uploaded" and
                   (r["id"] in eligible or r["state"] in ("cached", "staged", "publishing"))]
        if pending:
            stages = state.get("stages", {})
            waiting = next((stages[s] for s in ("import", "upload") if stages.get(s, {}).get("phase", "").startswith("waiting")), {})
            self.report(waiting.get("phase", "waiting_retry"), waiting.get("message", "仍有素材待处理，任务与完整缓存已保留"))
            return {"complete": False, "reason": "pending_files"}
        if not batch_rows:
            scanned = state.get("inventory", {}).get("scanned", False)
            self.report("idle" if scanned else "waiting_source", "没有待导入素材" if scanned else "等待相机，已有完整缓存可独立上传")
            return {"complete": scanned, "files": 0, "bytes": 0, "reason": None if scanned else "source_offline"}
        self.store.batch_state(batch_id, "uploaded")
        result = {"complete": True, "batch_id": batch_id, "files": len(batch_rows),
                  "bytes": sum(self.store.asset(r).size for r in batch_rows), "finished_at": now()}
        result["cleanup"] = "归档完成；相机原片按导入清理设置处理"
        current = self.store.files(batch_id=batch_id)
        result["cleanup_pending"] = sum(r["cleanup"] not in ("deleted", "absent") for r in current)
        result["complete"] = all(r["state"] == "uploaded" for r in current)
        previous = self.store.get_meta("last_result", {})
        if previous.get("batch_id") == batch_id and previous.get("complete"):
            result["finished_at"] = previous["finished_at"]
        self.store.set_meta("last_result", result)
        import_state = self.store.get_meta("status", {}).get("stages", {}).get("import", {})
        waiting_import = import_state.get("phase", "").startswith("waiting")
        self.report(import_state["phase"] if waiting_import else "completed" if result["complete"] else "waiting_retry",
                    "完整缓存已归档；" + import_state.get("message", "等待相机") if waiting_import else result["cleanup"], current_file=None,
                    current_bytes=0, current_total_bytes=0)
        return result

    def safe_cycle(self):
        try:
            return self.cycle()
        except Reconfigure as exc:
            self.report("applying_config", str(exc))
            return {"complete": False, "reason": "settings_changed"}
        except SyncDisabled as exc:
            self.report("paused", str(exc))
            return {"complete": False, "reason": "paused"}
        except Cancelled:
            raise
        except (BackupError, OSError, ValueError) as exc:
            self.store.status(source_work=False)
            phase = "waiting_destination" if isinstance(exc, DestinationError) else "waiting_source" if isinstance(exc, SourceError) else "waiting_cache" if isinstance(exc, CacheFull) else "waiting_retry"
            message = self.destination.redact(str(exc)) if hasattr(self.destination, "redact") else str(exc)
            self.report(phase, message)
            if isinstance(exc, DestinationError):
                self.store.status(destination={"online": False, "reason": message})
            status = self.store.get_meta("status", {})
            current = next((stage.get("current_file") for stage in status.get("stages", {}).values()
                            if stage.get("phase") == "failed" and stage.get("current_file")), status.get("current_file"))
            self.store.set_meta("last_error", {"time": now(), "file": current, "message": message})
            return {"complete": False, "reason": message}
        finally:
            self.store.status(source_work=False)
