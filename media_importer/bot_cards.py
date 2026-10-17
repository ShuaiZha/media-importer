"""Compact Feishu presentation over the shared, read-only status snapshot."""
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit
import math

from .notifications import _size, _duration
from .media import MEDIA_SCOPES


def plain(text):
    return {'tag': 'plain_text', 'content': str(text)}


def paragraph(text):
    # External file names, paths and errors must never become executable markup.
    return {'tag': 'div', 'text': plain(text)}


def markdown(text):
    return {'tag': 'div', 'text': {'tag': 'lark_md', 'content': text}}


def note(text):
    return {'tag': 'note', 'elements': [plain(text)]}


def columns(*texts):
    return {'tag': 'div', 'fields': [{'is_short': True, 'text': {'tag': 'lark_md', 'content': text}} for text in texts]}


def card(title, elements, color='blue', *, shared=False):
    return {'config': {'wide_screen_mode': True, **({'update_multi': True} if shared else {})},
            'header': {'title': plain(title), 'template': color}, 'elements': elements}


def feedback(notice):
    if isinstance(notice, str):
        notice = {'title': '操作提示', 'detail': notice, 'level': 'info'}
    return [paragraph(notice['title'] + ('\n' + notice['detail'] if notice.get('detail') else ''))]


def settings_result(changes, applied, stopping):
    backup = changes.get('backup', {})
    if 'enabled' in backup:
        title = '同步已开启' if backup['enabled'] else '正在停止同步' if stopping else '同步已关闭'
        detail = '正在扫描素材。' if backup['enabled'] else '正在结束操作，完整缓存保留。' if stopping else '任务和完整缓存已保留。'
    else:
        title = '已开启导入后删除原片' if backup.get('delete_source_after_import') else '已设置保留相机原片'
        detail = ''
    if not applied:
        return {'title': '设置已保存 · 等待生效', 'detail': '后台尚未应用，将更新此消息。', 'level': 'warning'}
    return {'title': title, 'detail': detail, 'level': 'warning' if stopping else 'success'}


def _connection(state, local=False):
    if not local and (state.get('stale') or state.get('online') is None):
        return '待更新'
    available = state.get('available') if local else state.get('online')
    return ('可用' if local else '在线') if available else '不可用'


def _stage_label(view, name):
    control = view.get('controls', {}).get(name, {})
    state = control.get('state', 'paused' if control.get('requested_stop') else 'running')
    if state in ('paused', 'pausing') or not view.get('running'):
        return '正在停止' if state == 'pausing' else '已停止'
    phase = view.get('stages', {}).get(name, {}).get('phase')
    return {'downloading': '导入中', 'uploading': '上传中', 'publishing': '确认归档',
            'scanning': '扫描中', 'verifying': '核对中', 'draining': '等待另一条传输停止'}.get(phase, '等待中')


def progress_bar(value):
    value = float(value or 0)
    value = min(100., max(0., value)) if math.isfinite(value) else 0.
    filled = min(10 if value == 100 else 9, int(value / 10))
    return '█' * filled + '░' * (10 - filled), value


def round_complete(view):
    batch = view.get('batch', {})
    return bool(batch.get('total_files') and batch.get('completed_files') == batch['total_files'])


def headline_elements(view):
    unplug = view.get('unplug', {})
    if unplug.get('ready') and unplug.get('can_disconnect', True):
        text = '**相机可以拔走**'
        batch = view.get('batch', {})
        remaining = max(batch.get('total_files', 0) - batch.get('completed_files', 0),
                        view.get('work', {}).get('pending_files', 0), view.get('backlog', {}).get('files', 0))
        if remaining:
            text += ' · 电脑和缓存盘需保持可用'
    elif unplug.get('can_disconnect'):
        text = '**相机可以断开** · 素材尚未确认收齐'
    elif not view.get('running'):
        text = '**暂不能确认相机是否可拔走**'
    elif view.get('health', {}).get('source', {}).get('online') is False:
        text = '**相机未连接** · 等待重连'
    elif view.get('status', {}).get('source_work') or view.get('stages', {}).get('import', {}).get('phase') in ('downloading', 'scanning', 'verifying'):
        text = '**正在访问相机，请保持连接**'
    else:
        text = '**相机状态待确认，请保持连接**'
    result = [markdown(text)]
    attention = view.get('attention', {})
    if attention.get('required'):
        result.append(paragraph(attention.get('message', '').removeprefix('需要处理：')))
    elif attention.get('message', '').startswith('暂时等待'):
        result.append(paragraph(attention['message']))
    if view.get('selection', {}).get('excluded_source_files'):
        result.append(note(f"所选类别之外的 {view['selection']['excluded_source_files']} 个文件未导入，原片保留。"))
    return result


def progress_elements(view):
    batch = view.get('batch', {})
    total = batch.get('total_files', 0)
    if not total:
        return [paragraph('没有待导入素材' if batch.get('scanned') else '尚未扫描素材')]
    label = '本轮任务'
    selection = view.get('selection', {})
    if selection.get('scope', 'all') != 'all':
        label += ' · ' + selection.get('label', MEDIA_SCOPES[selection['scope']])
    result = [note(f"{label} · {total} 个文件 · {_size(batch.get('total_bytes'))}")]
    if selection.get('cached_outside_scope_files'):
        result.append(note(f"含此前其他类别的 {selection['cached_outside_scope_files']} 个完整缓存，继续上传。"))
    if not batch.get('scanned'):
        result.append(note('相机尚未重新扫描；以下为已记录任务。'))
    for name, label, count, percent in (('import', '💾 导入', 'imported_files', 'import_percent'),
                                         ('upload', '📁 归档', 'completed_files', 'percent')):
        done = batch.get(count, 0)
        stage = view.get('stages', {}).get(name, {})
        control = view.get('controls', {}).get(name, {})
        stopped = control.get('requested_stop') or control.get('state') in ('paused', 'pausing') or not view.get('running')
        if done == total:
            result.append(markdown(f"{label} **{done} / {total}** · 完成"))
            continue
        bar, value = progress_bar(batch.get(percent))
        phase = _stage_label(view, name)
        if value == 100 and not stopped:
            phase = '等待导入核验' if name == 'import' else '确认归档'
        result.append(markdown(f"**{label} · {value:.1f}%**　{phase}\n{bar}　已完成 **{done} / {total}**"))
        if stopped:
            continue
        if stage.get('current_file') and stage.get('phase') in ('downloading', 'uploading', 'publishing'):
            detail = str(stage['current_file']).rsplit('/', 1)[-1]
            if stage.get('speed_bytes_per_second'):
                detail += f" · {_size(stage['speed_bytes_per_second'])}/s"
                if stage.get('eta_seconds') is not None:
                    detail += ' · 本文件剩余 ' + _duration(stage['eta_seconds'])
            result.append(paragraph(detail))
        elif stage.get('phase', '').startswith('waiting') and not view.get('attention', {}).get('message', '').startswith('暂时等待'):
            issue = stage.get('issue') or {}
            result.append(paragraph(str(issue.get('action') or stage.get('message') or '等待重试')[:150]))
    return result


def backlog_elements(view):
    backlog = view.get('backlog', {})
    if not backlog.get('source_removed_files'):
        return []
    return [note(f"{backlog['source_removed_files']} 个待归档文件的原片已删除或不存在，请保持缓存盘连接。")]


def cleanup_text(outcome):
    source = []
    for key, label in (('source_deleted', '已删除'), ('source_absent', '复查不存在'), ('source_pending', '清理待处理')):
        if outcome.get(key):
            source.append(f"{label} {outcome[key]} 个")
    untouched = outcome.get('source_not_deleted', 0) - outcome.get('source_pending', 0)
    if untouched:
        source.append(f'未执行删除 {untouched} 个')
    cache = []
    for key, label in (('cache_cleared', '已清理'), ('cache_pending', '清理待处理'), ('cache_unknown', '状态待确认')):
        if outcome.get(key):
            cache.append(f"{label} {outcome[key]} 个")
    lines = []
    if source:
        lines.append('原片：' + ' · '.join(source))
    if cache:
        lines.append('缓存：' + ' · '.join(cache))
    previews = []
    for key, label in (('preview_deleted', '已随原片清理'), ('preview_absent', '复查不存在'), ('preview_pending', '清理待处理')):
        if outcome.get(key):
            previews.append(f"{label} {outcome[key]} 个")
    if previews:
        lines.append('预览文件：' + ' · '.join(previews))
    return '\n'.join(lines)


def _title(view):
    if not view.get('running'):
        return '⚠️ 服务待检查', 'orange'
    if any(c.get('state') == 'pausing' for c in view.get('controls', {}).values()):
        return '⏳ 正在停止同步', 'orange'
    if not view.get('configuration', {}).get('sync_enabled', True):
        return '⏸ 同步已关闭', 'grey'
    if view.get('attention', {}).get('required'):
        return '⚠️ 需要处理', 'orange'
    if view.get('configuration', {}).get('stop_after_current'):
        return '⏳ 传完当前文件后停止', 'blue'
    if round_complete(view):
        pending = view.get('cleanup', {})
        if pending.get('source_pending') or pending.get('preview_pending') or pending.get('cache_pending') or pending.get('cache_unknown'):
            return '✅ 已归档 · 清理待处理', 'orange'
        return '✅ 本轮已归档', 'green'
    if any(s.get('phase') in ('uploading', 'publishing') for s in view.get('stages', {}).values()):
        return '🔄 正在归档', 'blue'
    if view.get('stages', {}).get('import', {}).get('phase') == 'downloading':
        return '🔄 正在导入', 'blue'
    if not view.get('batch', {}).get('total_files') and not view.get('work', {}).get('pending_files'):
        return '待机 · 等待新素材', 'blue'
    return '⏳ 等待传输', 'blue'


def _overview(view):
    result = headline_elements(view)
    result += progress_elements(view)
    result += backlog_elements(view)
    if view.get('batch', {}).get('excluded_preview_files'):
        result.append(note(f"按设置忽略 {view['batch']['excluded_preview_files']} 个预览文件，不计入传输数量。"))
    if view.get('cleanup') and round_complete(view):
        text = cleanup_text(view['cleanup'])
        if text:
            result.append(paragraph(text))
    elif view.get('issues'):
        first = view['issues'][0]
        reason = first.get('cache_reason') or first.get('error') or first.get('source_cleanup_error') or first.get('cache_cleanup_error') or '等待重新检查'
        result.append(paragraph(f"{len(view['issues'])} 个文件待处理 · " + str(first.get('path', '')) + '\n' + str(reason)[:180]))
    return result


def progress_card(view, interval, keyword='', updated_at=None, *, notifications_enabled=True):
    title, color = _title(view)
    if not notifications_enabled:
        title, color = '🔕 自动通知已关闭', 'grey'
    elements = ([note(keyword)] if keyword else []) + _overview(view)
    if not view.get('configuration', {}).get('sync_enabled', True) or not notifications_enabled:
        elements.append(note('自动进度更新已停止，任务和完整缓存保留。'))
    elements.append(note('更新于 ' + format_time(updated_at or view.get('observed_at'))))
    return card(title, elements, color, shared=True)


def short_location(location):
    return '/'.join(PurePosixPath(urlsplit(location).path).parts[-2:]) or location


def event_card(kind, title, text, view, *, keyword='', updated_at=None, locations=None):
    icon, color = {'connected': ('📷', 'blue'), 'disconnected': ('🔌', 'grey'),
                   'completed': ('✅', 'green'), 'imported': ('📷', 'green'),
                   'failed': ('⚠️', 'orange'), 'backlog': ('💾', 'orange'),
                   'recovered': ('✅', 'green'), 'test': ('✅', 'blue')}.get(kind, ('ℹ️', 'blue'))
    title = title.removeprefix('素材导入助手 · ')
    elements = [note(keyword)] if keyword else []
    if kind in ('connected', 'disconnected'):
        enabled = view.get('configuration', {}).get('sync_enabled', False)
        detail = ('同步已开启，等待扫描。' if enabled else '同步已关闭，尚未开始导入。') if kind == 'connected' else (
            '完整缓存继续上传，未导完的文件等待重连。' if enabled else '同步已关闭，任务和完整缓存保留。')
        elements.append(paragraph(text + '\n' + detail))
    elif kind == 'completed':
        elements.append(paragraph(text))
        for location in (locations or [])[:2]:
            elements.append(paragraph('归档位置：' + short_location(location)))
        if '待处理' in text or '待确认' in text:
            color = 'orange'
    elif kind == 'imported':
        if view.get('unplug', {}).get('ready') and view.get('unplug', {}).get('can_disconnect', True):
            title = '相机可以拔走'
        else:
            title = '素材已导入 · 相机状态待确认'
        total = view.get('batch', {}).get('total_files', 0)
        pending = view.get('queues', {}).get('upload', {}).get('pending_files', 0)
        elements.append(paragraph(f'{total} 个文件已完整导入，仍有 {pending} 个待归档。\n电脑和缓存盘需保持可用。'))
    elif kind == 'backlog':
        backlog = view.get('backlog', {})
        elements.append(paragraph(f"{backlog.get('files', 0)} 个文件 · {_size(backlog.get('bytes'))}\n" + text))
        elements += backlog_elements(view)
    else:
        elements.append(paragraph(text))
    elements.append(note(format_time(updated_at or view.get('observed_at'))))
    return card(icon + ' ' + title, elements, color, shared=kind in ('failed', 'backlog', 'recovered', 'completed'))


def status_content(view):
    title, color = _title(view)
    elements = headline_elements(view)
    health, disk = view.get('health', {}), view.get('disk', {})
    source, target = health.get('source', {}), health.get('destination', {})
    telemetry = source.get('telemetry', {})
    battery = ''
    if (source.get('online') and not source.get('stale') and telemetry.get('available')
            and not telemetry.get('stale') and telemetry.get('battery_percent') is not None):
        battery = ' · 电量 ' + str(telemetry['battery_percent']) + '%'
    elements.append(paragraph('USB 相机 ' + _connection(source) + battery + '\n缓存盘 ' +
        _connection(health.get('cache', {}), True) + ' · SMB ' + _connection(target)))
    elements += progress_elements(view) + backlog_elements(view)
    if view.get('batch', {}).get('excluded_preview_files'):
        elements.append(note(f"按设置忽略 {view['batch']['excluded_preview_files']} 个预览文件，不计入传输数量。"))
    if disk.get('available'):
        elements.append(note('缓存占用 ' + _size(disk.get('cache_used_bytes')) + ' · 磁盘可用 ' + (_size(disk['free_bytes']) if disk.get('free_bytes') is not None else '未知')))
    if round_complete(view):
        result = cleanup_text(view.get('cleanup', {}))
        if result:
            elements.append(paragraph(result))
    elif view.get('issues'):
        first = view['issues'][0]
        reason = first.get('cache_reason') or first.get('error') or first.get('source_cleanup_error') or first.get('cache_cleanup_error') or '等待重新检查'
        elements.append(paragraph(f"{len(view['issues'])} 个文件待处理 · " + str(first.get('path', '')) + '\n' + str(reason)[:180]))
    if view.get('temporary_cleanup'):
        elements.append(note(f"{len(view['temporary_cleanup'])} 个临时文件等待清理，恢复连接后自动重试。"))
    archive = view.get('archive', {})
    for path in (archive.get('locations') or archive.get('planned_locations', []))[:1]:
        elements.append(paragraph('归档位置：' + path))
    elements.append(note('原片：' + ('完整导入后删除' if view.get('policy', {}).get('delete_source') else '保留') +
                         ' · 查询于 ' + format_time(view.get('observed_at'))))
    return title, color, elements


def format_time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return '时间未知'
        return stamp.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S 北京时间')
    except (ValueError, TypeError, OverflowError):
        return '时间未知'
