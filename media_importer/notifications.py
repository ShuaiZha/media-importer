"""Durable, best-effort outgoing notifications, independent from transfer workers."""
import hashlib
import copy
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .adapters import notification_adapter
from .model import BackupError, ConfigError


DELIVERY_ERRORS = {
    "notification_network": "通知服务暂时不可达，将自动重试。",
    "notification_rate_limited": "通知服务限制发送频率，将稍后重试。",
    "notification_authentication": "应用认证失败，请检查 App ID 和 App Secret。",
    "notification_permission": "应用缺少发送消息权限，请开通以应用身份发消息权限并发布版本。",
    "notification_recipient": "接收人不可用，请检查账号邮箱或当前应用的 Open ID，确认接收人在应用可用范围内，且未禁止机器人单聊。",
    "notification_bot_unavailable": "应用机器人不可用，请启用机器人能力并发布版本。",
    "notification_ip_restricted": "应用 IP 白名单拒绝请求，请检查白名单与当前出口地址。",
    "notification_quota": "应用消息额度不足，请在飞书管理后台检查消息用量与额度。",
    "notification_rejected": "应用拒绝了通知，请检查机器人、消息权限及应用发布状态。",
    "notification_response": "通知服务返回了无法确认成功的响应，将稍后重试。",
    "notification_credentials": "请在 .env 中检查飞书应用凭据与 FEISHU_OWNER_OPEN_ID，修改后重新创建容器。",
    "notification_internal": "通知暂时无法发送，将稍后重试；素材传输不受影响。",
    "notification_card_unavailable": "原进度卡已撤回或超过可更新期限，下次进度通知将使用新卡片。",
}
PROBLEMS = {
    "network": ("连接暂时不可用", "检查网络或设备连接；程序会自动重试，已保存的任务和完整缓存会保留；中断文件从头重传。"),
    "authentication": ("目的地认证失败", "检查目的地账号和密码，修改后测试连接；完整缓存保留。"),
    "permission": ("目的地权限不足", "检查共享目录的访问、写入和改名权限；完整缓存保留。"),
    "destination_path": ("目的地目录不可用", "检查共享名和目标目录，修改后测试连接。"),
    "destination_full": ("目的地空间不足", "释放目的地空间或调整配额；完整缓存会保留，程序会自动重试。"),
    "cache_capacity": ("缓存空间或额度不足", "等待上传释放空间，或调整缓存额度及磁盘预留；暂停新导入。"),
    "cache_unavailable": ("缓存盘不可用", "连接原缓存盘并检查挂载和读写权限；两路传输会等待恢复。"),
    "source_state": ("源设备暂时无法读取", "等待录像结束、设备就绪或温度告警解除；完整缓存可继续上传。"),
    "source_file": ("源文件导入失败", "检查源设备和存储卡；失败文件会自动重试，其他可处理文件继续。"),
    "file": ("文件传输失败", "失败文件会自动重试，其他可处理文件继续；发送“状态”查看原因，详细记录可通过本机 status --json 查看。"),
    "source_cleanup": ("已导入，原片清理待处理", "完整缓存继续上传；检查源设备连接，发送“状态”查看清理原因。"),
    "cache_cleanup": ("已归档，缓存清理待处理", "已归档素材不会重复上传；检查缓存盘连接和写入权限。"),
}


def _stamp(seconds=None):
    return datetime.fromtimestamp(time.time() if seconds is None else seconds, timezone.utc).isoformat(timespec="seconds")


def _safe_failure(exc):
    code = "notification_credentials" if isinstance(exc, ConfigError) else getattr(exc, "code", None)
    if code not in DELIVERY_ERRORS:
        code = "notification_internal"
    return code, DELIVERY_ERRORS[code]


def _meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _put_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))


@contextmanager
def _database(directory):
    db = sqlite3.connect(str(Path(directory) / "state.sqlite3"), timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("""CREATE TABLE IF NOT EXISTS notification_events (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, batch_id TEXT,
            payload TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
            sent_at REAL
        )""")
        db.execute("INSERT OR IGNORE INTO meta (key,value) VALUES (?,?)",
                   ("notification_instance_id", json.dumps(uuid.uuid4().hex)))
        db.commit()
        yield db
    finally:
        db.close()


def _delivery(directory, **updates):
    with _database(directory) as db, db:
        value = _meta(db, "notification_delivery", {})
        value.update(updates)
        _put_meta(db, "notification_delivery", value)


def send_test(settings, directory):
    """Explicit user-triggered message; works even when automatic sync is off."""
    try:
        item = settings.read()
        if item.get("error") or not item.get("config"):
            raise ConfigError(DELIVERY_ERRORS["notification_credentials"])
        options = item["config"]["notifications"]
        adapter = notification_adapter("feishu")
        from .bot_cards import event_card
        adapter.send_card(settings.notification_credentials(), event_card(
            'test', '素材导入助手 · 测试通知',
            '开启通知和同步后，进度将定时更新到同一张卡片，归档完成和失败会单独提醒。', {},
            keyword=options.get('keyword', ''), updated_at=_stamp()))
    except Exception as exc:
        code, message = _safe_failure(exc)
        _delivery(directory, state="error", last_event="test", last_error=message, last_error_at=_stamp(), error_code=code, next_retry_at=None)
        # The HTTP layer may expose str(exc); the original exception must never escape.
        raise BackupError(message) from None
    _delivery(directory, state="sent", last_event="test", last_sent_at=_stamp(), last_error=None,
              error_code=None, next_retry_at=None)


def _size(value):
    value = max(0, value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def _duration(seconds):
    if seconds is None:
        return "待估算"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"


def _digest(value):
    # Only compact metadata is hashed; media is never read by the notification worker.
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


class NotificationWorker:
    def __init__(self, directory, settings, stop, *, sender=None, updater=None, snapshotter=None, clock=None):
        self.directory, self.settings, self.stop = Path(directory), settings, stop
        self.sender, self.snapshotter, self.clock = sender, snapshotter, clock or time.time
        self.updater = updater

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                code, message = _safe_failure(exc)
                try:
                    _delivery(self.directory, state="error", last_error=message, last_error_at=_stamp(), error_code=code)
                except Exception:
                    pass  # An unavailable state disk must not kill the notification or transfer worker.
            self.stop.wait(5)

    def _batches(self, db, destination, source):
        result = {}
        for row in db.execute("""SELECT b.id batch_id,b.created_at batch_created,b.imported_at imported_at,f.*
                FROM batches b JOIN batch_files bf ON bf.batch_id=b.id
                JOIN files f ON f.id=bf.file_id WHERE b.destination=? ORDER BY b.created_at""", (destination,)):
            asset = json.loads(row["asset"])
            if asset.get("serial") != source["serial"] or asset.get("source_type") != source["type"]:
                continue
            result.setdefault(row["batch_id"], []).append(dict(row))
        return result

    @staticmethod
    def _complete(rows):
        return bool(rows) and all(row["state"] == "uploaded" for row in rows)

    @staticmethod
    def _cleaning(view, batch_id):
        return (view.get("batch", {}).get("id") == batch_id and
                view.get("status", {}).get("phase") in ("checking_cleanup", "cleaning_source"))

    def _enqueue(self, db, key, kind, scope, title, text, stamp, batch_id=None, state="pending"):
        db.execute("""INSERT OR IGNORE INTO notification_events
            (id,kind,scope,batch_id,payload,state,created_at) VALUES (?,?,?,?,?,?,?)""",
            (key, kind, scope, batch_id, json.dumps({"title": title, "text": text}, ensure_ascii=False), state, stamp))

    @staticmethod
    def _reconcile_delivery(db, options, stamp):
        pending = db.execute("SELECT COUNT(*), MIN(next_attempt_at) FROM notification_events WHERE state='pending'").fetchone()
        delivery = _meta(db, "notification_delivery", {})
        delivery.update(enabled=bool(options["enabled"]), sync_enabled=True, pending_count=pending[0],
                        next_retry_at=_stamp(pending[1]) if pending[0] and pending[1] > stamp else None)
        if not pending[0] and delivery.get("state") in (None, "disabled", "retrying"):
            delivery["state"] = "idle"
        _put_meta(db, "notification_delivery", delivery)

    def _baseline(self, db, batches, scope, stamp):
        for batch_id, rows in batches.items():
            if rows and rows[0].get("imported_at"):
                self._enqueue(db, "imported:" + scope + ":" + batch_id, "imported", scope, "", "", stamp,
                              batch_id=batch_id, state="suppressed")
            if self._complete(rows):
                self._enqueue(db, "completed:" + scope + ":" + batch_id, "completed", scope, "", "", stamp,
                              batch_id=batch_id, state="suppressed")

    @staticmethod
    def _progress(view):
        batch, lines = view["batch"], []
        signature = [batch.get("id")]
        for name, label, count, percent in (("import", "导入", "imported_files", "import_percent"),
                                           ("upload", "上传", "completed_files", "percent")):
            stage, queue = view.get("stages", {}).get(name, {}), view.get("queues", {}).get(name, {})
            signature.extend((batch.get(count, 0), stage.get("current_file_id"), stage.get("current_bytes", 0),
                              round(batch.get(percent, 0), 4)))
            lines.append(f"{label}：{batch.get(count, 0)}/{batch.get('total_files', 0)} 文件，"
                         f"{min(100, batch.get(percent, 0)):.1f}%")
            if view.get("controls", {}).get(name, {}).get("requested_stop"):
                lines.append("已暂停；任务与完整缓存保留；中断文件重新传输")
            else:
                lines.append(f"速度 {_size(stage.get('speed_bytes_per_second', 0))}/s，"
                             f"预计剩余 {_duration(queue.get('eta_seconds'))}")
        lines.append("上传百分比包含临时文件中的数据；正式归档完成会另行通知。")
        return _digest(signature), "\n".join(lines)

    @staticmethod
    def _completion(rows, view):
        total = sum(json.loads(row["asset"])["size"] for row in rows)
        policy = view.get("policy", {})
        live_files = {f["id"]: f for f in view.get("files", [])}
        cache_pending = sum(bool(row.get("cache_cleanup_error") or live_files.get(row["id"], {}).get("cache_available")) for row in rows)
        source_pending = sum(row["cleanup"] not in ("deleted", "absent") for row in rows)
        source = (f"原片清理待处理 {source_pending} 文件" if source_pending else "原片清理完成") if policy.get("delete_source") else "原片保留"
        cache = (f"缓存清理待处理 {cache_pending} 文件" if cache_pending else "缓存清理完成") if policy.get("cleanup_cache") else "缓存保留"
        try:
            started = min(datetime.fromisoformat(row["batch_created"]).timestamp() for row in rows)
            finished = max(datetime.fromisoformat(row["updated_at"]).timestamp() for row in rows)
            duration = _duration(finished - started)
        except (ValueError, TypeError):
            duration = "未知"
        return f"已正式归档 {len(rows)} 个文件，共 {_size(total)}。\n批次历时：{duration}\n{source}\n{cache}"

    @staticmethod
    def _card_key(scope, view, credentials):
        # The destination, batch, application and recipient all own this message.
        # Secret rotation must not create a new card; changing the recipient must.
        return 'notification_progress_card:' + _digest([
            scope, view.get('batch', {}).get('id'), credentials['app_id'],
            credentials['receive_id_type'], credentials['receive_id']])

    def _card_signature(self, view, options):
        return _digest([self._progress(view)[0], view.get('running'),
                        view.get('configuration', {}).get('sync_enabled'), options['enabled'],
                        options['progress_interval_seconds'], options.get('keyword'),
                        view.get('unplug'), view.get('attention'),
                        [(name, stage.get('phase'), stage.get('message')) for name, stage in view.get('stages', {}).items()],
                        view.get('controls')])

    def _send_progress(self, db, scope, view, options, stamp, *, create=True):
        from .bot_cards import progress_card
        credentials = self.settings.notification_credentials()
        key = self._card_key(scope, view, credentials)
        pointer = _meta(db, key, {})
        # A start card can precede the first camera scan. Adopt that message when
        # its batch becomes known, rather than leaving a second orphan card.
        if not pointer and view.get('batch', {}).get('id'):
            startup = {**view, 'batch': {}}
            startup_key = self._card_key(scope, startup, credentials)
            pending = _meta(db, startup_key, {})
            if (pending.get('message_id') or pending.get('creation_id')) and not pending.get('finished'):
                pointer = pending
                with db:
                    _put_meta(db, key, pointer)
                    db.execute('DELETE FROM meta WHERE key=?', (startup_key,))
        signature = self._card_signature(view, options)
        # A completed batch card is a final result, not a monitor for later batches.
        if pointer.get('finished'):
            return
        if not create and (not pointer.get('message_id') or pointer.get('signature') == signature
                           or pointer.get('retry_at', 0) > stamp):
            return
        content = progress_card(view, options['progress_interval_seconds'], options.get('keyword', ''),
                                _stamp(stamp), notifications_enabled=options['enabled'])
        adapter = notification_adapter('feishu')
        try:
            if pointer.get('message_id'):
                try:
                    (self.updater or adapter.update_card)(credentials, pointer['message_id'], content)
                except Exception as exc:
                    # Only an explicit obsolete/recalled response permits replacement.
                    # Authentication, rate limits and uncertain PATCH results never POST.
                    if getattr(exc, 'code', None) != 'notification_card_unavailable':
                        raise
                    pointer.update(message_id=None, generation=pointer.get('generation', 0) + 1)
                    pointer.pop('creation_id', None)
                    with db:
                        _put_meta(db, key, pointer)
                    if not create:
                        return
            if not pointer.get('message_id'):
                if not create:
                    return
                identity = pointer.setdefault('creation_id', _meta(db, 'notification_instance_id') + ':' + key + ':' + str(pointer.get('generation', 0)))
                pointer['message_id'] = (self.sender or adapter.send_card)(credentials, content, event_id=identity)
                pointer['created_at'] = stamp
            batch = view.get('batch', {})
            pointer.update(signature=signature, updated_at=stamp, retry_at=0, attempts=0,
                           finished=bool(batch.get('total_files') and batch.get('completed_files') == batch['total_files']))
            with db:
                _put_meta(db, key, pointer)
        except Exception:
            pointer['attempts'] = pointer.get('attempts', 0) + 1
            pointer['retry_at'] = stamp + min(1800, 30 * 2 ** min(pointer['attempts'] - 1, 6))
            with db:
                _put_meta(db, key, pointer)
            raise

    def _refresh_existing_progress(self, db, scope, view, options, stamp):
        """End/wait/stop states should not leave an old card appearing to transfer.

        This path only PATCHes an existing message; disabling sync or notifications
        never creates a new notification. It never changes transfer records.
        """
        try:
            self._send_progress(db, scope, view, options, stamp, create=False)
        except Exception as exc:
            code, message = _safe_failure(exc)
            with db:
                delivery = _meta(db, 'notification_delivery', {})
                delivery.update(last_error=message, error_code=code, last_error_at=_stamp(stamp))
                _put_meta(db, 'notification_delivery', delivery)

    @staticmethod
    def _completed_view(view, rows):
        result = copy.deepcopy(view)
        result['batch'] = {'id': rows[0]['batch_id'], 'scanned': True, 'total_files': len(rows),
                           'total_bytes': sum(json.loads(r['asset'])['size'] for r in rows),
                           'completed_files': len(rows), 'imported_files': len(rows),
                           'percent': 100, 'import_percent': 100}
        result['stages'] = {name: {'phase': 'completed'} for name in ('import', 'upload')}
        result['queues'] = {name: {'pending_files': 0} for name in ('import', 'upload')}
        return result

    @staticmethod
    def _problems(view):
        found = {}
        controls = view.get("controls", {})
        def active(stage):
            return not controls.get(stage, {}).get("requested_stop", False)
        def add(code, scope, stage):
            if code not in PROBLEMS:
                code = "file"
            if active(stage):
                found[f"{scope}:{stage}:{code}"] = {"code": code, "scope": scope, "stage": stage}
        for stage, progress in view.get("stages", {}).items():
            issue = progress.get("issue") or {}
            phase = progress.get("phase", "")
            if (phase == "failed" or phase.startswith("waiting")) and issue:
                add(issue.get("code"), issue.get("scope", stage), stage)
        for name, stage in (("source", "import"), ("destination", "upload")):
            health = view.get("health", {}).get(name, {})
            pending = view.get("queues", {}).get(stage, {}).get("pending_files", 0)
            if pending and health.get("online") is False and not health.get("stale"):
                add((health.get("issue") or {}).get("code", "network"), name, stage)
        if (view.get("health", {}).get("cache", {}).get("available") is False
                and view.get("totals", {}).get("pending_files", 0)):
            for stage in ("import", "upload"):
                if active(stage):
                    add("cache_unavailable", "cache", stage)
                    break
        for item in view.get("files", []):
            if item.get("error") and item.get("state") != "uploaded":
                stage = item.get("error_stage") or "import"
                progress = view.get("stages", {}).get(stage, {})
                retrying = (progress.get("phase") in ("downloading", "uploading", "publishing") and
                            (item.get("id") == progress.get("current_file_id") or
                             item.get("current") and item.get("stage") == stage))
                # Prefer the classified stage error; a generic file alert would duplicate it.
                if not retrying and not any(p["stage"] == stage for p in found.values()):
                    add("source_file" if stage == "import" else "file", "file", stage)
            if item.get("source_cleanup_error") and view.get("policy", {}).get("delete_source"):
                add("source_cleanup", "source", "import")
            if item.get("state") == "uploaded":
                if item.get("cache_cleanup_error") and view.get("policy", {}).get("cleanup_cache"):
                    add("cache_cleanup", "cache", "upload")
                if item.get("cleanup") in ("blocked", "changed") and view.get("policy", {}).get("delete_source"):
                    add("source_cleanup", "source", "import")
        return found

    def tick(self, view=None):
        if self.stop.is_set() or not (self.directory / "state.sqlite3").is_file():
            return
        item = self.settings.read()
        if item.get("error") or not item.get("config"):
            return
        values, stamp = item["config"], self.clock()
        options = values["notifications"]
        from .config import Config
        from .status import snapshot
        destination = Config.from_values(values).destination_id
        scope = _digest([destination, values["source"]["type"], values["source"]["serial"]])
        view = view if view is not None else (self.snapshotter or snapshot)(self.directory, self.settings)
        view = {**view, 'configuration': {**view.get('configuration', {}), 'sync_enabled': values['backup']['enabled']}}
        enabled = bool(options["enabled"] and values["backup"]["enabled"])
        with _database(self.directory) as db:
            state = _meta(db, "notification_state", {})
            batches = self._batches(db, destination, values["source"])
            if state.get("scope") != scope:
                state = {"scope": scope, "armed_at": None, "problems": {}}
            resumed = enabled and state.get('armed_at') is None
            with db:
                db.execute("UPDATE notification_events SET state='cancelled' WHERE scope<>? AND state='pending'", (scope,))
                if not enabled or not state.get("initialized"):
                    self._baseline(db, batches, scope, stamp)
                if not enabled or state.get("armed_at") is None:
                    state.update(armed_at=stamp if enabled else None, progress_at=stamp,
                                 progress_signature=self._progress(view)[0], problems={})
                if resumed:
                    self._enqueue(db, 'started:' + scope + ':' + str(state['armed_at']), 'started', scope,
                                  '素材导入助手 · 传输进度', '', stamp)
                state["initialized"] = True
                if not enabled:
                    db.execute("UPDATE notification_events SET state='cancelled' WHERE state='pending'")
                    _put_meta(db, "notification_state", state)
                    delivery = _meta(db, "notification_delivery", {})
                    # Retain an explicit test result while automatic notification is disabled.
                    delivery.update(enabled=bool(options["enabled"]), sync_enabled=bool(values["backup"]["enabled"]), pending_count=0)
                    if delivery.get("last_event") != "test":
                        delivery.update(state="disabled", next_retry_at=None)
                    _put_meta(db, "notification_delivery", delivery)
                    db.commit()
                    self._refresh_existing_progress(db, scope, view, options, stamp)
                    return
                if (not view.get("running") or view.get("configuration", {}).get("pending")
                        or view.get("status", {}).get("phase") == "applying_config"):
                    _put_meta(db, "notification_state", state)
                    self._reconcile_delivery(db, options, stamp)
                    return
                for batch_id, rows in batches.items():
                    if self._complete(rows) and not self._cleaning(view, batch_id):
                        self._enqueue(db, "completed:" + scope + ":" + batch_id, "completed", scope,
                                      "素材导入助手 · 归档完成", self._completion(rows, view), stamp, batch_id=batch_id)
                    if rows and rows[0].get("imported_at"):
                        self._enqueue(db, "imported:" + scope + ":" + batch_id, "imported", scope,
                                      "素材导入助手 · 相机导入完成",
                                      "本批次的主文件及附属文件均已完整保存。是否可以拔相机，请以当前相机状态为准。", stamp, batch_id=batch_id)
                active = any(stage.get("phase") in ("downloading", "uploading") and
                             not view.get("controls", {}).get(name, {}).get("requested_stop", False)
                             for name, stage in view.get("stages", {}).items())
                if not active or self._complete(batches.get(view["batch"].get("id"), [])):
                    db.execute("UPDATE notification_events SET state='cancelled' WHERE kind='progress' AND state='pending'")
                db.execute("UPDATE notification_events SET state='cancelled' WHERE kind='progress' AND state='pending' AND batch_id IS NOT ?",
                           (view["batch"].get("id"),))
                signature, text = self._progress(view)
                if (active and view["batch"].get("total_files", 0) > 0
                        and stamp - state.get("progress_at", stamp) >= options["progress_interval_seconds"]
                        and signature != state.get("progress_signature")):
                    db.execute("UPDATE notification_events SET state='cancelled' WHERE kind='progress' AND state='pending'")
                    self._enqueue(db, "progress:" + scope + ":" + str(stamp), "progress", scope,
                                  "素材导入助手 · 传输进度", text, stamp, batch_id=view["batch"].get("id"))
                    state.update(progress_at=stamp, progress_signature=signature)
                observed = self._problems(view)
                previous = state.get("problems", {})
                current = {}
                for key, problem in observed.items():
                    started = previous.get(key, {}).get("since", stamp)
                    current[key] = {"since": started}
                    delay = 120 if problem["code"] in ("network", "source_state") else 0
                    if stamp - started >= delay:
                        title, action = PROBLEMS[problem["code"]]
                        self._enqueue(db, "failed:" + scope + ":" + key + ":" + str(started), "failed", scope,
                                      "素材导入助手 · " + title, action, stamp)
                # A recovered fault should not be delivered later from an old retry queue.
                for key, episode in previous.items():
                    if key not in current:
                        db.execute("UPDATE notification_events SET state='cancelled' WHERE id=? AND state='pending'",
                                   ("failed:" + scope + ":" + key + ":" + str(episode["since"]),))
                state["problems"] = current
                _put_meta(db, "notification_state", state)
                self._reconcile_delivery(db, options, stamp)
            self._send_pending(db, scope, options, stamp, view)
            # Promote an initial scanning card as soon as a batch is known,
            # without waiting for the ordinary progress interval.
            if view['batch'].get('id'):
                startup_key = self._card_key(scope, {**view, 'batch': {}}, self.settings.notification_credentials())
                if _meta(db, startup_key, {}).get('message_id'):
                    self._refresh_existing_progress(db, scope, view, options, stamp)
            if (not self._complete(batches.get(view['batch'].get('id'), [])) and
                    (resumed or not active or any(stage.get('phase', '').startswith('waiting') or stage.get('phase') == 'failed'
                                       for stage in view.get('stages', {}).values()))):
                self._refresh_existing_progress(db, scope, view, options, stamp)
            for rows in batches.values():
                if self._complete(rows):
                    self._refresh_existing_progress(db, scope, self._completed_view(view, rows), options, stamp)

    def _send_pending(self, db, scope, options, stamp, view):
        # Send at most one message per tick; failure backoff is independent for each event.
        candidates = db.execute("""SELECT * FROM notification_events WHERE state='pending' AND scope=?
            AND next_attempt_at<=? ORDER BY CASE kind WHEN 'started' THEN -1 WHEN 'failed' THEN 0 WHEN 'imported' THEN 1 WHEN 'completed' THEN 2 ELSE 3 END, created_at""",
                         (scope, stamp)).fetchall()
        if not candidates or self.stop.is_set():
            return
        latest = self.settings.read().get("config") or {}
        if not latest.get("backup", {}).get("enabled") or not latest.get("notifications", {}).get("enabled"):
            return
        from .config import Config
        latest_destination = Config.from_values(latest).destination_id
        if scope != _digest([latest_destination, latest["source"]["type"], latest["source"]["serial"]]):
            return
        options = latest["notifications"]
        current_batches = None
        current = []
        for row in candidates:
            payload = json.loads(row["payload"])
            if row["kind"] == "completed":
                if current_batches is None:
                    current_batches = self._batches(db, latest_destination, latest["source"])
                current = current_batches.get(row["batch_id"], [])
                if not self._complete(current) or self._cleaning(view, row["batch_id"]):
                    with db:
                        db.execute("UPDATE notification_events SET next_attempt_at=? WHERE id=?", (stamp + 5, row["id"]))
                        self._reconcile_delivery(db, options, stamp)
                    continue
                payload["text"] = self._completion(current, view)
            elif row["kind"] in ("progress", "started"):
                payload["text"] = self._progress(view)[1]
            break
        else:
            return
        try:
            adapter = notification_adapter("feishu")
            if row['kind'] in ('progress', 'started'):
                progress_view = view
                if row['kind'] == 'started':
                    key = self._card_key(scope, view, self.settings.notification_credentials())
                    if _meta(db, key, {}).get('finished'):
                        # The previous batch is final; this start awaits a new scan.
                        progress_view = {**view, 'batch': {'scanned': False}, 'stages': {}}
                self._send_progress(db, scope, progress_view, options, stamp)
            else:
                from .bot_cards import event_card
                from .paths import archive_location
                from pathlib import PurePosixPath
                locations = sorted({archive_location(latest['destination'], str(PurePosixPath(r['remote_path']).parent))
                                    for r in current if r.get('remote_path')})
                content = event_card(row['kind'], payload['title'], payload['text'], view,
                                     keyword=options.get('keyword', ''), updated_at=_stamp(stamp), locations=locations)
                (self.sender or adapter.send_card)(self.settings.notification_credentials(), content,
                                                  event_id=_meta(db, "notification_instance_id") + ":" + row["id"])
        except Exception as exc:
            code, message = _safe_failure(exc)
            next_retry_at = stamp + min(1800, 30 * 2 ** min(row["attempts"], 6))
            with db:
                db.execute("UPDATE notification_events SET attempts=attempts+1,next_attempt_at=? WHERE id=?", (next_retry_at, row["id"]))
                delivery = _meta(db, "notification_delivery", {})
                delivery.update(state="retrying", enabled=True, sync_enabled=True, last_event=row["kind"], last_error=message, last_error_at=_stamp(stamp),
                                error_code=code, next_retry_at=_stamp(next_retry_at), pending_count=db.execute("SELECT COUNT(*) FROM notification_events WHERE state='pending'").fetchone()[0])
                _put_meta(db, "notification_delivery", delivery)
            return
        with db:
            db.execute("UPDATE notification_events SET state='sent',attempts=attempts+1,sent_at=? WHERE id=?", (stamp, row["id"]))
            delivery = _meta(db, "notification_delivery", {})
            delivery.update(state="sent", enabled=True, sync_enabled=True, last_event=row["kind"], last_sent_at=_stamp(stamp),
                            last_error=None, error_code=None, next_retry_at=None,
                            pending_count=db.execute("SELECT COUNT(*) FROM notification_events WHERE state='pending'").fetchone()[0])
            _put_meta(db, "notification_delivery", delivery)
