"""One connected source, one explicit approval; discovery belongs to its adapter."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .model import ConfigError, SourceBlocked, now


KEY = 'source_connection'


class ConfirmationRequired(SourceBlocked):
    pass


def read(directory, key=KEY):
    path = Path(directory) / 'state.sqlite3'
    if not path.is_file():
        return {}
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            return {}
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        db.close()


def selected(directory, source):
    binding = read(directory, 'source_selection')
    if not binding or binding.get('type') != source['type']:
        return source
    return {**source, 'url': binding['url'], 'serial': binding['serial_number']}


def fresh(state, stamp=None):
    stamp = time.time() if stamp is None else stamp
    return bool(state.get('online') and 0 <= stamp - state.get('last_seen', 0) <= 45)


def deletion_enabled(config, selection):
    """Keep the user's preference; unverified selected devices retain originals."""
    same = (selection.get('serial_number') == config.source['serial'] and
            selection.get('type') == config.source['type'])
    return bool(config.backup['delete_source_after_import'] and
                config.source.get('transport') != 'wifi' and selection.get('transport') != 'wifi' and
                (not same or selection.get('compatibility', {}).get('delete_verified', False)))


def reason(store, config, *, deleting=False):
    state = store.get_meta(KEY, {})
    if not state:  # Low-level library tests/maintenance have no discovery session.
        return None
    device = state.get('device', {})
    if (state.get('state') != 'approved' or not fresh(state) or
            device.get('type') != config.source['type'] or
            device.get('serial_number') != config.source['serial'] or not device.get('http_ready', True)):
        return ('等待飞书确认当前相机；已有完整缓存可继续上传' if state.get('state') in ('pending', 'declined')
                else '等待相机连接；已有完整缓存可继续上传')
    if deleting and (device.get('transport') != 'usb' or not device.get('compatibility', {}).get('delete_verified')):
        return '此连接方式、型号或固件尚未验证原片删除，当前保留原片'
    return None


def require(store, config):
    waiting = reason(store, config)
    if waiting:
        raise ConfirmationRequired(waiting)


def _identity(device):
    return tuple(device.get(key) for key in ('type', 'serial_number'))


def _session(device):
    return _identity(device) + tuple(device.get(key) for key in ('url', 'transport', 'firmware_version', 'http_ready'))


def restore(store, source):
    """Keep approval for the same camera across process start, and retire the old card."""
    state = store.get_meta(KEY, {})
    device = state.get('device') or {}
    same = (state.get('state') == 'approved' and device.get('type') == source.get('type')
            and device.get('serial_number') and device.get('serial_number') == source.get('serial'))
    if not same:
        store.set_meta(KEY, {'state': 'searching', 'online': False})
        return store.get_meta(KEY)
    store.set_meta(KEY, {**state, 'id': uuid.uuid4().hex, 'state': 'approved', 'online': False,
                         'last_seen': 0, 'failures': 0, 'missing_since': None, 'restored': True})
    return store.get_meta(KEY)


def observe(store, device, stamp=None):
    """Preserve decisions across successful probes; revoke after sustained loss."""
    stamp = time.time() if stamp is None else stamp
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        state = store.get_meta(KEY, {})
        old = state.get('device', {})
        if device:
            same = _session(old) == _session(device)
            binding = store.get_meta('source_selection', {})
            trusted = bool(device.get('serial_number') and _identity(binding) == _identity(device))
            # A restart may mark the camera offline before it is seen again.
            # The same serial stays approved; a new session id already retired the old card.
            if state.get('restored') and same and old.get('serial_number'):
                state.pop('restored', None)
                state['state'] = 'approved'
            elif state.get('state') in ('searching', 'offline', None) or not same:
                state = {'id': uuid.uuid4().hex, 'state': 'approved' if trusted else 'pending',
                         'device': device, 'created_at': now()}
                status = store.get_meta('status', {})
                if status:
                    status['inventory'] = {**status.get('inventory', {}), 'scanned': False, 'verified': False,
                                           'invalidated_reason': '相机连接变化，需重新核验清单'}
                    store.db.execute("UPDATE meta SET value=? WHERE key='status'", (json.dumps(status),))
            else:
                state.pop('restored', None)
            state.update(device=device, online=True, last_seen=stamp, failures=0, missing_since=None)
        elif old:
            state['failures'] = state.get('failures', 0) + 1
            state['missing_since'] = state.get('missing_since') or stamp
            if state['failures'] >= 3 and stamp - state['missing_since'] >= 20:
                state.update(state='offline', online=False)
        else:
            state = {'state': 'searching', 'online': False}
        store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (KEY, json.dumps(state)))
    return state


def _other_camera_busy(store, config, device):
    selection = {}
    raw = store.db.execute("SELECT value FROM meta WHERE key='source_selection'").fetchone()
    if raw:
        selection = json.loads(raw[0])
    delete_ready = bool(config['backup']['delete_source_after_import'] and
                        selection.get('compatibility', {}).get('delete_verified'))
    open_temps = {item['file_id'] for item in store.temporary_items()}
    for row in store.files():
        asset = store.asset(row)
        if (asset.source_type, asset.serial) == (device['type'], device['serial_number']):
            continue
        owes_delete = (delete_ready and selection.get('type') == asset.source_type and
                       selection.get('serial_number') == asset.serial)
        if row['id'] in open_temps or _unfinished_row(row, owes_delete):
            return True
    return False


def _unfinished_row(row, owes_delete):
    if row['state'] not in ('uploaded', 'excluded'):
        return True
    if row['state'] == 'uploaded' and not all(row[key] for key in ('remote_path', 'remote_identity', 'cache_cleared_at')):
        return True
    if row.get('cache_cleanup_error') or row['cleanup'] == 'deleting':
        return True
    return bool(owes_delete and (row.get('source_cleanup_error') or row['cleanup'] not in ('deleted', 'absent')))


def decide(store, settings, session_id, accept):
    """Only called by authenticated, deduplicated bot jobs; stale cards do nothing."""
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        state = store.get_meta(KEY, {})
        if state.get('id') != session_id or not fresh(state) or state.get('state') == 'offline':
            raise ConfigError('相机连接已变化，请通过状态菜单重新确认')
        if accept:
            configured = settings.read()
            if configured.get('error'):
                raise ConfigError(configured['error'])
            if state['device'].get('type') != configured['config']['source']['type']:
                raise ConfigError('来源类型已变化，请等待重新识别并确认设备')
        if state.get('state') == 'approved':
            if not accept:
                raise ConfigError('本次连接已经确认；如需停止传输，请使用“停止备份”')
            return False  # Replaying a confirmation must never re-enable stopped sync.
        if not accept:
            state['state'] = 'declined'
        else:
            device = state['device']
            if not device['compatibility']['import_supported']:
                raise ConfigError('此型号尚未适配，未开启备份')
            # One active camera only. Completed work awaiting retirement does not block a switch.
            if _other_camera_busy(store, configured['config'], device):
                raise ConfigError('另一台相机仍有传输或清理事项，请先完成原任务后再切换设备')
            # Device authorization never changes the user's synchronization choice.
            state['state'] = 'approved'
            state['approved_at'] = now()
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                             ('source_selection', json.dumps(device)))
        store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (KEY, json.dumps(state)))
    return True


def run(directory, settings, stop):
    from .connections import ConnectionManager
    from .store import Store
    store = Store(directory)
    try:
        manager = ConnectionManager(store, settings, stop)
        while not stop.is_set():
            manager.tick()
            stop.wait(5)
    finally:
        store.close()
