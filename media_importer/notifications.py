"""Durable, best-effort outgoing notifications, independent from transfer workers."""
import copy
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .adapters import notification_adapter
from .model import BackupError, ConfigError
from .completion import read as read_results
from .outbox import _digest




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
    'transfer_stalled': ('传输等待重试', '正在重新建立连接'),
    'network': ('归档等待连接', '连接恢复后自动继续'),
    'source_network': ('导入等待连接', '相机连接恢复后自动继续'),
    'source_connection_mode': ('相机连接方式不可用', '请在相机连接设置中调整连接方式'),
    'authentication': ('归档暂停', 'SMB 身份验证失败\n请检查归档账号和密码'),
    'permission': ('归档暂停', 'SMB 权限不足\n请检查共享目录权限'),
    'destination_path': ('归档暂停', '共享目录不可用\n请检查共享名和归档目录'),
    'destination_identity': ('归档暂停', '存储身份不一致\n请核对归档位置'),
    'destination_full': ('归档暂停', '目的地空间不足\n请释放存储空间'),
    'cache_capacity': ('导入等待空间', '归档释放缓存后自动继续'),
    'cache_unavailable': ('传输暂停', '缓存盘不可用\n请连接原缓存盘'),
    'source_state': ('导入等待相机', '相机就绪后自动继续'),
    'source_file': ('文件导入等待重试', '其他文件继续处理'),
    'file': ('文件归档等待重试', '其他文件继续处理'),
    'source_cleanup': ('原片清理未完成', '请检查相机连接及副本核验结果'),
    'cache_cleanup': ('归档完成 · 缓存待清理', '请检查缓存盘写入权限'),
    'cache_verification': ('缓存核验未通过', '请连接原缓存盘并核验副本'),
}
# These conditions recover automatically; they remain visible on progress cards.
# A six-hour lack of progress still produces the existing backlog alert.
RETRY_ONLY = {'network', 'source_network', 'source_state', 'transfer_stalled', 'cache_capacity', 'source_file', 'file'}

BACKLOG_DELAY = 6 * 3600
CONNECTION_KINDS = ('connected', 'disconnected')


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
    db.execute("""INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value
        WHERE meta.value<>excluded.value""", (key, json.dumps(value, ensure_ascii=False)))




@contextmanager
def _database(directory):
    from .database import connect
    db = connect(directory, timeout=5)
    try:
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
            'test', '素材导入助手 · 通知测试',
            '通知通道已验证。备份开启后，进度按设置更新，完成与异常单独通知。', {},
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





class NotificationWorker:
    def __init__(self, directory, settings, stop, *, sender=None, updater=None, snapshotter=None, clock=None):
        self.directory, self.settings, self.stop = Path(directory), settings, stop
        self.settings.directory = self.directory
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

    def _results(self, db, values, destination):
        from .config import Config
        config = Config.from_values({**values, 'destination': {**values['destination'], 'archive_id': destination}})
        return read_results(db, config, selection=values['backup']['media_scope'])

    @staticmethod
    def _cleaning(view, batch_id):
        return bool(view.get('status', {}).get('source_work') or
                    view.get('status', {}).get('phase') in ('checking_cleanup', 'cleaning_source'))

    def _enqueue(self, db, key, kind, scope, title, text, stamp, batch_id=None, state="pending"):
        if batch_id:
            run = db.execute('SELECT 1 FROM transfer_runs WHERE id=?', (batch_id,)).fetchone()
            if not run:
                return  # A stale observer must not resurrect a closed round.
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

    def _baseline(self, db, reports, scope, stamp):
        for batch_id, report in reports.items():
            if report.get('imported_at'):
                self._enqueue(db, "imported:" + scope + ":" + batch_id, "imported", scope, "", "", stamp,
                              batch_id=batch_id, state="suppressed")
            if report['complete']:
                self._enqueue(db, "completed:" + scope + ":" + batch_id, "completed", scope, "", "", stamp,
                              batch_id=batch_id, state="suppressed")

    def _observe_connection(self, db, scope, values, view, stamp):
        if _meta(db, 'source_connection'):
            # Discovery confirmations replace duplicate connection notices.
            db.execute("UPDATE notification_events SET state='cancelled' WHERE kind IN ('connected','disconnected') AND state='pending'")
            return
        """Track fresh, identity-checked observations, separately from sync.

        One successful identity check announces a connection. Disconnection
        requires three different observations spanning at least twenty seconds.
        Service restarts, stale status and repeated reads are not transitions.
        """
        source = values['source']
        transport = source['transport']
        address = source['url']
        peer = _digest([scope, source['type'], source['serial'], transport, address])
        state = _meta(db, 'notification_connection', {})
        if state.get('peer') != peer:
            db.execute("UPDATE notification_events SET state='cancelled' WHERE kind IN ('connected','disconnected') AND state='pending'")
            state = {'peer': peer, 'stable': None, 'episode': uuid.uuid4().hex, 'sequence': 0}
        if not values['notifications'].get('connection_events', True):
            db.execute("UPDATE notification_events SET state='cancelled' WHERE kind IN ('connected','disconnected') AND state='pending'")
            state.update(stable=None, last_observation=None, failures=0)
            _put_meta(db, 'notification_connection', state)
            return
        _put_meta(db, 'notification_connection', state)
        observation = view.get('health', {}).get('source', {})
        if (not view.get('running') or view.get('configuration', {}).get('pending') or observation.get('stale')
                or observation.get('transport') != transport or type(observation.get('online')) is not bool):
            return
        online = observation['online']
        if online and observation.get('serial_number') != source['serial']:
            return
        try:
            observed = datetime.fromisoformat(observation['checked_at']).timestamp()
        except (KeyError, ValueError, TypeError, OverflowError):
            return
        if not 0 <= stamp - observed <= max(30, values['backup']['poll_seconds'] * 3):
            return
        if state.get('last_observation') is not None and observed <= state['last_observation']:
            return
        state['last_observation'] = observed
        transition = False
        if online:
            transition = state.get('stable') is not True
            state.update(failures=0, failure_since=None)
        elif state.get('stable') is None:
            # Starting with no camera is normal, not a disconnect event.
            state.update(stable=False, failures=0)
        elif state.get('stable') is True:
            state['failures'] = state.get('failures', 0) + 1
            state['failure_since'] = state.get('failure_since') or observed
            transition = state['failures'] >= 3 and observed - state['failure_since'] >= 20
        if transition:
            state.update(stable=online, sequence=state.get('sequence', 0) + 1)
            # If delivery was unavailable, send the latest confirmed state only.
            db.execute("UPDATE notification_events SET state='cancelled' WHERE kind IN ('connected','disconnected') AND state='pending'")
            kind = 'connected' if online else 'disconnected'
            mode = {'usb': 'USB', 'wifi': 'Wi-Fi'}.get(transport, '相机连接')
            title = '素材导入助手 · 相机连接已建立' if online else '素材导入助手 · 相机连接不可用'
            text = mode + (' · 已核对相机身份' if online else ' · 暂时无法访问相机')
            key = 'connection:' + scope + ':' + state['episode'] + ':' + str(state['sequence'])
            self._enqueue(db, key, kind, scope, title, text, stamp)
        _put_meta(db, 'notification_connection', state)

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
    def _completion(result):
        from .bot_cards import cleanup_text
        return f"{result['file_count']} 个文件 · {_size(result['byte_count'])}\n" + cleanup_text(
            result['cleanup'], result['ignored_preview_files'])

    @staticmethod
    def _card_key(scope, view, credentials):
        # The destination, batch, application and recipient all own this message.
        # Secret rotation must not create a new card; changing the recipient must.
        media_scope = view.get('selection', {}).get('scope', 'all')
        if media_scope != 'all':
            scope += ':' + media_scope
        return 'notification_progress_card:' + _digest([
            scope, view.get('batch', {}).get('id'), credentials['app_id'],
            credentials['receive_id_type'], credentials['receive_id']])

    def _card_signature(self, view, options):
        from .bot_cards import connection_elements
        return _digest([self._progress(view)[0], view.get('running'),
                        view.get('configuration', {}).get('sync_enabled'), options['enabled'],
                        options['progress_interval_seconds'], options.get('keyword'),
                        view.get('unplug'), view.get('attention'),
                        [(name, stage.get('phase'), stage.get('message')) for name, stage in view.get('stages', {}).items()],
                        view.get('controls'), view.get('cleanup'), connection_elements(view)])

    def _send_progress(self, db, scope, view, options, stamp, *, create=True):
        from .bot_cards import progress_card
        batch_id = view.get('batch', {}).get('id')
        if batch_id and not db.execute('SELECT 1 FROM transfer_runs WHERE id=?', (batch_id,)).fetchone():
            return
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
        cleanup_signature = _digest(view.get('cleanup'))
        if pointer.get('finished') and pointer.get('cleanup_signature') == cleanup_signature:
            return
        if not create and (not pointer.get('message_id') or pointer.get('signature') == signature
                           or pointer.get('retry_at', 0) > stamp):
            return
        content = progress_card(view, options['progress_interval_seconds'], options.get('keyword', ''),
                                _stamp(stamp), notifications_enabled=options['enabled'])
        if credentials.get('receive_id_type') == 'open_id':
            from .bot import Bot
            content['elements'] += Bot(self.settings, self.directory).progress_actions(view)
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
            pointer.update(signature=signature, updated_at=stamp, retry_at=0, attempts=0, batch_id=batch.get('id'), recipient=self._recipient(),
                           cleanup_signature=cleanup_signature,
                           finished=bool(batch.get('total_files') and batch.get('completed_files') == batch['total_files']
                                         and not view.get('selection', {}).get('cached_outside_scope_files')))
            with db:
                self._save_progress(db, key, pointer, batch_id)
        except Exception:
            pointer['attempts'] = pointer.get('attempts', 0) + 1
            pointer['retry_at'] = stamp + min(1800, 30 * 2 ** min(pointer['attempts'] - 1, 6))
            with db:
                self._save_progress(db, key, pointer, batch_id)
            raise

    @staticmethod
    def _save_progress(db, key, pointer, batch_id):
        """A send may finish after the round retired; attach its message to the result."""
        db.execute('BEGIN IMMEDIATE')
        if not batch_id or db.execute('SELECT 1 FROM transfer_runs WHERE id=?', (batch_id,)).fetchone():
            _put_meta(db, key, pointer)
            return
        if not pointer.get('message_id'):
            return
        row = db.execute("SELECT id,payload FROM notification_events WHERE batch_id=? AND detached=1 AND state='pending'", (batch_id,)).fetchone()
        if row:
            payload = json.loads(row['payload'])
            message = {'id': pointer['message_id'], 'recipient': pointer.get('recipient')}
            if message not in payload.setdefault('progress_messages', []):
                payload['progress_messages'].append(message)
                db.execute('UPDATE notification_events SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), row['id']))

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
    def _completed_view(view, report):
        result = copy.deepcopy(view)
        facts = report['result']
        result['batch'] = {'id': report['id'], 'scanned': True, 'total_files': facts['file_count'],
                           'total_bytes': facts['byte_count'],
                           'excluded_preview_files': facts['ignored_preview_files'],
                           'completed_files': facts['file_count'], 'imported_files': facts['file_count'],
                           'percent': 100, 'import_percent': 100}
        result['stages'] = {name: {'phase': 'completed'} for name in ('import', 'upload')}
        result['queues'] = {name: {'pending_files': 0} for name in ('import', 'upload')}
        result['cleanup'] = facts['cleanup']
        return result

    @staticmethod
    def _problems(view):
        found = {}
        controls = view.get("controls", {})
        cleaning_source = view.get('status', {}).get('phase') in ('checking_cleanup', 'cleaning_source')
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
                and view.get("work", {}).get("pending_files", 0)):
            for stage in ("import", "upload"):
                if active(stage):
                    add("cache_unavailable", "cache", stage)
                    break
        for item in view.get("files", []):
            if not item.get('selected', True) and not item.get('imported') and item.get('state') in ('pending', 'missing'):
                continue
            if item.get('cache_state') == 'unverified' or (item.get('state') in ('cached', 'staged', 'publishing') and item.get('cache_state') == 'missing'):
                add('cache_verification', 'cache', 'import')
            if item.get("error") and item.get("state") != "uploaded":
                stage = item.get("error_stage") or "import"
                progress = view.get("stages", {}).get(stage, {})
                retrying = (progress.get("phase") in ("downloading", "uploading", "publishing") and
                            (item.get("id") == progress.get("current_file_id") or
                             item.get("current") and item.get("stage") == stage))
                # Prefer the classified stage error; a generic file alert would duplicate it.
                if not retrying and not any(p["stage"] == stage for p in found.values()):
                    add("source_file" if stage == "import" else "file", "file", stage)
            if not cleaning_source and item.get("source_cleanup_error") and item.get('cleanup_selected', item.get('selected', True)) and view.get("policy", {}).get("delete_source"):
                add("source_cleanup", "source", "import")
            if item.get("state") == "uploaded":
                if item.get("cache_cleanup_error") and view.get("policy", {}).get("cleanup_cache"):
                    add("cache_cleanup", "cache", "upload")
                if not cleaning_source and item.get("cleanup") in ("blocked", "changed") and item.get('selected', True) and view.get("policy", {}).get("delete_source"):
                    add("source_cleanup", "source", "import")
        link = view.get('source_link', {})
        if link.get('failure') and link.get('phase') in ('connecting', 'waiting_retry'):
            found.pop('source:import:source_state', None)
            add('source_network', 'source', 'import')
        elif link.get('phase') == 'unsupported':
            add('source_connection_mode', 'source', 'import')
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
        view = view if view is not None else (self.snapshotter or snapshot)(self.directory, self.settings)
        view = {**view, 'configuration': {**view.get('configuration', {}), 'sync_enabled': values['backup']['enabled']}}
        enabled = bool(options["enabled"] and values["backup"]["enabled"])
        with _database(self.directory) as db:
            from .destination_binding import identity
            destination = identity(db, Config.from_values(values))
            scope = _digest([destination, values["source"]["type"], values["source"]["serial"]])
            state = _meta(db, "notification_state", {})
            batches = self._results(db, values, destination)
            if state.get("scope") != scope:
                state = {"scope": scope, "armed_at": None, "problems": {}}
            resumed = enabled and state.get('armed_at') is None
            with db:
                db.execute("UPDATE notification_events SET state='cancelled' WHERE scope<>? AND state='pending'", (scope,))
                self._observe_connection(db, scope, values, view, stamp)
                db.execute("UPDATE notification_events SET state='cancelled' WHERE detached=0 AND state='pending' AND kind IN ('completed','imported','progress') AND batch_id IS NOT NULL AND batch_id NOT IN (SELECT id FROM transfer_runs)")
                self._baseline(db, {k: v for k, v in batches.items() if v['historical']}, scope, stamp)
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
                    if not values['backup']['enabled']:
                        db.execute("UPDATE notification_events SET state='cancelled' WHERE kind='requested_progress' AND state='pending'")
                    db.execute("UPDATE notification_events SET state='cancelled' WHERE state='pending' AND kind NOT IN ('connected','disconnected','requested_progress') AND (detached=0 OR ?=0)", (int(options['enabled']),))
                    _put_meta(db, "notification_state", state)
                    delivery = _meta(db, "notification_delivery", {})
                    # Retain an explicit test result while automatic notification is disabled.
                    delivery.update(enabled=bool(options["enabled"] or options.get('connection_events', True)),
                                    sync_enabled=bool(values["backup"]["enabled"]),
                                    pending_count=db.execute("SELECT COUNT(*) FROM notification_events WHERE state='pending'").fetchone()[0])
                    if delivery.get("last_event") != "test":
                        delivery.update(state="disabled", next_retry_at=None)
                    _put_meta(db, "notification_delivery", delivery)
                    db.commit()
                    self._refresh_existing_progress(db, scope, view, options, stamp)
                    self._send_pending(db, scope, options, stamp, view)
                    return
                if (not view.get("running") or view.get("configuration", {}).get("pending")
                        or view.get("status", {}).get("phase") == "applying_config"):
                    _put_meta(db, "notification_state", state)
                    self._reconcile_delivery(db, options, stamp)
                    db.commit()
                    self._send_pending(db, scope, options, stamp, view, starts_only=True)
                    return
                for batch_id, report in batches.items():
                    if report['historical']:
                        continue
                    if report['complete'] and not self._cleaning(view, batch_id):
                        self._enqueue(db, "completed:" + scope + ":" + batch_id, "completed", scope,
                                      "素材导入助手 · 归档完成", self._completion(report['result']), stamp, batch_id=batch_id)
                    if (report.get('imported_at') and not report['complete']
                            and batch_id == view.get('batch', {}).get('id')
                            and view.get('unplug', {}).get('ready') and view.get('unplug', {}).get('can_disconnect', True)
                            and not self._cleaning(view, batch_id)):
                        self._enqueue(db, "imported:" + scope + ":" + batch_id, "imported", scope,
                                      "素材导入助手 · 本地导入完成",
                                      "素材已完整导入。", stamp, batch_id=batch_id)
                active = any(stage.get("phase") in ("downloading", "uploading") and
                             not view.get("controls", {}).get(name, {}).get("requested_stop", False)
                             for name, stage in view.get("stages", {}).items())
                if not active or (batches.get(view["batch"].get("id"), {}).get("complete", False)
                                  and not view.get('selection', {}).get('cached_outside_scope_files')):
                    db.execute("UPDATE notification_events SET state='cancelled' WHERE kind='progress' AND state='pending'")
                db.execute("UPDATE notification_events SET state='cancelled' WHERE kind='progress' AND state='pending' AND batch_id IS NOT ?",
                           (view["batch"].get("id"),))
                signature, text = self._progress(view)
                if (active and (view["batch"].get("total_files", 0) > 0 or view.get('selection', {}).get('cached_outside_scope_files'))
                        and stamp - state.get("progress_at", stamp) >= options["progress_interval_seconds"]
                        and signature != state.get("progress_signature")):
                    db.execute("UPDATE notification_events SET state='cancelled' WHERE kind='progress' AND state='pending'")
                    self._enqueue(db, "progress:" + scope + ":" + str(stamp), "progress", scope,
                                  "素材导入助手 · 传输进度", text, stamp, batch_id=view["batch"].get("id"))
                    state.update(progress_at=stamp, progress_signature=signature)
                observed = {key: value for key, value in self._problems(view).items() if value['code'] not in RETRY_ONLY}
                previous = state.get("problems", {})
                current = {}
                for key, problem in observed.items():
                    started = previous.get(key, {}).get("since", stamp)
                    current[key] = previous.get(key) or {"since": started, 'files': [f['id'] for f in view.get('files', [])
                        if f.get('error') or f.get('source_cleanup_error') or f.get('cache_cleanup_error') or
                        f.get('cache_state') == 'unverified' or f.get('state') in ('cached', 'staged', 'publishing') and f.get('cache_state') == 'missing']}
                    delay = 0
                    if stamp - started >= delay:
                        title, action = PROBLEMS[problem["code"]]
                        self._enqueue(db, "failed:" + scope + ":" + key + ":" + str(started), "failed", scope,
                                      "素材导入助手 · " + title, action, stamp)
                # A recovered fault should not be delivered later from an old retry queue.
                for key, episode in previous.items():
                    if key not in current:
                        failed_id = "failed:" + scope + ":" + key + ":" + str(episode["since"])
                        db.execute("UPDATE notification_events SET state='cancelled' WHERE id=? AND state='pending'",
                                   (failed_id,))
                        if self._recovered(key, episode, view):
                            code = key.split(':', 2)[-1]
                            label = {'network': '目的地连接已恢复' if key.startswith('destination:') else '相机连接已恢复',
                                     'source_network': '相机连接已恢复', 'source_cleanup': '原片清理已完成',
                                     'cache_cleanup': '缓存清理已完成', 'cache_verification': '缓存核验已通过',
                                     'cache_unavailable': '缓存盘已恢复', 'cache_capacity': '缓存空间已恢复',
                                     'source_state': '相机已就绪'}.get(code, '相关文件已处理成功')
                            self._recover_card(db, failed_id, scope, stamp, '程序将按当前备份设置继续处理。', title=label)
                        else:
                            current[key] = episode
                state["problems"] = current
                self._backlog(db, scope, state, view, stamp)
                _put_meta(db, "notification_state", state)
                self._reconcile_delivery(db, options, stamp)
            self._send_pending(db, scope, options, stamp, view)
            # Promote an initial scanning card as soon as a batch is known,
            # without waiting for the ordinary progress interval.
            if view['batch'].get('id'):
                startup_key = self._card_key(scope, {**view, 'batch': {}}, self.settings.notification_credentials())
                if _meta(db, startup_key, {}).get('message_id'):
                    self._refresh_existing_progress(db, scope, view, options, stamp)
            if (not batches.get(view['batch'].get('id'), {}).get('complete', False) and
                    (resumed or not active or any(stage.get('phase', '').startswith('waiting') or stage.get('phase') == 'failed'
                                       for stage in view.get('stages', {}).values()))):
                self._refresh_existing_progress(db, scope, view, options, stamp)
            for report in batches.values():
                if report['complete']:
                    self._refresh_existing_progress(db, scope, self._completed_view(view, report), options, stamp)
                    self._refresh_completion(db, scope, report, view, options, stamp)

    def _refresh_completion(self, db, scope, report, view, options, stamp):
        """Cleanup may finish after archive delivery; correct that same result card."""
        key = 'event_message:completed:' + scope + ':' + report['id']
        sent = _meta(db, key, {})
        if not sent.get('id') or sent.get('recipient') != self._recipient() or sent.get('retry_at', 0) > stamp:
            return
        from .bot_cards import event_card
        locations = report['locations']
        text = self._completion(report['result'])
        signature = _digest([text, locations])
        if signature == sent.get('signature'):
            return
        try:
            content = event_card('completed', '归档完成', text, view, keyword=options.get('keyword', ''),
                                 updated_at=_stamp(stamp), locations=locations, result=report['result'])
            content['config']['update_multi'] = True
            if self.settings.notification_credentials().get('receive_id_type') == 'open_id':
                from .bot import Bot
                bot = Bot(self.settings, self.directory)
                content['elements'] += bot.result_actions(report['result'], locations)
            adapter = notification_adapter('feishu')
            (self.updater or adapter.update_card)(self.settings.notification_credentials(), sent['id'], content)
            sent.update(signature=signature, result_signature=_digest([report['result'], locations]), retry_at=0)
        except Exception:
            sent['retry_at'] = stamp + 30
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM transfer_runs WHERE id=?', (report['id'],)).fetchone():
                _put_meta(db, key, sent)

    def _recover_card(self, db, event_id, scope, stamp, message, title='缓存上传已恢复'):
        sent = _meta(db, 'event_message:' + event_id, {})
        if sent and sent.get('recipient') == self._recipient():
            self._enqueue(db, 'recovered:' + event_id, 'recovered', scope, '素材导入助手 · ' + title,
                          message, stamp)
            _put_meta(db, 'recovery_message:recovered:' + event_id, sent)

    def _recipient(self):
        credentials = self.settings.notification_credentials()
        return _digest([credentials['app_id'], credentials['receive_id_type'], credentials['receive_id']])

    @staticmethod
    def _recovered(key, episode, view):
        scope, stage, code = key.split(':', 2)
        files = {f['id']: f for f in view.get('files', [])}
        affected = [files[i] for i in episode.get('files', []) if i in files]
        if episode.get('files') and len(affected) != len(episode['files']):
            return False
        if code == 'source_cleanup':
            return bool(affected) and all(f.get('cleanup') in ('deleted', 'absent') for f in affected)
        if code == 'cache_cleanup':
            return bool(affected) and all(f.get('state') == 'uploaded' and f.get('cache_state') == 'missing' for f in affected)
        if code == 'cache_verification':
            return bool(affected) and all(f.get('cache_available') or f.get('state') == 'uploaded' and f.get('cache_state') == 'missing' for f in affected)
        if scope == 'file' or code in ('permission', 'destination_full', 'source_file'):
            return bool(affected) and all(f.get('imported') if stage == 'import' else f.get('state') == 'uploaded' for f in affected)
        if code == 'cache_unavailable':
            return view.get('health', {}).get('cache', {}).get('available') is True
        if code == 'cache_capacity':
            disk = view.get('disk', {})
            return bool(disk.get('available') and disk.get('extra_bytes_needed') == 0 and disk.get('quota_extra_bytes') in (None, 0))
        peer = view.get('health', {}).get(scope, {})
        if code == 'source_state':
            telemetry = peer.get('telemetry', {})
            return bool(telemetry.get('available') and not telemetry.get('stale') and telemetry.get('ready') is True
                        and all(telemetry.get(k) is False for k in ('recording', 'busy', 'overheating', 'cold')))
        return peer.get('online') is True and not peer.get('stale')

    def _backlog(self, db, scope, state, view, stamp):
        """One alert per stalled episode; retries of old bytes are not progress."""
        backlog = view.get('backlog', {})
        previous = state.get('backlog', {})
        pending = backlog.get('files', 0)
        archived = view.get('batch', {}).get('completed_files', 0)
        upload = view.get('stages', {}).get('upload', {})
        file_id = upload.get('current_file_id')
        peaks = previous.get('peaks', {})
        transferred = upload.get('current_bytes', 0) if upload.get('phase') in ('uploading', 'publishing') else 0
        changed = (view.get('batch', {}).get('id') == previous.get('round') and archived > previous.get('archived', archived)) or bool(file_id and transferred > peaks.get(file_id, 0))
        event_id = previous.get('event_id')
        if not pending or changed:
            if event_id:
                db.execute("UPDATE notification_events SET state='cancelled' WHERE id=? AND state='pending'", (event_id,))
                self._recover_card(db, event_id, scope, stamp,
                                   '缓存已全部归档。' if not pending else '上传已恢复进展，剩余缓存会继续归档。')
            previous.update(progress_at=stamp, event_id=None)
        if not pending:
            state['backlog'] = {}
            return
        previous.setdefault('progress_at', stamp)
        live_ids = {f['id'] for f in view.get('files', []) if f.get('state') != 'uploaded'}
        peaks = {key: value for key, value in peaks.items() if key in live_ids}
        if file_id:
            peaks[file_id] = max(peaks.get(file_id, 0), transferred)
        previous.update(peaks=peaks, archived=archived, round=view.get('batch', {}).get('id'))
        if stamp - previous['progress_at'] >= BACKLOG_DELAY and not previous.get('event_id'):
            event_id = 'backlog:' + scope + ':' + str(previous['progress_at'])
            self._enqueue(db, event_id, 'backlog', scope, '素材导入助手 · 归档进度长时间未更新',
                          '已连续 6 小时没有上传进展。请检查目的地和网络，并保持缓存盘连接；不会清理未归档的完整缓存。', stamp)
            previous['event_id'] = event_id
        state['backlog'] = previous

    def _send_pending(self, db, scope, options, stamp, view, *, starts_only=False):
        # Send at most one message per tick; failure backoff is independent for each event.
        candidates = db.execute("""SELECT * FROM notification_events WHERE state='pending' AND scope=?
            AND next_attempt_at<=? ORDER BY CASE kind WHEN 'connected' THEN -2 WHEN 'disconnected' THEN -2 WHEN 'started' THEN -1 WHEN 'requested_progress' THEN -1 WHEN 'failed' THEN 0 WHEN 'imported' THEN 1 WHEN 'completed' THEN 2 ELSE 3 END, created_at""",
                         (scope, stamp)).fetchall()
        if not candidates or self.stop.is_set():
            return
        latest = self.settings.read().get("config") or {}
        if not latest:
            return
        transfer_enabled = bool(latest.get('backup', {}).get('enabled') and latest.get('notifications', {}).get('enabled'))
        connection_enabled = bool(latest.get('notifications', {}).get('connection_events', True))
        from .config import Config
        from .destination_binding import identity
        latest_destination = identity(db, Config.from_values(latest))
        if scope != _digest([latest_destination, latest["source"]["type"], latest["source"]["serial"]]):
            return
        options = latest["notifications"]
        current_batches = None
        current = {}
        for row in candidates:
            if starts_only and row['kind'] not in ('started', 'requested_progress'):
                continue
            if row['kind'] in CONNECTION_KINDS:
                if not connection_enabled or not view.get('running') or view.get('configuration', {}).get('pending'):
                    continue
                peer = latest['source']
                expected = _digest([scope, peer['type'], peer['serial'], peer['transport'],
                                    peer['url']])
                if _meta(db, 'notification_connection', {}).get('peer') != expected:
                    continue
                observation = view.get('health', {}).get('source', {})
                if (observation.get('stale') or observation.get('transport') != peer['transport']
                        or observation.get('online') is not (row['kind'] == 'connected')):
                    continue
            elif not transfer_enabled and not (row['detached'] and latest['notifications']['enabled']) and not (row['kind'] == 'requested_progress' and latest['backup']['enabled']):
                continue
            payload = json.loads(row["payload"])
            if row["kind"] == "completed" and not row['detached']:
                if current_batches is None:
                    current_batches = self._results(db, latest, latest_destination)
                current = current_batches.get(row["batch_id"], {})
                if not current.get('complete') or self._cleaning(view, row["batch_id"]):
                    with db:
                        db.execute("UPDATE notification_events SET next_attempt_at=? WHERE id=?", (stamp + 5, row["id"]))
                        self._reconcile_delivery(db, options, stamp)
                    continue
                payload.update(title='素材导入助手 · 归档完成', text=self._completion(current['result']),
                               result=current['result'], locations=current['locations'])
            elif row['kind'] == 'imported':
                if row['batch_id'] != view.get('batch', {}).get('id'):
                    with db:
                        db.execute("UPDATE notification_events SET state='cancelled' WHERE id=?", (row['id'],))
                    continue
                if not view.get('unplug', {}).get('ready') or not view.get('unplug', {}).get('can_disconnect', True) or self._cleaning(view, row['batch_id']):
                    continue
                if view.get('batch', {}).get('completed_files') == view.get('batch', {}).get('total_files'):
                    with db:
                        db.execute("UPDATE notification_events SET state='cancelled' WHERE id=?", (row['id'],))
                    continue
            elif row["kind"] in ("progress", "started", "requested_progress"):
                payload["text"] = self._progress(view)[1]
            break
        else:
            return
        try:
            adapter = notification_adapter("feishu")
            if row['kind'] in ('progress', 'started', 'requested_progress'):
                progress_view = view
                if row['kind'] in ('started', 'requested_progress'):
                    key = self._card_key(scope, view, self.settings.notification_credentials())
                    if _meta(db, key, {}).get('finished'):
                        # The previous batch is final; this start awaits a new scan.
                        progress_view = {**view, 'batch': {'scanned': False}, 'stages': {}}
                self._send_progress(db, scope, progress_view, options, stamp)
            else:
                from .bot_cards import event_card
                locations = payload.get('locations', [])
                if payload.get('result'):
                    payload['result'].setdefault('destination_name', self.settings.read()['config']['destination']['name'])
                if row['kind'] == 'completed' and payload.get('result'):
                    payload.update(title='素材导入助手 · 归档完成', text=self._completion(payload['result']))
                content = event_card(row['kind'], payload['title'], payload['text'], view,
                                     keyword=options.get('keyword', ''), updated_at=_stamp(stamp), locations=locations,
                                     result=payload.get('result'))
                if row['kind'] in ('completed', 'imported') and self.settings.notification_credentials().get('receive_id_type') == 'open_id':
                    from .bot import Bot
                    bot = Bot(self.settings, self.directory)
                    content['elements'] += (bot.result_actions(payload.get('result'), locations) if row['kind'] == 'completed' else
                                            [bot.buttons(bot.button('查看详情', {'panel': 'details'}))])
                    content['config']['update_multi'] = True
                if row['kind'] in ('failed', 'backlog') and self.settings.notification_credentials().get('receive_id_type') == 'open_id':
                    from .bot import Bot
                    bot = Bot(self.settings, self.directory)
                    content['elements'] += bot.problem_actions(view, self.settings.read().get('revision'))
                    content['config']['update_multi'] = True
                if row['detached']:
                    for message in payload.get('progress_messages', []):
                        if message['recipient'] != self._recipient():
                            continue
                        try:
                            (self.updater or adapter.update_card)(self.settings.notification_credentials(), message['id'], content)
                        except Exception as exc:
                            if getattr(exc, 'code', None) != 'notification_card_unavailable':
                                raise
                    if payload.get('update_message_id') and payload.get('update_recipient') == self._recipient():
                        (self.updater or adapter.update_card)(self.settings.notification_credentials(), payload['update_message_id'], content)
                    else:
                        (self.sender or adapter.send_card)(self.settings.notification_credentials(), content, event_id=_meta(db, 'notification_instance_id') + ':' + row['id'])
                elif row['kind'] == 'recovered':
                    sent = _meta(db, 'recovery_message:' + row['id'], {})
                    if sent and sent.get('recipient') == self._recipient():
                        (self.updater or adapter.update_card)(self.settings.notification_credentials(), sent['id'], content)
                else:
                    message_id = (self.sender or adapter.send_card)(self.settings.notification_credentials(), content,
                                                      event_id=_meta(db, "notification_instance_id") + ":" + row["id"])
                    if row['kind'] in ('failed', 'backlog', 'completed'):
                        with db:
                            db.execute('BEGIN IMMEDIATE')
                            latest_event = db.execute('SELECT detached,payload FROM notification_events WHERE id=?', (row['id'],)).fetchone()
                            if latest_event and latest_event['detached']:
                                final_payload = json.loads(latest_event['payload'])
                                final_payload.update(update_message_id=message_id, update_recipient=self._recipient())
                                db.execute('UPDATE notification_events SET payload=? WHERE id=?', (json.dumps(final_payload, ensure_ascii=False), row['id']))
                            elif latest_event:
                                _put_meta(db, 'event_message:' + row['id'], {'id': message_id, 'recipient': self._recipient(),
                                    'signature': _digest([payload['text'], locations]),
                                    'result_signature': _digest([payload.get('result'), locations])})
        except Exception as exc:
            code, message = _safe_failure(exc)
            next_retry_at = stamp + min(1800, 30 * 2 ** min(row["attempts"], 6))
            with db:
                db.execute("UPDATE notification_events SET attempts=attempts+1,next_attempt_at=? WHERE id=?", (next_retry_at, row["id"]))
                delivery = _meta(db, "notification_delivery", {})
                delivery.update(state="retrying", enabled=True, sync_enabled=bool(latest['backup']['enabled']), last_event=row["kind"], last_error=message, last_error_at=_stamp(stamp),
                                error_code=code, next_retry_at=_stamp(next_retry_at), pending_count=db.execute("SELECT COUNT(*) FROM notification_events WHERE state='pending'").fetchone()[0])
                _put_meta(db, "notification_delivery", delivery)
            return
        with db:
            if row['detached']:
                db.execute('DELETE FROM notification_events WHERE id=?', (row['id'],))
                db.execute('DELETE FROM meta WHERE key=?', ('event_message:' + row['id'],))
            else:
                db.execute("UPDATE notification_events SET state='sent',attempts=attempts+1,sent_at=? WHERE id=? AND detached=0", (stamp, row["id"]))
            delivery = _meta(db, "notification_delivery", {})
            delivery.update(state="sent", enabled=True, sync_enabled=bool(latest['backup']['enabled']), last_event=row["kind"], last_sent_at=_stamp(stamp),
                            last_error=None, error_code=None, next_retry_at=None,
                            pending_count=db.execute("SELECT COUNT(*) FROM notification_events WHERE state='pending'").fetchone()[0])
            _put_meta(db, "notification_delivery", delivery)
