"""Short-lived, owner-authorized camera commands; never replay shutter intents."""
import json
import time
import uuid

from .model import BackupError, ConfigError, now
from .source_connection import fresh

COMMANDS = {'state', 'start_recording', 'stop_recording', 'photo', 'mode_video', 'mode_photo'}
LABELS = {'state': '相机状态已更新', 'start_recording': '录像已开始', 'stop_recording': '录像已停止',
          'photo': '拍照已完成', 'mode_video': '已切换视频模式', 'mode_photo': '已切换照片模式'}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS capture_requests (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, command TEXT NOT NULL,
        session_id TEXT NOT NULL, device_type TEXT NOT NULL, serial TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL, updated REAL NOT NULL,
        result TEXT NOT NULL DEFAULT '{}')''')


def pending(db):
    return db.execute("SELECT 1 FROM capture_requests WHERE state IN ('queued','running') LIMIT 1").fetchone() is not None


def get(db, identifier, owner):
    row = db.execute('SELECT * FROM capture_requests WHERE id=? AND owner=?', (identifier, owner)).fetchone()
    return dict(row) if row else None


def capabilities(device):
    from .adapters import source_controls
    return source_controls(device)


def allowed_commands(store, state):
    device = state.get('device', {})
    commands = capabilities(device)['commands']
    if state.get('state') == 'approved' and fresh(state):
        return commands
    binding = store.get_meta('source_selection', {})
    trusted = (device.get('serial_number') and
               all(device.get(k) == binding.get(k) for k in ('type', 'serial_number')))
    if state.get('id') and state.get('state') in ('approved', 'offline') and trusted:
        # Media reachability is not a prerequisite for reading BLE state or
        # stopping capture. The child freshly checks the complete BLE identity.
        return [c for c in commands if c in ('state', 'stop_recording')]
    return []


def enqueue(store, owner, key, command, session_id):
    if command not in COMMANDS:
        raise ConfigError('相机操作不存在')
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, owner + ':' + key).hex
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        previous = get(store.db, identifier, owner)
        if previous:
            return previous
        state = store.get_meta('source_connection', {})
        device = state.get('device', {})
        if state.get('id') != session_id or command not in allowed_commands(store, state):
            raise ConfigError('相机连接已变化，请刷新相机控制卡')
        support = capabilities(device)
        if command not in support['commands']:
            raise ConfigError(support['reason'] or '此相机暂不支持该操作')
        if pending(store.db):
            raise ConfigError('已有相机控制操作正在执行，请等待结果')
        stamp = time.time()
        store.db.execute('''INSERT INTO capture_requests
            (id,owner,command,session_id,device_type,serial,created,updated) VALUES (?,?,?,?,?,?,?,?)''',
            (identifier, owner, command, session_id, device['type'], device['serial_number'], stamp, stamp))
    return get(store.db, identifier, owner)


def _finish(store, op, result):
    with store.db:
        store.db.execute("UPDATE capture_requests SET state='done',updated=?,result=? WHERE id=?",
                         (time.time(), json.dumps(result, ensure_ascii=False), op['id']))
    state = {**result, 'command': op['command'], 'serial': op['serial'], 'observed_at': now()}
    store.set_meta('camera_control', state)


def process_one(service, settings):
    store = service.store
    row = store.db.execute("SELECT * FROM capture_requests WHERE state IN ('queued','running') ORDER BY created LIMIT 1").fetchone()
    if row is None:
        return False
    op = dict(row)
    store.status(capture_work=True)
    try:
        state = store.get_meta('source_connection', {})
        device = state.get('device', {})
        required = 'state' if op['state'] == 'running' else op['command']
        if (required not in allowed_commands(store, state) or
                (device.get('type'), device.get('serial_number')) != (op['device_type'], op['serial'])):
            raise ConfigError('相机连接不可用；未重发拍摄命令，请重新连接后刷新状态')
        if op['state'] == 'running':
            # A process died after persisting intent. Read state, never repeat a shutter write.
            from .adapters import source_link
            observed = source_link(op['device_type'], 'state', op['serial'], service.stop)
            _finish(store, op, {**observed, 'uncertain': True, 'reason': '服务曾中断；已重新读取状态，原命令未重发'})
            return True
        if state.get('id') != op['session_id'] or time.time() - op['created'] > 30:
            raise ConfigError('操作已过期或相机会话已变化，未执行；请刷新控制卡')
        support = capabilities(device)
        if op['command'] not in support['commands']:
            raise ConfigError(support['reason'] or '当前连接不支持该操作')
        with store.db:
            store.db.execute("UPDATE capture_requests SET state='running',updated=? WHERE id=?", (time.time(), op['id']))
        from .adapters import source_link
        result = source_link(op['device_type'], op['command'], op['serial'], service.stop)
        _finish(store, op, result)
        if op['command'] != 'state':
            store.invalidate_inventory('拍摄状态已变化，需重新核验素材清单')
    except (BackupError, OSError, ValueError) as exc:
        # Configuration errors occur before dispatch. Everything else may have
        # reached the camera and must not be reported as definitively unexecuted.
        current = get(store.db, op['id'], op['owner'])
        dispatched = current['state'] == 'running'
        message = str(exc) if isinstance(exc, (BackupError, ConfigError)) else '相机控制暂不可用，请刷新状态核对'
        _finish(store, op, {'error': message, 'uncertain': dispatched})
    finally:
        store.status(capture_work=False, source_work=False)
    return True


def prune(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='capture_requests'").fetchone():
        db.execute("DELETE FROM capture_requests WHERE state='done' AND updated<?", (time.time() - 86400,))
