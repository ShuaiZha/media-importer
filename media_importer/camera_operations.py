"""One-shot source inspection and cleanup, executed by the source worker.

Plans contain an immutable, short-lived inventory. They never change preferences,
download files, or select new material during confirmation or recovery.
"""
import json
import threading
import time
import uuid

from .model import Asset, BackupError, Cancelled, ConfigError, now
from .source_connection import fresh
from .storage import require_cache


MODES = {'archived': '已归档素材', 'imported': '已完整导入素材'}
ACTIVE = ('queued', 'running', 'queued_delete', 'deleting', 'queued_verify', 'verifying')


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS camera_operations (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
        mode TEXT NOT NULL, state TEXT NOT NULL, revision TEXT NOT NULL,
        session_id TEXT NOT NULL, destination TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
        plan TEXT, result TEXT, intents TEXT NOT NULL DEFAULT '[]'
    )""")


def get(db, identifier, owner=None):
    row = db.execute('SELECT * FROM camera_operations WHERE id=?' +
                     (' AND owner=?' if owner else ''), (identifier, owner) if owner else (identifier,)).fetchone()
    return dict(row) if row else None


def pending(db):
    return db.execute("SELECT 1 FROM camera_operations WHERE state IN "
                      "('queued','running','queued_delete','deleting','queued_verify','verifying') LIMIT 1").fetchone() is not None


def enqueue(store, settings, key, owner, kind, mode='archived'):
    if kind not in ('inspect', 'preview') or mode not in MODES:
        raise ConfigError('相机操作无效，请重新打开菜单')
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, owner + ':' + key).hex
    existing = get(store.db, identifier, owner)
    if existing:
        return existing
    item = settings.read()
    connection = store.get_meta('source_connection', {})
    if item['error']:
        raise ConfigError(item['error'])
    if not fresh(connection):
        raise ConfigError('相机不可访问，请连接并开启相机后重试')
    if kind == 'preview' and connection.get('state') != 'approved':
        raise ConfigError('请先确认当前相机，再预览原片清理范围')
    from .destination_binding import identity
    from .config import Config
    config = Config.from_values(item['config'])
    with store.db:
        # Bound pending work: repeated menu clicks should not queue full scans.
        if pending(store.db):
            raise ConfigError('已有相机操作等待执行，请等待原卡片更新，或停止本次操作')
        stamp = time.time()
        store.db.execute("""INSERT INTO camera_operations
            (id,owner,kind,mode,state,revision,session_id,destination,created,updated)
            VALUES (?,?,?,?,'queued',?,?,?,?,?)""",
            (identifier, owner, kind, mode, item['revision'], connection['id'],
             identity(store.db, config), stamp, stamp))
    return get(store.db, identifier, owner)


def confirm(store, settings, identifier, owner):
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        op = get(store.db, identifier, owner)
        if not op or op['kind'] != 'preview':
            raise ConfigError('清理预览不存在，请重新预览')
        if op['state'] in ('queued_delete', 'deleting', 'done'):
            return op  # Replayed confirmation cannot create another operation.
        if op['state'] != 'ready' or op['cancelled']:
            raise ConfigError('本次预览已失效，请重新预览')
        item = settings.read()
        connection = store.get_meta('source_connection', {})
        if (time.time() - op['updated'] > 300 or item['error'] or item['revision'] != op['revision']
                or connection.get('id') != op['session_id'] or not fresh(connection)
                or connection.get('state') != 'approved'):
            raise ConfigError('相机连接、配置或预览有效期已变化，请重新预览')
        if pending(store.db):
            raise ConfigError('已有相机操作正在执行，请稍后重新确认')
        plan = json.loads(op['plan'])
        if not plan['groups']:
            raise ConfigError('没有符合条件的素材，未执行删除')
        store.db.execute("UPDATE camera_operations SET state='queued_delete',updated=? WHERE id=?",
                         (time.time(), identifier))
    return get(store.db, identifier, owner)


def cancel(db, identifier=None, owner=None):
    query = """UPDATE camera_operations SET cancelled=1 WHERE state IN
        ('queued','running','ready','queued_delete','deleting','queued_verify','verifying')"""
    args = []
    if identifier:
        query += ' AND id=?'
        args.append(identifier)
    if owner:
        query += ' AND owner=?'
        args.append(owner)
    db.execute(query, args)


def verify(store, identifier, owner):
    """Read-only retry of an uncertain result; never resume deletion."""
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        op = get(store.db, identifier, owner)
        if not op or op['kind'] != 'preview' or op['state'] != 'done' or not json.loads(op['result'] or '{}').get('unknown'):
            raise ConfigError('没有待复查的删除结果，请重新获取卡片')
        if pending(store.db):
            raise ConfigError('已有相机操作正在执行，请稍后复查')
        store.db.execute("UPDATE camera_operations SET state='queued_verify',updated=? WHERE id=?",
                         (time.time(), identifier))
    return get(store.db, identifier, owner)


def prune(db):
    # Short-lived confirmation manifests and receipts, never a history catalogue.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='camera_operations'").fetchone():
        for row in db.execute("SELECT id,result FROM camera_operations WHERE state IN ('ready','done') AND updated<?",
                              (time.time() - 86400,)).fetchall():
            if not json.loads(row['result'] or '{}').get('unknown'):
                db.execute('DELETE FROM camera_operations WHERE id=?', (row['id'],))


def _save(store, op, state, result, plan=None):
    with store.db:
        store.db.execute("""UPDATE camera_operations SET state=?,updated=?,result=?,
            plan=COALESCE(?,plan) WHERE id=?""",
            (state, time.time(), json.dumps(result, ensure_ascii=False),
             json.dumps(plan, ensure_ascii=False) if plan is not None else None, op['id']))


class OperationControl:
    """Cancellation independent of sync: an explicit stop still cancels deletion."""
    def __init__(self, service, settings, op):
        self.service, self.settings, self.op = service, settings, op
        self.transfer_stop = threading.Event()

    def boundary(self):
        if self.service.stop.is_set():
            raise Cancelled('服务停止，本次相机操作已中断')
        current = get(self.service.store.db, self.op['id'])
        if not current or current['cancelled']:
            raise Cancelled('本次相机操作已停止，未执行的原片保留')
        item = self.settings.read()
        if item['error'] or item['revision'] != self.op['revision']:
            raise Cancelled('配置已变化，本次相机操作停止；请重新预览')
        connection = self.service.store.get_meta('source_connection', {})
        if connection.get('id') != self.op['session_id'] or not fresh(connection):
            raise Cancelled('相机连接已变化，本次操作停止；请重新预览')

    def allowed(self, policy=None):
        self.boundary()
        return True

    def refresh(self):
        self.boundary()


def _rows(service, assets):
    current = {r['asset_id']: r for r in service.rows()}
    indexed = {r['asset_id']: dict(r) for r in service.store.db.execute(
        'SELECT * FROM source_index WHERE destination=? AND source_type=? AND serial=?',
        (service.destination_id, service.config.source['type'], service.config.source['serial']))}
    for asset in assets:
        if asset.identity not in current and asset.identity in indexed:
            record = indexed[asset.identity]
            current[asset.identity] = {
                **record, 'id': service.store.file_id(service.destination_id, asset),
                'asset': json.dumps(asset.to_dict()), 'cache_identity': None, 'cleanup': 'pending',
                'indexed': True,
            }
    return current


def _proof(service, row, mode):
    if not row:
        return '没有本程序记录的完整副本'
    if mode == 'imported' and service.cache_valid(row):
        return None
    if row['state'] == 'uploaded' and row.get('remote_path') and row.get('remote_identity'):
        if service.confirmed_final(row, service.destination.stat(row['remote_path'])):
            return None
        return '正式归档的大小或文件身份与记录不符'
    return '尚未完成远端归档' if mode == 'archived' else '缺少可核验的完整缓存或正式归档'


def inspect(service, mode=None):
    identity = service.observe_source()
    connection = service.store.get_meta('source_connection', {})
    identity = {**identity, 'transport': connection.get('device', {}).get('transport')}
    if connection.get('state') != 'approved':
        return {'identity': identity, 'scanned': False, 'reason': '等待确认当前相机，尚未扫描素材'}, None
    service.boundary()
    try:
        assets = service.source_snapshot()
    except BackupError as exc:
        return {'identity': identity, 'scanned': False, 'error': str(exc)}, None
    groups = service.source.groups(assets)
    rows = _rows(service, assets)
    from .media import category
    counts = {kind: {'files': 0, 'bytes': 0} for kind in ('video', 'photo', 'other')}
    preview_ids = {key for group in groups for key in group.get('preview_ids', [])}
    counts['preview'] = {'files': 0, 'bytes': 0}
    for asset in assets:
        kind = 'preview' if asset.identity in preview_ids else category(asset)
        counts[kind]['files'] += 1
        counts[kind]['bytes'] += asset.size
    result = {'identity': identity, 'scanned': True, 'counts': counts,
              'files': len(assets), 'bytes': sum(a.size for a in assets), 'observed_at': now()}
    if mode is None:
        return result, None
    selected, retained = [], []
    can_delete = connection.get('device', {}).get('compatibility', {}).get('delete_verified', False)
    with service.cache_lock():
        require_cache(service.store)
        for group in groups:
            service.boundary()
            members = group['members']
            reason = None if can_delete else '当前型号或固件尚未通过删除验收'
            reason = reason or (group.get('reason') or '素材关联无法确认' if not group['safe'] else None)
            previews = set(group.get('preview_ids', []))
            pending_archive = 0
            for asset in members:
                if reason:
                    break
                row = rows.get(asset.identity)
                if row and row.get('cleanup') in ('deleted', 'absent'):
                    reason = '原片清理记录与当前相机内容不一致'
                    break
                if row and row['state'] == 'excluded' and row['role'] == 'preview' and asset.identity in previews:
                    continue
                try:
                    reason = _proof(service, row, mode)
                except BackupError as exc:
                    reason = str(exc)
                if not reason and row['state'] != 'uploaded':
                    pending_archive += 1
            item = {'id': group['id'], 'members': [a.to_dict() for a in members],
                    'preview_ids': sorted(previews), 'pending_archive': pending_archive}
            if reason:
                retained.append({'files': len(members), 'bytes': sum(a.size for a in members),
                                 'paths': [a.path for a in members], 'reason': reason})
            else:
                selected.append(item)
    plan = {'groups': selected, 'retained': retained}
    result.update(mode=mode, eligible_groups=len(selected),
                  eligible_files=sum(len(g['members']) for g in selected),
                  eligible_bytes=sum(a['size'] for g in selected for a in g['members']),
                  pending_archive=sum(g['pending_archive'] for g in selected), retained=retained)
    return result, plan


def _execute(service, op):
    plan = json.loads(op['plan'])
    store = service.store
    for group in plan['groups']:
        service.boundary()
        expected = [Asset.from_dict(a) for a in group['members']]
        fresh_assets = service.source_snapshot()
        current = {a.path: a for a in fresh_assets}
        # Restore only frozen, previously indexed entries. No new import tasks.
        rows = _rows(service, expected)
        from .lifecycle import restore_member
        for asset in expected:
            row = rows.get(asset.identity)
            if row and row.get('indexed'):
                with store.db:
                    restore_member(store, service.destination_id, asset, row)
        identifiers = [store.file_id(service.destination_id, a) for a in expected]
        if any(not store.get(key) for key in identifiers):
            continue
        if any(a.path in current and current[a.path].identity != a.identity for a in expected):
            for key in identifiers:
                store.update(key, source_cleanup_error='预览后原片身份变化，本组未执行清理')
            continue

        def intent(affected):
            # A stop can arrive during the final source scan, after the loop boundary.
            service.boundary()
            previous = json.loads(get(store.db, op['id'])['intents'])
            with store.db:
                store.db.execute('UPDATE camera_operations SET intents=?,updated=? WHERE id=?',
                                 (json.dumps(sorted(set(previous) | set(affected))), time.time(), op['id']))

        frozen = {'id': group['id'], 'members': json.dumps(identifiers), 'safe': True, 'reason': None}
        service.cleanup_groups(set(), groups=[frozen], copy_policy=op['mode'], before_delete=intent)
    return _outcome(service, op)


def _outcome(service, op, *, recovery=False):
    plan = json.loads(op['plan'] or '{"groups":[]}')
    assets = [Asset.from_dict(a) for group in plan['groups'] for a in group['members']]
    intents = set(json.loads(get(service.store.db, op['id'])['intents']))
    result = {'mode': op['mode'], 'deleted': 0, 'absent': 0, 'retained': 0, 'unknown': 0,
              'released_bytes': 0, 'details': [], 'recovered': recovery}
    try:
        # Reconciliation is read-only even after cancellation or process restart.
        if any((a.serial, a.source_type) != (service.config.source['serial'], service.config.source['type']) for a in assets):
            raise Cancelled('当前不是本次清理的相机，结果等待原设备复查')
        current = {a.path: a for a in service.source.snapshot()}
    except BackupError:
        current = None
    for asset in assets:
        row = service.store.get(service.store.file_id(service.destination_id, asset))
        if current is None:
            state = 'unknown' if asset.identity in intents else 'retained'
            reason = '相机不可访问，删除结果尚未复查' if state == 'unknown' else '未执行清理'
        elif asset.path not in current:
            state = 'deleted' if asset.identity in intents else 'absent'
            reason = None
            if row:
                service.store.update(row['id'], cleanup=state, source_cleanup_error=None)
                if service.store.cached_path(row).is_file():
                    service.checkpoint(service.store.get(row['id']))
        else:
            state = 'retained'
            reason = ((row or {}).get('source_cleanup_error') or
                      ('原片身份变化，保留当前文件' if current[asset.path].identity != asset.identity else '未删除，原片保留'))
        result[state] += 1
        if state == 'deleted':
            result['released_bytes'] += asset.size
        result['details'].append({'path': asset.path, 'state': state, 'reason': reason})
    return result


def process_one(parent, settings):
    """Called at an import boundary, or by the idle daemon while sync is off."""
    from .live_preview import active
    if active(parent.store):
        return False
    store = parent.store
    op = store.db.execute("SELECT * FROM camera_operations WHERE state IN "
                          "('queued','running','queued_delete','deleting','queued_verify','verifying') ORDER BY created LIMIT 1").fetchone()
    if op is None:
        return False
    op = dict(op)
    recovery = op['state'] in ('running', 'deleting')
    deleting = op['state'] in ('queued_delete', 'deleting')
    verifying = op['state'] in ('queued_verify', 'verifying')
    control = OperationControl(parent, settings, op)
    source = parent.source.fork(parent.stop) if hasattr(parent.source, 'fork') else parent.source
    remote = parent.destination.fork(stop=parent.stop) if hasattr(parent.destination, 'fork') else parent.destination
    from .service import Service
    service = Service(parent.config, store, source, remote, parent.stop, control=control)
    service.cache_guard = parent.cache_guard
    service.pipeline_stop = parent.pipeline_stop
    if parent.activity:
        source.on_activity = lambda: parent.activity.touch('import')
        remote.on_activity = lambda: parent.activity.touch('import')
    store.status(maintenance_running=True, source_work=True)
    with store.db:
        store.db.execute('UPDATE camera_operations SET state=?,updated=? WHERE id=?',
                         ('verifying' if verifying else 'deleting' if deleting else 'running', time.time(), op['id']))
    try:
        if verifying:
            _save(store, op, 'done', _outcome(service, op, recovery=True))
        elif recovery:
            result = _outcome(service, op, recovery=True) if deleting else {}
            result['error'] = '服务曾中断，本次操作已结束；未执行的清理需重新预览确认'
            _save(store, op, 'done', result)
        else:
            control.boundary()
            if parent.destination_id != op['destination']:
                raise Cancelled('归档位置已变化，请重新预览')
            if time.time() - op['updated'] > 300:
                raise Cancelled('相机操作等待超时，请重新预览')
            if deleting:
                result = _execute(service, op)
                _save(store, op, 'done', result)
            else:
                result, plan = inspect(service, op['mode'] if op['kind'] == 'preview' else None)
                _save(store, op, 'ready' if op['kind'] == 'preview' and plan is not None else 'done', result, plan)
    except (BackupError, OSError, ValueError) as exc:
        result = _outcome(service, op) if deleting else {}
        result['error'] = remote.redact(str(exc)) if hasattr(remote, 'redact') else str(exc)
        _save(store, op, 'done', result)
    finally:
        store.status(maintenance_running=False, source_work=False)
        if remote is not parent.destination:
            remote.close()
    return True
