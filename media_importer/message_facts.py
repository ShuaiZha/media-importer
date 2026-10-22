"""Presentation facts shared by synchronization, selected imports and notifications."""
import math


def stopped(view, name):
    control = view.get('controls', {}).get(name, {})
    return bool(not view.get('running') or control.get('requested_stop') or
                control.get('state') in ('paused', 'pausing') or
                not view.get('transfer_enabled', view.get('configuration', {}).get('sync_enabled', True)))


def stage_label(view, name):
    control = view.get('controls', {}).get(name, {})
    if control.get('state') == 'pausing':
        return '正在停止'
    if stopped(view, name):
        return '已停止' if view.get('running') else '状态待确认'
    phase = view.get('stages', {}).get(name, {}).get('phase')
    return {'downloading': '导入中', 'uploading': '上传中', 'publishing': '归档确认中',
            'scanning': '扫描中', 'verifying': '核验中', 'draining': '等待当前操作结束',
            'waiting_source': '等待相机', 'waiting_confirmation': '等待相机确认', 'waiting_destination': '等待存储',
            'waiting_cache': '等待缓存盘', 'waiting_space': '等待可用空间',
            'waiting_retry': '等待重试', 'waiting_preview': '预览中，导入等待', 'failed': '等待重试'}.get(phase, '等待处理')


def progress_rows(view):
    # Completion counts are durable commits. Byte progress alone never completes a row.
    batch = view.get('batch', {})
    total = max(0, int(batch.get('total_files') or 0))
    rows = []
    for name, label, count, percent in (('import', '导入', 'imported_files', 'import_percent'),
                                       ('upload', '归档', 'completed_files', 'percent')):
        done = min(total, max(0, int(batch.get(count) or 0)))
        value = float(batch.get(percent) or 0)
        value = min(100, max(0, value)) if math.isfinite(value) else 0
        phase = stage_label(view, name)
        if total and done == total:
            value, phase = 100, '已完成'
        elif value >= 100:
            value = 99.9
            if phase not in ('已停止', '正在停止', '状态待确认'):
                phase = '导入核验中' if name == 'import' else '归档确认中'
        rows.append({'name': name, 'label': label, 'done': done, 'total': total,
                     'percent': math.floor(value * 10) / 10, 'phase': phase})
    return rows


def selected_view(result, state):
    size = result.get('total_bytes') or 0
    return {'running': True, 'transfer_enabled': state not in ('paused', 'done'),
            'stages': result.get('stages', {}),
            'batch': {'total_files': result.get('total_files', 0), 'total_bytes': size,
                      'imported_files': result.get('imported_files', 0),
                      'completed_files': result.get('uploaded_files', 0),
                      'import_percent': 100 * (result.get('import_bytes') or 0) / size if size else 0,
                      'percent': 100 * (result.get('upload_bytes') or 0) / size if size else 0}}
