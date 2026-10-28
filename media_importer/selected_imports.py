"""One frozen, user-selected transfer; full sync and preferences stay unchanged."""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .config import Config
from .model import BackupError, Cancelled, ConfigError, now
from .source_connection import fresh

PAGE_SIZE = 12
ACTIVE = ('queued', 'scanning', 'queued_transfer', 'running', 'waiting')


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS selected_imports (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
        revision TEXT NOT NULL, session_id TEXT NOT NULL, destination TEXT NOT NULL,
        choice_revision INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0,
        drain INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
        retry_at REAL NOT NULL DEFAULT 0, plan TEXT, result TEXT NOT NULL DEFAULT '{}'
    )""")


def get(db, identifier, owner=None):
    row = db.execute('SELECT * FROM selected_imports WHERE id=?' +
                     (' AND owner=?' if owner else ''),
                     (identifier, owner) if owner else (identifier,)).fetchone()
    return dict(row) if row else None


def active(db, owner=None):
    row = db.execute("""SELECT * FROM selected_imports WHERE state IN
        ('queued','scanning','queued_transfer','running','waiting')""" +
        (' AND owner=?' if owner else '') + ' ORDER BY created LIMIT 1',
        (owner,) if owner else ()).fetchone()
    return dict(row) if row else None


def pending(db):
    return db.execute("""SELECT 1 FROM selected_imports WHERE state IN
        ('queued','scanning','queued_transfer','running','waiting')
        AND (retry_at<=? OR cancelled=1 OR drain=1) LIMIT 1""", (time.time(),)).fetchone() is not None


def _settings(settings):
    item = settings.read()
    if item['error']:
        raise ConfigError(item['error'])
    return item


def _binding(store, item):
    from .destination_binding import identity
    return identity(store.db, Config.from_values(item['config']))


def enqueue(store, settings, key, owner):
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, 'selected:' + owner + ':' + key).hex
    existing = get(store.db, identifier, owner)
    if existing:
        return existing
    current = active(store.db, owner)
    if current:
        return current
    item = _settings(settings)
    connection = store.get_meta('source_connection', {})
    if not fresh(connection) or connection.get('state') != 'approved':
        raise ConfigError('请先连接并确认相机，再浏览素材')
    with store.db:
        stamp = time.time()
        store.db.execute("""INSERT INTO selected_imports
            (id,owner,state,revision,session_id,destination,created,updated)
            VALUES (?,?,'queued',?,?,?,?,?)""",
            (identifier, owner, item['revision'], connection['id'], _binding(store, item), stamp, stamp))
    return get(store.db, identifier, owner)


def _draft(store, settings, identifier, owner):
    op = get(store.db, identifier, owner)
    if not op or op['state'] not in ('choosing', 'ready') or op['cancelled']:
        raise ConfigError('本次选择已失效，请重新打开「选择素材」')
    item = _settings(settings)
    connection = store.get_meta('source_connection', {})
    if (item['revision'] != op['revision'] or not fresh(connection)
            or connection.get('state') != 'approved' or connection.get('id') != op['session_id']
            or _binding(store, item) != op['destination']
            or time.time() - op['created'] > 1800):
        raise ConfigError('相机、配置或清单有效期已变化，请重新浏览素材')
    return op, item, json.loads(op['plan'])


def choose(store, settings, identifier, owner, indices, page, revision):
    if type(page) is not int or page < 0 or not isinstance(indices, list):
        raise ConfigError('素材选择无效')
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        op, _, plan = _draft(store, settings, identifier, owner)
        if type(revision) is not int or revision != op['choice_revision']:
            raise ConfigError('选择已更新，请使用最新卡片')
        available = set(range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(plan['groups']))))
        if (any(type(index) is not int for index in indices) or len(set(indices)) != len(indices)
                or not set(indices) <= available or not available and plan['groups']):
            raise ConfigError('只能选择本页列出的素材')
        plan['selected'] = sorted((set(plan['selected']) - available) | set(indices))
        store.db.execute("""UPDATE selected_imports SET state='choosing',plan=?,choice_revision=choice_revision+1,
            updated=? WHERE id=?""", (json.dumps(plan, ensure_ascii=False), time.time(), identifier))
    return get(store.db, identifier, owner)


def archive_members(group, archive_previews):
    """Frozen archive scope shared by selection totals and file presentation."""
    previews = set(group.get('preview_ids', []))
    return [asset for asset in group.get('members', [])
            if archive_previews or asset['identity_key'] not in previews]


def _summary(plan):
    groups = [plan['groups'][i] for i in plan.get('selected', [])]
    assets = [asset for group in groups for asset in group['members']]
    included = [asset for group in groups for asset in archive_members(group, plan['archive_previews'])]
    return {'selected_groups': len(groups), 'source_files': len(assets),
            'total_files': len(included), 'total_bytes': sum(a['size'] for a in included),
            'excluded_preview_files': len(assets) - len(included),
            'already_archived': sum(a['identity_key'] in plan.get('archived_ids', []) for a in included),
            'imported_files': 0, 'uploaded_files': 0, 'import_bytes': 0, 'upload_bytes': 0}


def review(store, settings, identifier, owner):
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        op, _, plan = _draft(store, settings, identifier, owner)
        result = _summary(plan)
        if not result['total_files']:
            raise ConfigError('请至少选择一个需要导入的文件')
        store.db.execute("UPDATE selected_imports SET state='ready',result=?,updated=? WHERE id=?",
                         (json.dumps(result), time.time(), identifier))
    return get(store.db, identifier, owner)


def confirm(store, settings, identifier, owner, choice_revision):
    with settings.lock(), store.db:
        store.db.execute('BEGIN IMMEDIATE')
        op = get(store.db, identifier, owner)
        if not op or type(choice_revision) is not int or op['choice_revision'] != choice_revision:
            raise ConfigError('选择范围已变化，请重新确认文件')
        if op and op['state'] in ('queued_transfer', 'running', 'waiting', 'done'):
            return op
        op, item, plan = _draft(store, settings, identifier, owner)
        if op['state'] != 'ready' or not _summary(plan)['total_files']:
            raise ConfigError('请先检查所选文件，再确认备份')
        if item['config']['backup']['enabled']:
            raise ConfigError('全量备份仍开启，请先从「备份 → 停止备份」停止，再重新浏览素材')
        if active(store.db):
            raise ConfigError('已有选定任务正在处理，请先完成或停止该任务')
        if time.time() - op['updated'] > 300:
            raise ConfigError('文件确认已超过 5 分钟，请重新检查')
        plan['confirmed'] = True
        store.db.execute("""UPDATE selected_imports SET state='queued_transfer',plan=?,updated=?,retry_at=0
            WHERE id=?""", (json.dumps(plan, ensure_ascii=False), time.time(), identifier))
    return get(store.db, identifier, owner)


def cancel(db, identifier=None, owner=None, drain=False):
    query = """UPDATE selected_imports SET """ + ('drain=1' if drain else 'cancelled=1') + """
        WHERE state IN ('queued','scanning','choosing','ready','queued_transfer','running','waiting')"""
    args = []
    if identifier:
        query += ' AND id=?'
        args.append(identifier)
    if owner:
        query += ' AND owner=?'
        args.append(owner)
    db.execute(query, args)
    # Drafts have no transfer to stop; a cancelled draft must not stay confirmable.
    db.execute("""UPDATE selected_imports SET state='paused',updated=?
        WHERE (cancelled=1 OR drain=1) AND state IN ('choosing','ready')""", (time.time(),))


def resume(store, settings, identifier, owner):
    with settings.lock(), store.db:
        store.db.execute('BEGIN IMMEDIATE')
        op = get(store.db, identifier, owner)
        if not op or op['state'] != 'paused' or not json.loads(op['plan'] or '{}').get('confirmed'):
            raise ConfigError('没有可继续的选定任务，请重新浏览素材')
        item = _settings(settings)
        if item['config']['backup']['enabled'] or active(store.db):
            raise ConfigError('请先停止全量备份或其他选定任务')
        plan = json.loads(op['plan'])
        source = item['config']['source']
        if (_binding(store, item) != op['destination'] or
                (source['type'], source['serial']) != (plan['source_type'], plan['serial'])):
            raise ConfigError('当前相机或归档位置与原任务不同，任务与缓存已保留')
        store.db.execute("""UPDATE selected_imports SET state='queued_transfer',revision=?,cancelled=0,
            drain=0,updated=?,retry_at=0 WHERE id=?""", (item['revision'], time.time(), identifier))
    return get(store.db, identifier, owner)


class SelectionControl:
    """Thread-safe cancellation, independent of the full-sync stop event."""
    def __init__(self, parent, settings, op, browsing=False):
        self.settings, self.op, self.stop = settings, op, parent.stop
        self.path = parent.store.directory / 'state.sqlite3'
        self.browsing = browsing
        self.transfer_stop = threading.Event()
        self.source_interrupt = getattr(parent.control, 'source_interrupt', None)
        self.camera = getattr(parent.control, 'camera', None)
        self.lock = threading.RLock()
        self.checked_at = 0
        self.reason = None
        self._draining = False
        self.refresh(force=True)

    def refresh(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.checked_at < .5:
                return
            self.checked_at = time.monotonic()
            db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            try:
                current = get(db, self.op['id'])
                item = _settings(self.settings)
                reason = ('服务正在停止' if self.stop.is_set() else
                          '本次选定任务已停止，完整缓存与任务保留' if not current or current['cancelled'] else
                          '配置已变更，请确认后继续本次任务' if item['revision'] != self.op['revision'] else
                          '全量备份已开启，本次选定任务暂停' if not self.browsing and item['config']['backup']['enabled'] else None)
                self._draining = bool(current and current['drain'])
            except (BackupError, OSError, ValueError, sqlite3.Error):
                reason = '无法核对本次操作状态，已暂停传输'
            finally:
                db.close()
            if reason:
                self.reason = reason
                self.transfer_stop.set()

    def allowed(self, policy=None):
        self.refresh()
        return not self.transfer_stop.is_set() and policy is None

    def boundary(self):
        self.refresh()
        if self.transfer_stop.is_set():
            raise Cancelled(self.reason or '本次任务已停止')

    def draining(self):
        self.refresh()
        return self._draining

    def finish_stop(self):
        self.reason = '当前文件已完成，本次选定任务已停止'
        self.transfer_stop.set()
        return True


def _scan(service, op, settings):
    identity = service.observe_source()
    connection = service.store.get_meta('source_connection', {})
    if not fresh(connection) or connection.get('state') != 'approved' or connection.get('id') != op['session_id']:
        raise ConfigError('相机连接已变化，请重新确认并浏览')
    observed = service.source_scan()
    assets = observed.assets
    from .adapters import source_selection_groups
    groups = source_selection_groups(service.source, assets)
    plan = plan_for_groups(service.config, service.store, service.destination_id, identity, groups)
    plan['source_errors'] = observed.errors
    return plan, {'source_files': len(assets) + len(observed.errors), 'source_errors': observed.errors,
                  'source_bytes': sum(a.size for a in assets),
                  'observed_at': now(), **_summary(plan)}


def plan_for_groups(config, store, destination, identity, groups):
    """The same frozen scope for Feishu selection and the camera library."""
    # Read receipts; browsing must not prune dedupe entries or create file tasks.
    archived = {row['asset_id'] for row in store.db.execute(
        "SELECT asset_id FROM source_index WHERE destination=? AND state='uploaded'", (destination,))}
    archived.update(row['asset_id'] for row in store.files(destination=destination) if row['state'] == 'uploaded')
    from .paths import archive_location
    return {'destination_name': config.destination.get('name', 'SMB'),
            'archive_location': archive_location(config.destination, ''), 'groups': [{**{k: v for k, v in group.items() if k != 'members'},
                        'members': [asset.to_dict() for asset in group['members']]}
                       for group in groups],
            'selection_mode': 'files', 'selected': [],
            'archive_previews': config.backup['archive_previews'],
            'serial': config.source['serial'], 'source_type': config.source['type'],
            'archived_ids': sorted(archived), 'identity': identity}


def review_library(store, settings, library, owner, session, catalog, identifiers, key):
    """Create a normal selection draft from a server-owned library snapshot.

    The client supplies identities only. No task, transfer or deletion is started
    until the existing confirm() path accepts this exact preview revision.
    """
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, 'selected:' + owner + ':workspace:' + key).hex
    with settings.lock(), store.db:
        store.db.execute('BEGIN IMMEDIATE')
        existing = get(store.db, identifier, owner)
        if existing:
            plan = json.loads(existing['plan'])
            if plan.get('catalog') != catalog or plan.get('requested_ids') != identifiers:
                raise ConfigError('本次选择已变化，请重新确认备份范围')
            return existing
        item = _settings(settings)
        connection = store.get_meta('source_connection', {})
        if (not fresh(connection) or connection.get('state') != 'approved'
                or connection.get('id') != session):
            raise ConfigError('相机连接已变化，请刷新素材')
        if active(store.db):
            raise ConfigError('已有选定任务正在处理，请先完成或停止该任务')
        groups = library.selection(session, catalog, identifiers)
        config = Config.from_values(item['config'])
        if any((a.source_type, a.serial) != (config.source['type'], config.source['serial'])
               for group in groups for a in group['members']):
            raise ConfigError('相机身份已变化，请刷新素材')
        destination = _binding(store, item)
        plan = plan_for_groups(config, store, destination, connection['device'], groups)
        plan.update(selected=list(range(len(groups))), origin='camera_workspace',
                    catalog=catalog, requested_ids=identifiers)
        result = _summary(plan)
        if not result['total_files']:
            raise ConfigError('请至少选择一个需要备份的文件')
        stamp = time.time()
        store.db.execute("""INSERT INTO selected_imports
            (id,owner,state,revision,session_id,destination,created,updated,plan,result)
            VALUES (?,?,'ready',?,?,?,?,?,?,?)""", (identifier, owner, item['revision'], session,
                destination, stamp, stamp, json.dumps(plan, ensure_ascii=False), json.dumps(result)))
    return get(store.db, identifier, owner)


def _save(store, op, state, result, plan=None, retry_at=0):
    with store.db:
        store.db.execute("""UPDATE selected_imports SET state=?,result=?,plan=COALESCE(?,plan),
            updated=?,retry_at=? WHERE id=?""",
            (state, json.dumps(result, ensure_ascii=False),
             json.dumps(plan, ensure_ascii=False) if plan is not None else None,
             time.time(), retry_at, op['id']))
    if state not in ('choosing', 'ready') and (plan or op.get('plan')):
        current_plan = plan or json.loads(op['plan'])
        if current_plan.get('confirmed'):
            store.status(selected_import={**result, 'id': op['id'], 'state': state,
                'run_id': result.get('run_id'), 'destination': op['destination'],
                'serial': current_plan['serial'], 'source_type': current_plan['source_type'],
                'file_ids': result.get('file_ids', [])})


def process_one(parent, settings, *, browsing_only=False):
    # A lane of the full-sync pipeline may browse at a file boundary, but must
    # never start another transfer pipeline. The full-sync upload lane may
    # still be cancelling after enabled=False and a new selection is confirmed.
    states = "('queued','scanning')" if browsing_only else (
        "('queued','scanning','queued_transfer','running','waiting')")
    row = parent.store.db.execute("SELECT * FROM selected_imports WHERE state IN " + states +
        " AND (retry_at<=? OR cancelled=1 OR drain=1) ORDER BY created LIMIT 1", (time.time(),)).fetchone()
    if not row:
        return False
    op = dict(row)
    store = parent.store
    browsing = op['state'] in ('queued', 'scanning')
    from .camera_coordinator import operation_reason
    if browsing and operation_reason(store, 'browse'):
        return False
    control = SelectionControl(parent, settings, op, browsing)
    from .liveness import CombinedStop
    combined = CombinedStop(parent.stop, control.transfer_stop)
    source = parent.source.fork(combined) if hasattr(parent.source, 'fork') else parent.source
    remote = parent.destination.fork(stop=combined) if hasattr(parent.destination, 'fork') else parent.destination
    from .service import Service
    plan = json.loads(op['plan'] or '{}')
    result = json.loads(op['result'])
    scanning = False
    previous_activity = getattr(source, 'on_activity', None)
    if browsing:
        def scan_activity():
            control.boundary()
            if parent.activity:
                parent.activity.touch('import')
        source.on_activity = scan_activity
    try:
        control.boundary()
        if parent.destination_id != op['destination']:
            raise Cancelled('归档位置已变化，原任务与缓存保留')
        if browsing:
            _save(store, op, 'scanning', result)
            store.status(maintenance_running=True, source_work=True)
            scanning = True
            service = Service(parent.config, store, source, remote, parent.stop, control=control)
            plan, result = _scan(service, op, settings)
            control.boundary()
            _save(store, op, 'choosing', result, plan)
        else:
            from .selected_transfer import SelectedService
            if (parent.config.source['type'], parent.config.source['serial']) != (plan['source_type'], plan['serial']):
                raise Cancelled('等待原任务对应的相机，未处理当前设备')
            plan['confirmed'] = True
            selection = {'id': op['id'], 'groups': [plan['groups'][i] for i in plan['selected']],
                         'archive_previews': plan['archive_previews'],
                         'selection_mode': plan.get('selection_mode', 'groups')}
            service = SelectedService(parent.config, store, source, remote, parent.stop,
                                      control=control, selection=selection)
            service.cache_guard = parent.cache_guard
            result = {**result, 'run_id': service.run_id}
            _save(store, op, 'running', result, plan)
            result.pop('error', None)
            result.pop('reason', None)
            result.update(service.cycle())
            # Keep the truthful scope summary even if the caller's media_scope is different.
            result = {**_summary(plan), **result}
            if result.get('complete'):
                _save(store, op, 'done', result, plan)
                service.settle()
            else:
                control.boundary()
                _save(store, op, 'waiting', result, plan,
                      time.time() + parent.config.backup['retry_seconds'])
    except (BackupError, OSError, ValueError, sqlite3.Error) as exc:
        message = remote.redact(str(exc)) if hasattr(remote, 'redact') else str(exc)
        result.update(error=message, reason=message)
        paused = browsing or control.transfer_stop.is_set() or isinstance(exc, Cancelled)
        _save(store, op, 'paused' if paused else 'waiting', result, plan or None,
              0 if paused else time.time() + parent.config.backup['retry_seconds'])
    finally:
        if browsing:
            source.on_activity = previous_activity
        if scanning:
            store.status(maintenance_running=False, source_work=False)
        if remote is not parent.destination:
            remote.close()
    return True


def progress(store, op):
    """Current card counts; never write every progress tick back to SQLite."""
    plan = json.loads(op['plan'] or '{}')
    if not plan.get('confirmed'):
        return op
    result = {**_summary(plan), **json.loads(op['result'])}
    result['import_bytes'] = result.get('imported_bytes', result.get('import_bytes', 0))
    result['upload_bytes'] = result.get('uploaded_bytes', result.get('upload_bytes', 0))
    checks = store.get_meta('selected_archive_checks', {})
    verified = set(checks.get('verified_file_ids', []) if checks.get('id') == op['id']
                   else result.get('archive_verified_ids', []))
    if checks.get('id') == op['id']:
        errors = checks.get('errors', {})
        if isinstance(errors, dict):
            result['archive_errors'] = [
                {'file': store.asset(row).path if (row := store.get(key)) else key, 'reason': reason}
                for key, reason in errors.items()]
        else:
            result['archive_errors'] = errors
        result['archive_verified_ids'] = sorted(verified)
    run_id = result.get('run_id')
    rows = [dict(row) for row in store.db.execute("""SELECT f.* FROM files f
        JOIN transfer_run_files r ON r.file_id=f.id WHERE r.run_id=?""", (run_id,))]
    if rows:
        counted = [row for row in rows if row['state'] != 'excluded']
        from .storage import inspect_cache, observe_cached_file
        cache_identity = store.get_meta('cache_identity')
        cache_available = inspect_cache(store.cache, cache_identity)['available']
        stored = {row['id'] for row in counted if (row['state'] == 'uploaded' and row['id'] in verified) or
                  (cache_available and observe_cached_file(
                      store.cached_path(row), json.loads(row['cache_identity'] or 'null'),
                      row['asset_id'], cache_identity)['valid'])}
        result.update(total_files=len(counted),
                      total_bytes=sum(json.loads(r['asset'])['size'] for r in counted),
                      imported_files=sum(r['id'] in stored for r in counted),
                      uploaded_files=sum(r['state'] == 'uploaded' and r['id'] in verified for r in counted),
                      import_bytes=sum(json.loads(r['asset'])['size'] for r in counted
                                       if r['id'] in stored),
                      upload_bytes=sum(json.loads(r['asset'])['size'] for r in counted
                                       if r['state'] == 'uploaded' and r['id'] in verified))
        result['archive_verification_pending'] = sum(r['state'] == 'uploaded' and r['id'] not in verified
                                                    for r in counted)
    if op['state'] in ('running', 'waiting'):
        stages = store.get_meta('status', {}).get('stages', {})
        result['stages'] = stages
        if rows:
            for lane, field, complete in (
                    ('import', 'import_bytes', stored),
                    ('upload', 'upload_bytes', {r['id'] for r in counted if r['state'] == 'uploaded'})):
                stage = stages.get(lane, {})
                current = next((r for r in counted if r['id'] == stage.get('current_file_id')), None)
                if current and current['id'] not in complete and stage.get('phase') in ('downloading', 'uploading'):
                    result[field] += min(json.loads(current['asset'])['size'], max(0, stage.get('current_bytes', 0)))
    return {**op, 'result': json.dumps(result, ensure_ascii=False)}


def prune(db):
    db.execute("""DELETE FROM selected_imports WHERE updated<? AND
        (state IN ('choosing','ready','done') OR
         (state='paused' AND COALESCE(json_extract(plan,'$.confirmed'),0)=0))""",
               (time.time() - 86400,))
