"""Independent single-file workers; SQLite and the bounded cache are the handoff."""
import threading
import time

from .issues import describe
from .model import BackupError, SourceBlocked, CacheUnavailable, Cancelled, now
from .runtime import Reconfigure, SyncDisabled
from .storage import require_cache
from .store import Store


def transfer(service):
    from .service import Service, CacheFull

    finished = threading.Event()
    condition = threading.Condition()
    generation = 0
    idle = {"import": None, "upload": None}
    errors = []
    halted = set()
    service.store.status(phase="transferring", message="两路队列独立运行", source_work=False)

    def worker(stage):
        nonlocal generation
        db = Store(service.store.directory)
        remote = service.destination.fork() if stage == "upload" and hasattr(service.destination, "fork") else service.destination
        task = Service(service.config, db, service.source, remote, service.stop, service.control, stage=stage)
        task.pipeline_stop = finished
        task.cache_guard = service.cache_guard
        cleanup_attempted = set()
        failed, cleaned = set(), set()
        inventory, verified = None, False
        retry_at = 0
        try:
            while not finished.is_set():
                with condition:
                    observed = generation
                    idle[stage] = None
                row = None
                changed = False
                try:
                    task.boundary()
                    if task.control and task.control.draining():
                        with condition:
                            if stage not in halted:
                                task.report("draining", "本路当前文件已结束，等待另一条传输停止", current_file=None,
                                            current_file_id=None, current_bytes=0, current_total_bytes=0, issue=None)
                                halted.add(stage)
                            condition.notify_all()
                            # Keep this worker available if the user cancels the drain
                            # while the other worker is still transferring its file.
                            condition.wait(.15)
                        continue
                    with condition:
                        resumed = stage in halted
                        if resumed:
                            halted.remove(stage)
                            generation += 1
                            condition.notify_all()
                    if resumed:
                        task.report("idle", "已取消等待，继续同步", issue=None)
                    require_cache(db)
                    if time.monotonic() < retry_at:
                        pass
                    elif stage == "import":
                        observed_inventory = db.get_meta("status", {}).get("inventory", {})
                        if verified and not observed_inventory.get("verified") and observed_inventory.get("invalidated_reason"):
                            # A reconnect or a recording-state change starts the next fixed batch,
                            # even while the previous batch is still uploading remotely.
                            inventory, verified = None, False
                        if inventory is None:
                            inventory = task.prepare_imports()
                            changed = True
                        known = {r["asset_id"]: r for r in task.rows()}
                        row = next((known[a.identity] for a in inventory["assets"]
                                    if a.identity in known and known[a.identity]["state"] in ("pending", "missing")
                                    and known[a.identity]["id"] not in failed), None)
                        if row:
                            if task.control:
                                task.control.refresh()
                                if task.control.draining():
                                    continue
                            task.import_asset(row)
                            changed, verified = True, False
                            task.cleanup_groups(cleanup_attempted)
                        elif not verified:
                            task.cleanup_groups(cleanup_attempted)
                            verified = task.verify_imports(inventory)
                            if not verified:
                                inventory = None
                                retry_at = time.monotonic() + service.config.backup["retry_seconds"]
                    else:
                        # Cleanup failure never changes the archived state or reuploads the file.
                        for saved in task.rows():
                            if saved["state"] == "uploaded" and saved["id"] not in cleaned:
                                task.clean_cache(saved)
                                cleaned.add(saved["id"])
                        row = next((r for r in task.rows() if r["state"] in ("cached", "staged", "publishing")
                                    and r["id"] not in failed), None)
                        if row:
                            if task.control:
                                task.control.refresh()
                                if task.control.draining():
                                    continue
                            task.observe_destination()
                            task.boundary()
                            task.upload_cached(row)
                            changed = True
                            task.report("idle", "等待完整缓存", current_file=None, current_file_id=None,
                                        current_bytes=0, current_total_bytes=0, issue=None)
                        else:
                            task.report("idle", "可用缓存已处理，等待新素材", current_file=None,
                                        current_file_id=None, current_bytes=0, current_total_bytes=0, issue=None)
                except Cancelled:
                    raise
                except CacheFull as exc:
                    task.report("waiting_cache", str(exc), issue={"code": "cache_capacity", "message": str(exc),
                                "action": "等待上传释放缓存，或调整缓存额度和磁盘预留空间", "scope": "cache", "stage": stage})
                except (Reconfigure, SyncDisabled):
                    raise
                except (BackupError, OSError, ValueError) as exc:
                    message = remote.redact(str(exc)) if hasattr(remote, "redact") else str(exc)
                    issue = describe(exc, stage)
                    issue["message"] = message
                    if row:
                        db.update(row["id"], error=message, error_stage=stage, attempts=row["attempts"] + 1)
                    db.set_meta("last_error", {"time": now(), "file": db.asset(row).path if row else None,
                                               "message": message, "stage": stage})
                    if isinstance(exc, (CacheUnavailable, OSError)):
                        phase = "waiting_cache"
                    elif stage == "import":
                        phase = "waiting_source" if row is None or isinstance(exc, SourceBlocked) else "failed"
                    else:
                        phase = "waiting_destination" if issue["scope"] == "destination" else "failed"
                    task.report(phase, message, issue=issue)
                    if row and issue["scope"] == "file":
                        failed.add(row["id"])
                        changed = True
                    else:
                        retry_at = time.monotonic() + service.config.backup["retry_seconds"]
                finally:
                    if stage == "import":
                        db.status(source_work=False)
                with condition:
                    if changed:
                        generation += 1
                        condition.notify_all()
                        continue
                    idle[stage] = observed
                    condition.notify_all()
                    condition.wait(.15)
        except Reconfigure as exc:
            # Structural edits wait for the other worker's current file to commit.
            with condition:
                errors.append(exc)
                halted.add(stage)
                condition.notify_all()
            task.report("applying_config", str(exc), issue=None)
        except BaseException as exc:
            with condition:
                errors.append(exc)
                finished.set()
                condition.notify_all()
            task.report("paused" if isinstance(exc, (Cancelled, SyncDisabled, Reconfigure)) else "failed",
                        str(exc), issue=None, current_bytes=0, speed_bytes_per_second=0)
        finally:
            if stage == "import":
                db.status(source_work=False)
            if remote is not service.destination:
                remote.close()
            db.close()

    def guarded_worker(stage):
        try:
            worker(stage)
        except BaseException as exc:
            # Startup failures (including opening SQLite) must wake the coordinator too.
            with condition:
                errors.append(exc)
                finished.set()
                condition.notify_all()

    threads = [threading.Thread(target=guarded_worker, args=(stage,), name=stage + "-worker") for stage in idle]
    for thread in threads:
        thread.start()
    try:
        with condition:
            while not finished.is_set():
                # Empty-queue observations must include the latest cache commit.
                if all(name in halted or value == generation for name, value in idle.items()):
                    finished.set()
                    condition.notify_all()
                    break
                condition.wait(.15)
    finally:
        finished.set()
        for thread in threads:
            thread.join()
    if errors:
        raise errors[0]
    service.store.status(transfers_finished_at=now())
