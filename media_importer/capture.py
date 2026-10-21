"""Short-lived, owner-authorized camera commands; never replay shutter intents."""
import json
import time
import uuid

from .model import BackupError, ConfigError, now
from .source_connection import fresh

PREVIEW_COMMANDS = {'preview_record': 'start_recording', 'preview_photo': 'photo', 'preview_timelapse': 'start_timelapse'}
READ_COMMANDS = {'state', 'catalog'}
COMMANDS = {'state', 'catalog', 'load_preset', 'set_parameter', 'start_timelapse', 'hilight',
            'start_recording', 'stop_recording', 'photo', 'mode_video', 'mode_photo', *PREVIEW_COMMANDS}
LABELS = {'state': '相机状态已更新', 'start_recording': '录像已开始', 'stop_recording': '录像已停止',
          'photo': '拍照已完成', 'mode_video': '已切换视频模式', 'mode_photo': '已切换照片模式'}
LABELS['preview_record'] = '预览已结束，录像已开始'
LABELS.update(catalog='拍摄设置已读取', load_preset='拍摄预设已切换', set_parameter='拍摄参数已更新',
              start_timelapse='延时摄影已开始', hilight='HiLight 标记已添加',
              preview_photo='预览已结束，拍照已完成', preview_timelapse='预览已结束，延时摄影已开始')
OBSERVATIONS = ('recording', 'mode', 'busy', 'observed_at', 'preset', 'preset_label', 'specification',
                'recording_seconds', 'remaining_video_seconds', 'battery_percent',
                'storage_remaining_bytes', 'overheating', 'storage_status')


def validate_parameters(command, parameters):
    if not isinstance(parameters, dict):
        raise ConfigError('拍摄参数无效，请重新打开拍摄设置')
    expected = {'preset'} if command == 'load_preset' else {'key', 'value', 'expected_preset'} if command == 'set_parameter' else set()
    if set(parameters) != expected:
        raise ConfigError('拍摄参数不完整，请重新打开拍摄设置')
    if command == 'set_parameter':
        key = parameters['key']
        if not isinstance(key, str) or not key.isascii() or not key.replace('_', '').isalnum() or len(key) > 40:
            raise ConfigError('拍摄参数标识无效')
    for key in expected - {'key'}:
        if type(parameters[key]) is not int or not 0 <= parameters[key] <= (255 if key == 'value' else 0xffffffff):
            raise ConfigError('拍摄参数值无效')
    return parameters


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS capture_requests (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, command TEXT NOT NULL,
        session_id TEXT NOT NULL, device_type TEXT NOT NULL, serial TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL, updated REAL NOT NULL,
        result TEXT NOT NULL DEFAULT '{}')''')
    if 'parameters' not in {r[1] for r in db.execute('PRAGMA table_info(capture_requests)')}:
        db.execute("ALTER TABLE capture_requests ADD COLUMN parameters TEXT NOT NULL DEFAULT '{}'")


def pending(db):
    return db.execute("SELECT 1 FROM capture_requests WHERE state IN ('queued','running') LIMIT 1").fetchone() is not None


def mutating_pending(db):
    return db.execute("SELECT 1 FROM capture_requests WHERE state IN ('queued','running') AND command NOT IN ('state','catalog') LIMIT 1").fetchone() is not None


def observation(store, device):
    """A refresh never interrupts media IO or disguises an old observation as new."""
    from datetime import datetime
    candidates = []
    control = store.get_meta('camera_control', {})
    if control.get('serial') == device.get('serial_number') and not control.get('error'):
        candidates.append(control)
    source = store.get_meta('status', {}).get('source', {})
    if source.get('serial_number') == device.get('serial_number'):
        telemetry = source.get('telemetry') or {}
        if telemetry.get('available'):
            candidates.append({k: telemetry[k] for k in OBSERVATIONS if k in telemetry})
    observations = []
    for state in candidates:
        try:
            if 0 <= time.time() - datetime.fromisoformat(state['observed_at']).timestamp() < 45:
                observations.append(state)
        except (KeyError, ValueError, TypeError):
            pass
    if observations:
        merged = {}
        for state in sorted(observations, key=lambda item: datetime.fromisoformat(item['observed_at']).timestamp()):
            if any(key in state and key in merged and state[key] != merged[key] for key in ('mode', 'preset')):
                for key in ('specification', 'preset_label', 'recording_seconds'):
                    merged.pop(key, None)
            merged.update({k: v for k, v in state.items() if k in OBSERVATIONS})
        return merged
    return {'stale': True, 'reason': '暂无新鲜拍摄状态，等待后台观测；当前传输不受影响'}


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


def enqueue(store, owner, key, command, session_id, parameters=None):
    if command not in COMMANDS:
        raise ConfigError('相机操作不存在')
    from .live_preview import active
    parameters = validate_parameters(command, parameters or {})
    if active(store) and command not in READ_COMMANDS | {'stop_recording', *PREVIEW_COMMANDS}:
        raise ConfigError('实时预览尚未结束，请先在预览卡片点击「结束预览」')
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, owner + ':' + key).hex
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        previous = get(store.db, identifier, owner)
        if previous:
            return previous
        state = store.get_meta('source_connection', {})
        device = state.get('device', {})
        actual = PREVIEW_COMMANDS.get(command, command)
        if state.get('id') != session_id or actual not in allowed_commands(store, state):
            raise ConfigError('相机连接已变化，请刷新相机控制卡')
        support = capabilities(device)
        if actual not in support['commands']:
            raise ConfigError(support['reason'] or '此相机暂不支持该操作')
        if pending(store.db) and command not in READ_COMMANDS | {'stop_recording'}:
            raise ConfigError('已有相机控制操作正在执行，请等待结果')
        stamp = time.time()
        if command == 'stop_recording':
            # A queued start must not run after the newer stop intent.
            store.db.execute("UPDATE capture_requests SET state='done',updated=?,result=? WHERE state='queued' AND command NOT IN ('state','stop_recording')",
                             (stamp, json.dumps({'reason': '已由停止录像操作取消，未执行', 'cancelled': True})))
        store.db.execute('''INSERT INTO capture_requests
            (id,owner,command,session_id,device_type,serial,created,updated,parameters) VALUES (?,?,?,?,?,?,?,?,?)''',
            (identifier, owner, command, session_id, device['type'], device['serial_number'], stamp, stamp, json.dumps(parameters)))
        if command in PREVIEW_COMMANDS:
            current = store.get_meta('live_preview', {})
            if not active(store) or current.get('serial') != device['serial_number']:
                raise ConfigError('预览会话已变化，请刷新控制卡')
            current.update(stop_requested=True, url=None, local_url=None, observed_at=now())
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('live_preview', json.dumps(current)))
    return get(store.db, identifier, owner)


def _finish(store, op, result):
    with store.db:
        store.db.execute("UPDATE capture_requests SET state='done',updated=?,result=? WHERE id=?",
                         (time.time(), json.dumps(result, ensure_ascii=False), op['id']))
    state = {'observed_at': now(), **result, 'command': op['command'], 'serial': op['serial']}
    store.set_meta('camera_control', state)
    if op['command'] == 'catalog' and not result.get('error'):
        store.set_meta('camera_catalog', {**state, 'session_id': op['session_id']})


def process_one(service, settings):
    store = service.store
    from .live_preview import active
    row = store.db.execute("SELECT * FROM capture_requests WHERE state IN ('queued','running') ORDER BY (state='running') DESC,(command='stop_recording') DESC,created LIMIT 1").fetchone()
    if row is None:
        from .capture_sessions import settle
        return settle(service)
    op = dict(row)
    if active(store) and op['command'] not in READ_COMMANDS | {'stop_recording'}:
        return False
    store.status(capture_work=True)
    try:
        state = store.get_meta('source_connection', {})
        device = state.get('device', {})
        actual = PREVIEW_COMMANDS.get(op['command'], op['command'])
        parameters = validate_parameters(actual, json.loads(op.get('parameters') or '{}'))
        required = 'state' if op['state'] == 'running' else actual
        if (required not in allowed_commands(store, state) or
                (device.get('type'), device.get('serial_number')) != (op['device_type'], op['serial'])):
            raise ConfigError('相机连接不可用；未重发拍摄命令，请重新连接后刷新状态')
        if op['state'] == 'running':
            # A process died after persisting intent. Read state, never repeat a shutter write.
            from .adapters import source_link
            observed = source_link(op['device_type'], 'state', op['serial'], service.stop)
            _finish(store, op, {**observed, 'uncertain': True, 'reason': '服务曾中断；已重新读取状态，原命令未重发'})
            return True
        if state.get('id') != op['session_id'] or time.time() - op['created'] > (300 if actual in READ_COMMANDS else 30):
            raise ConfigError('操作已过期或相机会话已变化，未执行；请刷新控制卡')
        support = capabilities(device)
        if actual not in support['commands']:
            raise ConfigError(support['reason'] or '当前连接不支持该操作')
        from .capture_sessions import prepare, record_result
        prepare(service, op, actual)
        if actual not in READ_COMMANDS and time.time() - op['created'] > 30:
            raise ConfigError('拍摄准备超过有效期，未执行；请重新操作')
        with store.db:
            store.db.execute("UPDATE capture_requests SET state='running',updated=? WHERE id=?", (time.time(), op['id']))
        from .adapters import source_link
        result = (source_link(op['device_type'], actual, op['serial'], service.stop, parameters=parameters)
                  if parameters else source_link(op['device_type'], actual, op['serial'], service.stop))
        _finish(store, op, result)
        record_result(store, op, actual, result)
        if op['command'] not in READ_COMMANDS:
            store.invalidate_inventory('拍摄状态已变化，需重新核验素材清单')
    except (BackupError, OSError, ValueError) as exc:
        # Configuration errors occur before dispatch. Everything else may have
        # reached the camera and must not be reported as definitively unexecuted.
        current = get(store.db, op['id'], op['owner'])
        dispatched = current['state'] == 'running' and op['command'] not in READ_COMMANDS
        message = str(exc) if isinstance(exc, (BackupError, ConfigError)) else '相机控制暂不可用，请刷新状态核对'
        _finish(store, op, {'error': message, 'uncertain': dispatched})
        from .capture_sessions import record_result
        record_result(store, op, PREVIEW_COMMANDS.get(op['command'], op['command']), {'error': message, 'uncertain': dispatched})
    finally:
        store.status(capture_work=False)
    return True


def prune(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='capture_requests'").fetchone():
        db.execute("DELETE FROM capture_requests WHERE state='done' AND updated<?", (time.time() - 86400,))
