"""One recent shooting scope, not a history database or a second transfer engine."""
import json
import time

from .model import BackupError, ConfigError, now

KEY = 'capture_session'
STARTS = {'start_recording', 'photo', 'start_timelapse'}


def snapshot(service):
    # Explicit shooting is independent of the sync switch. Keep daemon shutdown
    # cancellation and the usual identity checks, without borrowing a stopped
    # import lane's cancellation event.
    source = service.source.fork(service.stop) if hasattr(service.source, 'fork') else service.source
    return service.source_snapshot(source)


def prepare(service, op, command):
    if command not in STARTS:
        return
    current = service.store.get_meta(KEY, {})
    if current.get('serial') == op['serial'] and current.get('state') in ('prepared', 'capturing', 'uncertain'):
        # Let the camera decide whether a repeated start is already satisfied;
        # never replace the baseline of an active recording.
        return
    assets = snapshot(service)
    service.store.set_meta(KEY, {'id': op['id'], 'owner': op['owner'], 'serial': op['serial'],
        'device_type': op['device_type'], 'command': command, 'state': 'prepared',
        'before': {a.path: a.identity for a in assets}, 'started': time.time(), 'observed_at': now(),
        'retry_at': 0, 'assets': [], 'reason': None})


def record_result(store, op, command, result):
    current = store.get_meta(KEY, {})
    if current.get('serial') != op['serial'] or current.get('device_type') != op['device_type']:
        return
    if command not in STARTS | {'stop_recording', 'state'}:
        return
    if current.get('state') in ('ready', 'empty', 'not_started'):
        return
    if result.get('error') and not result.get('uncertain') and current.get('id') == op['id']:
        current.update(state='not_started', reason='拍摄未开始；可重新操作')
        store.set_meta(KEY, current)
        return
    if result.get('uncertain') or result.get('error'):
        current.update(state='uncertain', reason='拍摄结果待核验；不会自动重复拍摄')
    elif result.get('recording') is True:
        current.update(state='capturing', reason=None)
    elif result.get('recording') is False and not result.get('busy', False):
        if current.get('state') == 'ready': return
        current.update(state='awaiting_media', reason='拍摄已结束，等待相机完成文件保存')
    current.update(observed_at=now(), retry_at=time.time() + 2)
    store.set_meta(KEY, current)


def settle(service):
    """Read the completed inventory only after a fresh idle observation."""
    store = service.store
    current = store.get_meta(KEY, {})
    if current.get('state') not in ('prepared', 'capturing', 'uncertain', 'awaiting_media'):
        return False
    if time.time() < current.get('retry_at', 0): return False
    from .capture import observation
    from .live_preview import active
    connection = store.get_meta('source_connection', {})
    device = connection.get('device', {})
    if (active(store) or current.get('serial') != device.get('serial_number') or
            current.get('device_type') != device.get('type')):
        return False
    observed = observation(store, device)
    if observed.get('recording') is not False or observed.get('busy') is not False:
        return False
    try:
        assets = snapshot(service)
        # Identity changes at an existing path are not automatically attributed
        # to this shoot. The regular browser can inspect them separately.
        added = [a for a in assets if a.path not in current['before']]
        if not added:
            current.setdefault('idle_since', time.time())
            expired = time.time() - current['idle_since'] > 120
            current.update(state='empty' if expired else 'awaiting_media',
                           reason='未发现本次新增素材，请在「选择素材」核对' if expired else '相机已停止拍摄，尚未发现新的完整文件')
        else:
            from .adapters import source_selection_groups
            # Include only groups wholly belonging to the newly observed set.
            groups = source_selection_groups(service.source, added)
            current.update(state='ready', assets=[a.to_dict() for a in added],
                groups=[{**g, 'members': [a.to_dict() for a in g['members']]} for g in groups],
                reason=None, completed_at=now())
            current.pop('before', None)
    except (BackupError, OSError, ValueError):
        current.update(reason='拍摄结束后的素材清单暂不可用，连接恢复后重新核验')
    current.update(observed_at=now(), retry_at=time.time() + 15)
    store.set_meta(KEY, current)
    return False  # The existing importer/upload queues remain independent.


def selection(store, settings, owner, identifier):
    """Create a normal reviewable selected import; never enable global sync."""
    from .selected_imports import get, active, _settings, _binding, _summary
    from .source_connection import fresh
    current = store.get_meta(KEY, {})
    if (current.get('id') != identifier or current.get('owner') != owner or
            current.get('state') != 'ready' or time.time() - current.get('started', 0) > 86400):
        raise ConfigError('本次拍摄清单已失效，请从「选择素材」重新浏览')
    item = _settings(settings)
    connection = store.get_meta('source_connection', {})
    device = connection.get('device', {})
    if (connection.get('state') != 'approved' or not fresh(connection) or
            device.get('serial_number') != current['serial'] or device.get('type') != current['device_type']):
        raise ConfigError('请连接并确认本次拍摄的相机')
    if item['config']['backup']['enabled']:
        raise ConfigError('全量备份已开启，新素材会按现有规则导入；可在「备份」查看进度')
    import uuid
    task_id = uuid.uuid5(uuid.NAMESPACE_URL, 'capture-' + identifier).hex
    previous = get(store.db, task_id, owner)
    if previous: return previous
    if active(store.db): raise ConfigError('已有选定任务正在执行，请先完成或停止该任务')
    plan = {'groups': current['groups'], 'selection_mode': 'files',
            'selected': list(range(len(current['groups']))),
            'archive_previews': item['config']['backup']['archive_previews'],
            'serial': current['serial'], 'source_type': current['device_type'], 'archived_ids': []}
    stamp = time.time()
    with store.db:
        store.db.execute('''INSERT INTO selected_imports
            (id,owner,state,revision,session_id,destination,created,updated,plan,result)
            VALUES (?,?,'ready',?,?,?,?,?,?,?)''', (task_id, owner, item['revision'], connection['id'],
                _binding(store, item), stamp, stamp, json.dumps(plan), json.dumps(_summary(plan))))
    return get(store.db, task_id, owner)
