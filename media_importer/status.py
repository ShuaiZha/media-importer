"""Consistent, read-only views of durable tasks and both live transfer stages."""
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .model import now, Asset
from .store import cache_bytes
from .storage import inspect_cache, inspect_cached_file
from .paths import archive_location
from .media import MEDIA_SCOPES, selected_file_ids


def age(stamp):
    try:
        return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds())
    except (ValueError, TypeError):
        return float('inf')


def cleanup_summary(files, delete_source=False):
    """Observed outcomes, kept separate from the user's deletion preference."""
    previews = [f for f in files if f['state'] == 'excluded']
    files = [f for f in files if f['state'] != 'excluded']
    archived = [f for f in files if f['state'] == 'uploaded']
    ready_groups = {f.get('group_id') for f in files if f.get('group_id') and f.get('imported')}
    ready_groups -= {f.get('group_id') for f in files if not f.get('imported')}
    return {
        'preview_deleted': sum(f['cleanup'] == 'deleted' for f in previews),
        'preview_absent': sum(f['cleanup'] == 'absent' for f in previews),
        'preview_pending': sum(delete_source and f.get('cleanup_selected', True)
                               and (f.get('group_id') in ready_groups or bool(f.get('source_cleanup_error')))
                               and f['cleanup'] not in ('deleted', 'absent') for f in previews),
        'source_deleted': sum(f['cleanup'] == 'deleted' for f in files),
        'source_absent': sum(f['cleanup'] == 'absent' for f in files),
        'source_pending': sum(delete_source and f.get('selected', True) and f.get('imported')
                              and f['cleanup'] not in ('deleted', 'absent') for f in files),
        'source_not_deleted': sum(f['cleanup'] not in ('deleted', 'absent') for f in files),
        'cache_cleared': sum(f.get('cache_state') == 'missing' and not f.get('cache_cleanup_error') for f in archived),
        'cache_pending': sum(f.get('cache_state') in ('verified', 'unverified') or bool(f.get('cache_cleanup_error')) for f in archived),
        'cache_unknown': sum(f.get('cache_state') in (None, 'unavailable') and not f.get('cache_cleanup_error') for f in archived),
    }


def attention(live, enabled, configuration, health, files, stages):
    """Explain whether waiting is enough; do not turn normal pauses into faults."""
    if configuration.get('error'):
        return {'required': True, 'message': '需要处理：配置无效，请检查 .env 和 settings.json。'}
    if not live:
        return {'required': True, 'message': '需要处理：传输服务未响应，请在本机检查 Docker。'}
    uncertain = [f for f in files if f['cache_state'] == 'unverified' and f['state'] != 'uploaded']
    if uncertain:
        return {'required': True, 'message': f'需要处理：{len(uncertain)} 个完整缓存待核验，文件已保留，未计入完成。请接回原相机和存储卡，停止同步后点击「核验缓存」。'}
    if any(f['cache_state'] == 'unverified' and f['state'] == 'uploaded' for f in files):
        return {'required': True, 'message': '需要处理：已归档文件的残留缓存身份不符，已保留待核验；归档完成数量不受影响。'}
    if not health['cache']['available']:
        return {'required': True, 'message': '需要处理：请接回原缓存盘并检查 Docker 挂载；不会改用内置磁盘。'}
    missing = [f for f in files if f['state'] in ('cached', 'staged', 'publishing') and f['cache_state'] == 'missing']
    if missing:
        return {'required': True, 'message': f'需要处理：{len(missing)} 个已记录的缓存文件缺失，请检查原缓存盘；尚未归档的原片请保留。'}
    if not enabled:
        return {'required': False, 'message': '无需处理：任务和完整缓存保留；需要继续时从「同步」菜单开启。'}
    for issue in [health['destination'].get('issue')] + [s.get('issue') for s in stages.values()]:
        if not issue:
            continue
        if issue.get('code') in ('authentication', 'permission', 'destination_path', 'destination_full', 'cache_unavailable'):
            return {'required': True, 'message': '需要处理：' + issue['action']}
        if issue.get('code') in ('network', 'source_network', 'source_state', 'cache_capacity'):
            return {'required': False, 'message': '暂时等待，程序会重试：' + issue['action']}
    if any(f['error'] or f['cache_cleanup_error'] or f['source_cleanup_error'] for f in files):
        return {'required': False, 'message': '有文件待重试或待清理，其他可处理文件继续；详情见「状态 → 归档结果」。'}
    return {'required': False, 'message': '无需处理：程序会继续导入和归档，暂时断网会自动重试。'}


def snapshot(directory, settings):
    item = settings.read()
    config = Config.from_values(item['config']) if item['config'] else None
    path = Path(directory) / 'state.sqlite3'
    meta, rows, batch_id, batch_ids, temporaries, batches, rounds = {}, [], None, set(), [], [], []
    receipts = {}
    destination = config.destination_id if config else None
    serial = config.source['serial'] if config else None
    source_type = config.source['type'] if config else None
    media_scope = config.backup['media_scope'] if config else 'all'
    if path.is_file():
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            meta = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM meta')}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='cache_receipts'").fetchone():
                receipts = dict(db.execute('SELECT file_id,imported_at FROM cache_receipts'))
            state = meta.get('status', {})
            destination = state.get('active_destination') or destination
            serial = state.get('active_serial') or serial
            source_type = state.get('active_source_type') or source_type
            rows = [dict(r) for r in db.execute('SELECT * FROM files WHERE destination=? ORDER BY rowid', (destination,))]
            rows = [r for r in rows if json.loads(r['asset'])['serial'] == serial
                    and json.loads(r['asset'])['source_type'] == source_type]
            candidate = db.execute('SELECT id FROM batches WHERE id=? AND destination=?', (state.get('batch_id'), destination)).fetchone()
            if candidate is None and not (state.get('inventory', {}).get('scanned') and state.get('batch_id') is None):
                candidate = db.execute('SELECT id FROM batches WHERE destination=? ORDER BY created_at DESC LIMIT 1', (destination,)).fetchone()
            batch_id = candidate[0] if candidate else None
            batch_ids = {r[0] for r in db.execute('SELECT file_id FROM batch_files WHERE batch_id=?', (batch_id,))}
            for entry in db.execute('SELECT * FROM batches WHERE destination=? ORDER BY updated_at DESC', (destination,)):
                ids = {r[0] for r in db.execute('SELECT file_id FROM batch_files WHERE batch_id=?', (entry['id'],))}
                if ids.intersection(r['id'] for r in rows):
                    batches.append({**dict(entry), 'file_ids': ids})
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='transfer_runs'").fetchone():
                from .runs import read
                rounds = read(db, destination, source_type, serial)
            temporaries = [dict(r) for r in db.execute("SELECT * FROM temporary_cleanup WHERE state!='done'")]
        finally:
            db.close()
    current_round = next((r for r in rounds if not r['historical'] and r['selection'] == media_scope),
                         next((r for r in rounds if r['historical']), None))
    reporting_ids = current_round['file_ids'] if current_round else batch_ids
    reporting_id = current_round['id'] if current_round else batch_id
    state = dict(meta.get('status', {}))
    service = meta.get('service', {})
    live = bool(service.get('running') and age(state.get('worker_heartbeat_at', state.get('updated_at'))) < 15)
    enabled = bool(config and config.backup['enabled'])
    draining = bool(enabled and config.backup.get('stop_after_current'))
    stages = {name: dict(state.get('stages', {}).get(name, {})) for name in ('import', 'upload')}
    controls = {}
    for name, progress in stages.items():
        progress.setdefault('phase', 'idle')
        requested = not enabled
        active = progress['phase'] in ('downloading', 'uploading', 'publishing', 'scanning', 'verifying')
        actual = 'pausing' if requested and active and live else 'paused' if requested or not live else 'draining' if draining else 'running'
        controls[name] = {'requested_stop': requested, 'state': actual}
        if not live or requested and not active:
            progress.update(phase='paused', current_bytes=0, speed_bytes_per_second=0, eta_seconds=None)
        elif age(progress.get('progress_updated_at')) > 10 or progress['phase'] not in ('downloading', 'uploading'):
            progress.update(speed_bytes_per_second=0, eta_seconds=None)
        progress['control_state'] = actual
    cache = Path(directory) / 'cache'
    cache_health = inspect_cache(cache, meta.get('cache_identity'))
    selected = selected_file_ids(rows, media_scope)
    cleanup_selected = selected_file_ids(rows, media_scope, include_excluded=True)
    files, total, committed, in_flight, imported, importing = [], 0, 0, 0, 0, 0
    for row in rows:
        asset = json.loads(row['asset'])
        size = asset['size']
        stage_name, progress = None, {}
        for name, stage in stages.items():
            if (live and enabled) and (row['id'] == stage.get('current_file_id') or
                (not stage.get('current_file_id') and asset['path'] == stage.get('current_file'))):
                stage_name, progress = name, stage
        uploading = live and enabled and stage_name == 'upload' and progress.get('phase') in ('uploading', 'publishing') and row['state'] != 'uploaded'
        downloading = stage_name == 'import' and progress.get('phase') == 'downloading'
        transferred = min(size, max(0, progress.get('current_bytes', 0))) if uploading else 0
        received = min(size, max(0, progress.get('current_bytes', 0))) if downloading else 0
        evidence = {'state': 'unavailable', 'valid': False, 'bytes': 0, 'reason': cache_health.get('reason')}
        if cache_health['available']:
            evidence = inspect_cached_file(cache / (row['id'] + '.bin'), json.loads(row['cache_identity'] or 'null'),
                                           row['asset_id'], meta.get('cache_identity'))
        cached = evidence['valid']
        stored = row['state'] == 'uploaded' or cached
        member = row['id'] in reporting_ids and row['state'] != 'excluded'
        if member:
            total += size
            committed += size if row['state'] == 'uploaded' else 0
            in_flight += transferred
            imported += size if stored else 0
            importing += received if not stored else 0
        files.append({'id': row['id'], 'path': asset['path'], 'size': size, 'state': row['state'], 'updated_at': row['updated_at'],
                      'role': row.get('role', 'original'), 'group_id': row['group_id'],
                      'exclusion_reason': '按本轮设置不归档预览文件' if row['state'] == 'excluded' else None,
                      'cleanup_selected': row['id'] in cleanup_selected,
                      'imported_at': receipts.get(row['id']),
                      'selected': row['id'] in selected,
                      'cleanup': row['cleanup'], 'error': row['error'], 'remote_path': row['remote_path'],
                      'error_stage': row.get('error_stage'), 'attempts': row.get('attempts', 0),
                      'cache_cleanup_error': row.get('cache_cleanup_error'), 'cache_path': str(cache / (row['id'] + '.bin')),
                      'in_batch': member, 'current': bool(stage_name), 'cache_available': cached,
                      'cache_state': evidence['state'], 'cache_reason': evidence['reason'], 'cache_bytes': evidence['bytes'],
                      'imported': stored, 'stage': stage_name,
                      'phase': progress.get('phase') if stage_name and row['state'] != 'uploaded' else row['state'],
                      'source_cleanup_error': row.get('source_cleanup_error'), 'transferred_bytes': transferred})
    batch_files = [f for f in files if f['in_batch']]
    round_files = [f for f in files if f['id'] in reporting_ids]
    ignored = [f for f in round_files if f['state'] == 'excluded']
    values = item['config'] or {}
    limits = values.get('limits', {})
    reserve = int(limits.get('min_free_disk_gib', 10) * 1024 ** 3)
    quota = int(limits.get('max_cache_gib', 50) * 1024 ** 3)
    used = complete_cache = partial_cache = None
    free = cache_health.get('free_bytes')
    if cache_health['available']:
        try:
            used = cache_bytes(cache)
            partial_cache = sum(p.stat().st_size for p in cache.glob('*.download') if p.is_file())
            complete_cache = max(0, used - partial_cache)
        except OSError as exc:
            cache_health.update(available=False, reason=str(exc), action='检查缓存盘连接和挂载，不会改用其他磁盘')
            used = complete_cache = partial_cache = free = None
    next_file = next((f for f in files if f['selected'] and not f['imported'] and f['cache_state'] != 'unverified'), None)
    next_size = next_file['size'] if next_file else 0
    partial = 0
    if next_file and cache_health['available']:
        try:
            partial = (cache / (next_file['id'] + '.download')).stat().st_size
        except OSError:
            pass
    disk = {'path': str(cache), 'available': cache_health['available'], 'free_bytes': free, 'reserve_bytes': reserve,
            'reason': cache_health.get('reason'), 'action': cache_health.get('action'),
            'next_file_bytes': next_size, 'next_file': next_file['path'] if next_file else None,
            'extra_bytes_needed': max(0, reserve + next_size - partial - free) if next_file and free is not None else None,
            'cache_used_bytes': used, 'complete_cache_bytes': complete_cache, 'partial_cache_bytes': partial_cache,
            'verified_cache_bytes': sum(f['cache_bytes'] for f in files if f['cache_available']) if used is not None else None,
            'unverified_cache_files': sum(f['cache_state'] == 'unverified' for f in files),
            'unverified_cache_bytes': sum(f['cache_bytes'] for f in files if f['cache_state'] == 'unverified') if used is not None else None,
            'quota_bytes': quota, 'quota_extra_bytes': max(0, used - partial + next_size - quota)
            if quota and next_file and used is not None else None}
    queues = {}
    for name, stage in stages.items():
        waiting = [f for f in files if f['selected'] and not f['imported']] if name == 'import' else [
            f for f in files if f['state'] != 'uploaded' and (f['selected'] or f['imported'] or f['state'] in ('cached', 'staged', 'publishing'))]
        ready_files = [f for f in waiting if f['cache_state'] != 'unverified'] if name == 'import' else [f for f in waiting if f['cache_available'] or f['state'] in ('staged', 'publishing')]
        remaining = sum(f['size'] for f in waiting) - (sum(f['transferred_bytes'] for f in waiting) if name == 'upload' else stage.get('current_bytes', 0) if stage.get('phase') == 'downloading' else 0)
        speed = stage.get('speed_bytes_per_second', 0)
        queues[name] = {'pending_files': len(waiting), 'pending_bytes': sum(f['size'] for f in waiting),
                        'ready_files': len(ready_files), 'ready_bytes': sum(f['size'] for f in ready_files),
                        'remaining_bytes': max(0, remaining), 'eta_seconds': max(0, remaining) / speed if speed else None,
                        'failed_files': sum(bool(f['error']) and f['error_stage'] == name for f in waiting)}
    health = {'cache': cache_health}
    for name, interval in (('source', max(30, values.get('backup', {}).get('poll_seconds', 10) * 3)),
                           ('destination', max(90, values.get('backup', {}).get('retry_seconds', 30) * 3))):
        observation = dict(state.get(name, {}))
        observation['stale'] = age(observation.get('checked_at')) > interval or not live
        if observation['stale']:
            observation['online'] = None
        if name == 'source':
            transport = config.source.get('transport', 'usb') if config else 'unknown'
            if observation.get('transport', 'usb') != transport:
                observation.update(online=None, stale=True)
            observation['transport'] = transport
            if observation['transport'] == 'wifi':
                from .wireless_control import connection_status
                observation['wireless'] = connection_status(directory, serial)
            telemetry = dict(observation.get('telemetry', {}))
            telemetry['stale'] = age(telemetry.get('observed_at')) > interval or observation['stale']
            telemetry['available'] = bool(telemetry.get('available') and not telemetry['stale'] and observation.get('online'))
            observation['telemetry'] = telemetry
        state[name] = observation
        health[name] = observation
    if not live or age(state.get('progress_updated_at')) > 10 or state.get('phase') not in ('downloading', 'uploading'):
        state.update(speed_bytes_per_second=0, eta_seconds=None)
    if not live:
        state.update(phase='stopped' if not service.get('running') else 'unresponsive',
                     message='导入服务已停止' if not service.get('running') else '导入服务暂未响应，下面显示最后记录')
    configuration = dict(state.get('configuration', {}))
    configuration.update(saved_revision=item['revision'], error=item['error'], sync_enabled=enabled, stop_after_current=draining)
    configuration['pending'] = bool(configuration.get('pending') or item['revision'] != configuration.get('applied_revision'))
    inventory = state.get('inventory', {})
    scope_current = inventory.get('media_scope', 'all') == media_scope
    all_imported = all(f['imported'] for f in files if f['selected'])
    reading = (state.get('source_work') or state.get('maintenance_running') or state.get('phase') in ('scanning', 'checking_cleanup', 'cleaning_source')
               or stages['import'].get('phase') in ('downloading', 'scanning', 'verifying'))
    # Wireless monitoring deliberately releases the camera after the inventory
    # was verified. Expired telemetry must remain unknown, but does not undo
    # durable import completion while the independent upload queue continues.
    source_released = bool(config and config.source.get('transport') == 'wifi')
    ready = bool(live and (source_released or state.get('source', {}).get('online'))
                 and inventory.get('verified') and inventory.get('scanned') and scope_current
                 and cache_health['available'] and all_imported and not reading
                 and inventory.get('batch_id') == batch_id and inventory.get('destination') == destination
                 and inventory.get('serial') == serial)
    can_disconnect = bool(live and not reading and (ready or not enabled))
    if ready:
        subject = '本轮素材' if media_scope == 'all' else '本轮所选类别素材'
        unplug_message = subject + '已收齐，可以拔走相机；电脑与缓存盘请保持可用，剩余素材' + ('将继续上传' if enabled else '待开启同步后上传')
        if all(f['state'] == 'uploaded' for f in files if f['selected']):
            unplug_message = subject + '已全部归档，可以拔走相机。'
        if media_scope != 'all':
            unplug_message += ' 未选类别不在本轮备份范围，原片保留。'
        if ignored:
            unplug_message += f' 已按设置忽略 {len(ignored)} 个预览文件。'
    elif not live:
        unplug_message = '服务未响应，暂不能确认是否仍在读写相机；请先检查 Docker。'
    elif reading:
        unplug_message = '正在访问相机，请保持连接。'
    elif not enabled:
        pending_import = sum(not f['imported'] for f in files if f['selected'])
        unplug_message = '同步已关闭，程序未在读写相机，可以断开；' + (
            f'记录中还有 {pending_import} 个文件未确认完整导入，下次需再连接相机。' if pending_import else
            '当前相机清单尚未重新核对，不能据此认定所有素材已收齐。')
    elif state.get('source', {}).get('stale'):
        unplug_message = '相机状态尚未更新，暂不能确认是否可以拔线'
    elif not state.get('source', {}).get('online'):
        unplug_message = '相机未连接；同步开启时，已有完整缓存可继续上传'
    else:
        unplug_message = '本轮导入尚未完成，请保持相机连接'
    storage = {key: {'host': os.environ.get('MEDIA_IMPORTER_' + key.upper() + '_HOST_DIR'), 'path': str(value)}
               for key, value in (('config', settings.path.parent), ('data', Path(directory)), ('cache', cache))}
    # Use the destination belonging to these records, not a newly saved but unapplied address.
    try:
        target = json.loads(destination) if destination else {}
    except (ValueError, TypeError):
        target = {}
    locations = sorted({archive_location(target, str(Path(f['remote_path']).parent)) for f in files
                        if f['state'] == 'uploaded' and f['remote_path']})
    planned = sorted({archive_location(target, Asset.from_dict(json.loads(r['asset'])).device_directory)
                      for r in rows})
    recent = next((r for r in rounds if r['finished_at']), next((r for r in rounds if r['historical']), current_round))
    if not rounds:
        recent = next((b for b in batches if b['state'] == 'uploaded'), batches[0] if batches else None)
    recent_result = None
    if recent:
        all_members = [f for f in files if f['id'] in recent['file_ids']]
        members = [f for f in all_members if f['state'] != 'excluded']
        complete = bool(members and all(f['state'] == 'uploaded' for f in members))
        recent_result = {'batch_id': recent['id'], 'complete': complete, 'total_files': len(members),
                         'excluded_preview_files': sum(f['state'] == 'excluded' for f in all_members),
                         'historical': bool(recent.get('historical')), 'total_bytes': sum(f['size'] for f in members),
                         'archived_files': sum(f['state'] == 'uploaded' for f in members),
                         'imported_files': sum(f['imported'] for f in members),
                         'failed_files': sum(bool(f['error']) for f in members),
                         'pending_files': sum(f['state'] != 'uploaded' for f in members),
                         'source_deleted': sum(f['cleanup'] in ('deleted', 'absent') for f in members),
                         'cache_cleanup_pending': sum(f['state'] == 'uploaded' and bool(f['cache_state'] in ('verified', 'unverified') or f['cache_cleanup_error']) for f in members),
                         'finished_at': (recent.get('finished_at') or recent.get('updated_at')) if complete else None,
                         'cleanup': cleanup_summary(all_members, bool(values.get('backup', {}).get('delete_source_after_import'))),
                         'archives': [{'source': f['path'], 'location': archive_location(target, f['remote_path'])}
                                      for f in members if f['state'] == 'uploaded' and f['remote_path']]}
    waiting = [f for f in files if f['state'] != 'uploaded' and (f['cache_available'] or f['state'] in ('cached', 'staged', 'publishing'))]
    local_only = [f for f in waiting if f['cache_available'] and f['cleanup'] in ('deleted', 'absent')]
    stamps = [f['imported_at'] for f in waiting if f['imported_at'] and age(f['imported_at']) != float('inf')]
    oldest = min(stamps) if stamps else None
    backlog = {'files': len(waiting), 'bytes': sum(f['size'] for f in waiting),
               'verified_files': sum(f['cache_available'] for f in waiting),
               'source_removed_files': len(local_only), 'source_removed_bytes': sum(f['size'] for f in local_only),
               'oldest_imported_at': oldest, 'oldest_age_seconds': age(oldest) if oldest else None,
               'unknown_age_files': sum(not f['imported_at'] for f in waiting)}
    active_files = [f for f in files if f['selected'] or f['imported'] or f['state'] in ('cached', 'staged', 'publishing')
                    or f['state'] == 'excluded' and f['cleanup_selected'] and f['source_cleanup_error']]
    all_cleanup = cleanup_summary(files, bool(values.get('backup', {}).get('delete_source_after_import')))
    return {'running': live, 'observed_at': now(), 'status': state, 'stages': stages, 'preflight': meta.get('preflight'), 'backlog': backlog,
            'selection': {'scope': media_scope, 'label': MEDIA_SCOPES[media_scope],
                          'archive_previews': bool(values.get('backup', {}).get('archive_previews')),
                          'round_archive_previews': bool(current_round.get('archive_previews', True)) if current_round else None,
                          'excluded_preview_files': len(ignored),
                          'applied': scope_current and not configuration['pending'],
                          'cached_outside_scope_files': sum(not f['selected'] for f in waiting),
                          'excluded_source_files': inventory.get('excluded_files') if scope_current and inventory.get('scanned') else None,
                          'deferred_files': sum(not f['selected'] and not f['imported'] and f['state'] != 'excluded' for f in files)},
            'configuration': configuration, 'controls': controls, 'health': health, 'queues': queues, 'storage': storage,
            'unplug': {'ready': ready, 'can_disconnect': can_disconnect, 'message': unplug_message},
            'attention': attention(live, enabled, configuration, health, active_files, stages),
            'archive': {'locations': locations, 'planned_locations': planned or [archive_location(target)]}, 'recent_result': recent_result,
            'policy': {'delete_source': bool(values.get('backup', {}).get('delete_source_after_import', False)),
                       'cleanup_cache': True},
            'cleanup': cleanup_summary(round_files, bool(values.get('backup', {}).get('delete_source_after_import'))),
            'batch': {'id': reporting_id, 'scan_batch_id': batch_id, 'historical': bool(current_round and current_round['historical']),
                      'file_ids': [f['id'] for f in batch_files],
                      'excluded_preview_files': len(ignored), 'excluded_preview_bytes': sum(f['size'] for f in ignored),
                      'scanned': bool(inventory.get('scanned') and scope_current), 'verified': bool(inventory.get('verified') and scope_current), 'total_files': len(batch_files),
                      'completed_files': sum(f['state'] == 'uploaded' for f in batch_files),
                      'total_bytes': total, 'confirmed_bytes': committed, 'in_flight_bytes': in_flight,
                      'percent': 100 * (committed + in_flight) / total if total else 0,
                      'imported_files': sum(f['imported'] for f in batch_files), 'imported_bytes': imported,
                      'unverified_files': sum(f['cache_state'] == 'unverified' for f in batch_files),
                      'import_in_flight_bytes': importing, 'import_percent': 100 * (imported + importing) / total if total else 0,
                      'cached_files': sum(f['cache_available'] and f['state'] != 'uploaded' for f in batch_files)},
            'totals': {'total_files': sum(f['state'] != 'excluded' for f in files),
                       'excluded_preview_files': sum(f['state'] == 'excluded' for f in files),
                       'uploaded_files': sum(f['state'] == 'uploaded' for f in files),
                       'uploaded_bytes': sum(f['size'] for f in files if f['state'] == 'uploaded'),
                       'pending_files': sum(f['state'] not in ('uploaded', 'excluded') for f in active_files),
                       'cache_cleanup_pending': sum(f['state'] == 'uploaded' and bool(f['cache_state'] in ('verified', 'unverified') or f['cache_cleanup_error']) for f in files),
                       'pending_cleanup': all_cleanup['source_pending'],
                       'pending_preview_cleanup': all_cleanup['preview_pending']},
            'disk': disk, 'files': files, 'last_result': meta.get('last_result'), 'last_error': meta.get('last_error'),
            'temporary_cleanup': temporaries, 'issues': [f for f in active_files if f['error'] or f['cache_cleanup_error'] or f['source_cleanup_error'] or f['cache_state'] == 'unverified'
                or f['state'] in ('cached', 'staged', 'publishing') and f['cache_state'] == 'missing']}
