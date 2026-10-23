"""Independent single-file workers; SQLite and the bounded cache are the handoff."""
import threading
import time
import os
import errno

from .issues import describe
from .model import BackupError, SourceBlocked, CacheUnavailable, CacheFileError, TransferStalled, Cancelled, now
from .liveness import Activity, CombinedStop
from .runtime import Reconfigure, SyncDisabled
from .storage import require_cache
from .store import Store
from .source_connection import ConfirmationRequired


def transfer(service):
    from .service import Service, CacheFull

    finished = threading.Event()
    condition = threading.Condition()
    generation = 0
    idle = {"import": None, "upload": None}
    errors = []
    halted = set()
    rate = service.config.limits['upload_mib_per_second']
    base = max(120, service.config.source.get('timeout_seconds', 20) * service.config.source.get('request_attempts', 3) + 30)
    activity = Activity(getattr(service, 'activity_timeouts', {'import': base, 'upload': max(base, 1 / rate + 30) if rate else base}))
    attempt_stop = CombinedStop(finished, service.stop, service.control.transfer_stop if service.control else None)
    cancel_grace = getattr(service, 'cancel_grace', 30)
    service.store.status(phase="transferring", message="两路队列独立运行", source_work=False)

    def worker(stage):
        nonlocal generation
        activity.begin(stage, 'starting')
        db = Store(service.store.directory)
        remote = service.destination.fork(stop=attempt_stop) if hasattr(service.destination, "fork") else service.destination
        source_interrupt = getattr(service.control, 'source_interrupt', None)
        source_stop = CombinedStop(attempt_stop, source_interrupt)
        source = service.source.fork(stop=source_stop) if stage == 'import' and hasattr(service.source, 'fork') else service.source
        task = service.transfer_task(db, source, remote, stage)
        task.pipeline_stop = finished
        task.cache_guard = service.cache_guard
        task.activity = activity
        if stage == 'import':
            source.on_activity = lambda: activity.touch(stage)
        remote.on_activity = lambda: activity.touch(stage)
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
                    activity.begin(stage)
                    if stage == 'import' and task.control and hasattr(task.control, 'settings'):
                        if source_interrupt and source_interrupt.is_set():
                            source_interrupt.clear()
                            inventory, verified = None, False
                        from .camera_coordinator import dispatch
                        if dispatch(task, task.control.settings, import_lane=True).handled:
                            inventory, verified = None, False
                            changed = True
                            continue
                    task.boundary()
                    if task.control and task.control.draining():
                        with condition:
                            if stage not in halted:
                                task.report("draining", "本路当前文件已结束，等待另一条传输停止", current_file=None,
                                            current_file_id=None, current_bytes=0, current_total_bytes=0, issue=None)
                                halted.add(stage)
                            condition.notify_all()
                            activity.idle(stage)
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
                        task.report("idle", "已取消等待，继续备份", issue=None)
                    require_cache(db)
                    if time.monotonic() < retry_at:
                        pass
                    elif stage == "import":
                        observed_inventory = db.get_meta("status", {}).get("inventory", {})
                        if (task.tracks_source_inventory and verified and
                                not observed_inventory.get("verified") and observed_inventory.get("invalidated_reason")):
                            # A reconnect or a recording-state change starts the next fixed batch,
                            # even while the previous batch is still uploading remotely.
                            inventory, verified = None, False
                        if inventory is None:
                            inventory = task.prepare_imports()
                            changed = True
                        known = {r["asset_id"]: r for r in task.rows()}
                        row = next((known[a.identity] for a in inventory["assets"]
                                    if a.identity in known and known[a.identity]["state"] in ("pending", "missing")
                                    and known[a.identity]["id"] not in failed
                                    and known[a.identity].get('retry_at', 0) <= time.time()), None)
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
                        work = task.rows(work=True)
                        for saved in work:
                            if saved["state"] == "uploaded" and saved["id"] not in cleaned:
                                if task.verify_archived(saved):
                                    if task.clean_cache(saved) is False:
                                        continue
                                cleaned.add(saved["id"])
                        row = next((r for r in work if r["state"] in ("cached", "staged", "publishing")
                                    and r["id"] not in failed and r.get('retry_at', 0) <= time.time()), None)
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
                    if stage == 'import' and source_interrupt and source_interrupt.is_set() and not attempt_stop.is_set():
                        source_interrupt.clear()
                        inventory, verified = None, False
                        task.report('waiting_source', '相机操作或连接变化，未完成导入已清理，上传继续',
                                    current_file=None, current_file_id=None, current_bytes=0, speed_bytes_per_second=0)
                        continue
                    raise
                except ConfirmationRequired as exc:
                    inventory, verified = None, False
                    task.report('waiting_confirmation', str(exc), issue=None, current_file=None,
                                current_file_id=None, current_bytes=0, current_total_bytes=0,
                                speed_bytes_per_second=0, eta_seconds=None)
                except CacheFull as exc:
                    task.report("waiting_cache", str(exc), issue={"code": "cache_capacity", "message": str(exc),
                                "action": "等待上传释放缓存，或调整缓存额度和磁盘预留空间", "scope": "cache", "stage": stage})
                except (Reconfigure, SyncDisabled):
                    raise
                except (BackupError, OSError, ValueError) as exc:
                    if row and isinstance(exc, OSError) and exc.errno in (
                            errno.ENOENT, errno.EACCES, errno.EPERM, errno.EISDIR, errno.ENOTDIR, errno.ELOOP):
                        try:
                            require_cache(db)
                        except CacheUnavailable as unavailable:
                            exc = unavailable
                        else:
                            exc = CacheFileError('此文件无法读取或写入，请检查文件权限；其他文件继续')
                    message = remote.redact(str(exc)) if hasattr(remote, "redact") else str(exc)
                    issue = describe(exc, stage)
                    issue["message"] = message
                    if row:
                        attempts = db.get(row['id'])['attempts'] + 1
                        delay = min(3600, service.config.backup['retry_seconds'] * 2 ** min(attempts - 1, 7))
                        db.update(row["id"], error=message, error_stage=stage, attempts=attempts,
                                  error_code=issue['code'], retry_at=time.time() + delay if issue['scope'] == 'file' else 0)
                    db.set_meta("last_error", {"time": now(), "file": db.asset(row).path if row else None,
                                               "message": message, "stage": stage})
                    if isinstance(exc, CacheFileError):
                        phase = 'failed'
                    elif isinstance(exc, (CacheUnavailable, OSError)):
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
                    activity.idle(stage)
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
                coordinated_stop = isinstance(exc, Cancelled) and finished.is_set() and not service.stop.is_set()
                if not coordinated_stop:
                    errors.append(exc)
                finished.set()
                condition.notify_all()
            if not coordinated_stop:
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

    threads = [threading.Thread(target=guarded_worker, args=(stage,), name=stage + "-worker", daemon=True) for stage in idle]
    for thread in threads:
        thread.start()
    try:
        published_at = 0
        with condition:
            while not finished.is_set():
                if attempt_stop.is_set():
                    errors.append(Cancelled('传输已取消，任务与完整缓存保留'))
                    finished.set()
                    condition.notify_all()
                    break
                stalled = activity.expired()
                if stalled:
                    errors.insert(0, TransferStalled('传输长时间无有效活动，已取消本次尝试并重建连接；完整缓存保留'))
                    for stage in stalled:
                        service.store.stage_status(stage, phase='waiting_retry', issue=describe(errors[0], stage),
                                                   speed_bytes_per_second=0, eta_seconds=None)
                    service.store.status(transfer_health=activity.snapshot())
                    finished.set()
                    condition.notify_all()
                    break
                if time.monotonic() - published_at >= 5:
                    service.store.status(transfer_health=activity.snapshot())
                    published_at = time.monotonic()
                # Empty-queue observations must include the latest cache commit.
                if all(name in halted or value == generation for name, value in idle.items()):
                    finished.set()
                    condition.notify_all()
                    break
                condition.wait(.15)
    finally:
        finished.set()
        deadline = time.monotonic() + cancel_grace
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            service.store.status(transfer_health=activity.snapshot(), phase='unresponsive',
                                 message='传输线程取消后仍未退出，正在重启服务；完整缓存保留')
            # Python cannot safely kill an arbitrary blocked thread. Leave
            # durable intents for startup recovery and let Compose restart.
            getattr(service, 'hard_exit', os._exit)(75)
            raise TransferStalled('传输线程无法退出，需要重新启动服务')
        service.store.status(transfer_health=activity.snapshot())
    if errors:
        raise errors[0]
    service.store.status(transfers_finished_at=now())
