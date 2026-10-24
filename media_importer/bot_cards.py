"""Consistent Feishu messages: outcome first, progress second, action last."""
from datetime import datetime, timedelta, timezone
import math

from .notifications import _size, _duration
from .media import MEDIA_SCOPES


def plain(text):
    return {'tag': 'plain_text', 'content': str(text)}


def paragraph(text):
    # Filenames, paths and external errors are always plain text.
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
        notice = {'title': '操作说明', 'detail': notice, 'level': 'info'}
    return [paragraph(notice['title'] + ('\n' + notice['detail'] if notice.get('detail') else ''))]


def settings_result(changes, applied, stopping):
    backup = changes.get('backup', {})
    if not applied:
        return {'title': '设置已保存 · 等待生效', 'detail': '', 'level': 'warning'}
    if 'enabled' in backup:
        return {'title': '自动备份已开启' if backup['enabled'] else '正在停止备份' if stopping else '备份已停止',
                'detail': '', 'level': 'warning' if stopping else 'success'}
    if 'stop_after_current' in backup:
        return {'title': '当前文件完成后停止', 'detail': '不再开始新文件。', 'level': 'success'}
    titles = {'livestream': '直播网络已保存', 'preview': '预览入口已保存', 'source_options': '相机连接已生效',
              'limits': '传输与缓存已生效', 'archive': '归档存储已生效', 'notifications': '通知设置已生效'}
    labels = {'upload_mib_per_second': '上传限速', 'max_cache_gib': '缓存额度', 'min_free_disk_gib': '磁盘预留',
              'poll_seconds': '扫描间隔', 'retry_seconds': '重试间隔', 'stability_seconds': '清单复查间隔',
              'progress_interval_seconds': '进度间隔', 'connection_events': '连接提醒', 'keyword': '通知前缀'}
    title, lines = '设置已生效' if changes else '设置未变化', []
    for group, updates in changes.items():
        title = titles.get(group, title)
        for key, value in updates.items():
            if group == 'limits':
                unit = ' MiB/s' if key == 'upload_mib_per_second' else ' GiB'
                lines.append(labels[key] + '：' + (f'{value:g}' + unit if value or key == 'min_free_disk_gib' else '不限'))
            elif key in ('poll_seconds', 'retry_seconds', 'stability_seconds', 'progress_interval_seconds'):
                lines.append(labels[key] + (f'：{value / 60:g} 分钟' if key == 'progress_interval_seconds' else f'：{value:g} 秒'))
            elif group == 'notifications':
                label = '自动通知' if key == 'enabled' else labels.get(key, key)
                lines.append(label + '：' + (('开启' if value else '关闭') if type(value) is bool else value or '无'))
            elif key == 'media_scope':
                lines.append('素材范围：' + MEDIA_SCOPES[value])
            elif key == 'archive_previews':
                lines.append('辅助文件：' + ('归档' if value else '不归档') + '（下轮生效）')
            elif key == 'delete_source_after_import':
                lines.append('原片处理：' + ('完整导入后删除' if value else '保留'))
            elif key == 'source_transport':
                lines.append('连接方式：' + {'auto': '自动', 'usb': 'USB', 'wifi': '无线'}[value])
            elif key == 'public_url':
                lines.append('观看入口：' + (value or '仅本机'))
        if group == 'archive':
            from .bot_settings import location
            lines.append(location(updates))
    return {'title': title, 'detail': '\n'.join(lines), 'level': 'success'}

def _connection(state, local=False):
    if state.get('stale'):
        return '状态已过期'
    available = state.get('available') if local else state.get('online')
    if available is None:
        return '未知'
    return ('可用' if local else '在线') if available else ('不可用' if local else '离线')

def connection_elements(view):
    health = view.get('health', {})
    source = health.get('source', {})
    label = _connection(source)
    if view.get('source_connection', {}).get('state') in ('pending', 'declined') and not source.get('stale'):
        label = '待确认'
    elif view.get('running') and view.get('configuration', {}).get('sync_enabled') and source.get('online') is not True:
        label = {'connecting': '连接中', 'waiting_retry': '等待重连'}.get(view.get('source_link', {}).get('phase'), label)
    if view.get('camera'):
        label = view['camera']['connection']['label']
    return [paragraph('相机' + label + ' · 缓存' + _connection(health.get('cache', {}), True) +
                      ' · 存储' + _connection(health.get('destination', {})))]

from .message_facts import stopped as _stopped, stage_label as _stage_label


def progress_bar(value):
    value = float(value or 0)
    value = min(100., max(0., value)) if math.isfinite(value) else 0.
    filled = min(10 if value == 100 else 9, int(value / 10))
    return '█' * filled + '░' * (10 - filled), value


def round_complete(view):
    batch = view.get('batch', {})
    return bool(batch.get('total_files') and batch.get('completed_files') == batch['total_files'])


def _cleanup_pending(outcome):
    return any(outcome.get(k) for k in ('source_pending', 'preview_pending', 'cache_pending', 'cache_unknown'))


def _pending_archive(view):
    batch = view.get('batch', {})
    return max(0, batch.get('total_files', 0) - batch.get('completed_files', 0),
               view.get('queues', {}).get('upload', {}).get('pending_files', 0),
               view.get('backlog', {}).get('files', 0))


def _title(view):
    configuration = view.get('configuration', {})
    if configuration.get('error'):
        return '配置异常', 'red'
    if not view.get('running'):
        return '服务未响应', 'red'
    if any(c.get('state') == 'pausing' for c in view.get('controls', {}).values()):
        return ('正在停止所选文件传输' if view.get('selected_import') else '正在停止备份'), 'orange'
    if configuration.get('pending'):
        return '设置等待生效', 'orange'
    if view.get('attention', {}).get('required'):
        return '任务需要处理', 'orange'
    selection = view.get('selected_import', {})
    if selection:
        if selection.get('state') == 'paused':
            return '所选文件传输已停止', 'grey'
        if round_complete(view):
            return ('所选文件归档完成 · 缓存待清理', 'orange') if _cleanup_pending(view.get('cleanup', {})) else ('所选文件归档完成', 'green')
        if selection.get('state') == 'done':
            return '选定文件任务已结束', 'grey'
        stages = view.get('stages', {})
        importing = stages.get('import', {}).get('phase') == 'downloading'
        uploading = stages.get('upload', {}).get('phase') in ('uploading', 'publishing')
        if importing and uploading:
            return '所选文件导入与归档进行中', 'blue'
        if uploading:
            return '所选文件归档进行中', 'blue'
        if importing:
            return '所选文件导入进行中', 'blue'
        return ('所选文件等待重试', 'orange') if selection.get('state') == 'waiting' else ('所选文件等待传输', 'blue')
    if view.get('source_connection', {}).get('state') in ('pending', 'declined'):
        uploading = view.get('stages', {}).get('upload', {}).get('phase') in ('uploading', 'publishing')
        return ('远端归档进行中 · 相机待确认' if uploading else '相机待确认'), 'blue'
    if not configuration.get('sync_enabled', True):
        return '备份已停止', 'grey'
    if configuration.get('stop_after_current'):
        return '当前文件完成后停止', 'blue'
    if round_complete(view):
        return ('归档完成 · 清理待处理', 'orange') if _cleanup_pending(view.get('cleanup', {})) else ('归档完成', 'green')
    batch, stages = view.get('batch', {}), view.get('stages', {})
    total = batch.get('total_files', 0)
    importing = stages.get('import', {}).get('phase') == 'downloading' and batch.get('imported_files', 0) < total
    uploading = stages.get('upload', {}).get('phase') in ('uploading', 'publishing') and batch.get('completed_files', 0) < total
    if importing and uploading:
        return '导入与归档进行中', 'blue'
    if uploading:
        return '远端归档进行中', 'blue'
    if importing:
        return '本地导入进行中', 'blue'
    activity = view.get('camera', {}).get('activity', {})
    if activity.get('state') in ('preview', 'livestream', 'recording', 'browsing', 'recovering'):
        return '导入等待相机', 'orange' if activity['state'] == 'recovering' else 'blue'
    if stages.get('import', {}).get('phase') in ('scanning', 'verifying') or view.get('status', {}).get('source_work'):
        return '正在核验素材', 'blue'
    if any(s.get('phase') == 'failed' for s in stages.values()) or view.get('issues'):
        return '部分文件等待重试', 'orange'
    link = view.get('source_link', {})
    if link.get('phase') in ('connecting', 'waiting_retry', 'unsupported', 'waiting_source'):
        return {'connecting': ('正在连接相机', 'blue'),
                'waiting_retry': ('等待相机自动重连', 'blue'),
                'unsupported': ('连接方式需要调整', 'orange'),
                'waiting_source': ('等待相机连接', 'blue')}[link['phase']]
    if total or view.get('work', {}).get('pending_files'):
        if not view.get('health', {}).get('cache', {}).get('available', True):
            return '等待缓存盘恢复', 'orange'
        if batch.get('imported_files', 0) == total and total:
            return '等待远端归档', 'blue'
        if view.get('health', {}).get('source', {}).get('online') is False:
            return '等待相机连接', 'blue'
        return '等待传输', 'blue'
    return '备份待机', 'blue'


def headline_elements(view):
    activity = view.get('camera', {}).get('activity', {})
    if activity.get('state') in ('preview', 'livestream', 'recording', 'browsing', 'recovering'):
        waiting = ' · 导入等待' if view.get('configuration', {}).get('sync_enabled') else ''
        return [paragraph(activity['label'] + waiting)]
    if view.get('running') and view.get('status', {}).get('preview_work'):
        return [paragraph('预览进行中，导入等待')]
    if view.get('running') and view.get('status', {}).get('source_work'):
        return [paragraph('相机操作进行中')]
    unplug = view.get('unplug', {})
    source = view.get('health', {}).get('source', {})
    if (source.get('online') is True and not source.get('stale') and view.get('running') and unplug.get('ready') and unplug.get('can_disconnect', True) and
            view.get('batch', {}).get('total_files') and not view.get('status', {}).get('source_work')):
        return [paragraph('相机读取已结束')]
    return []

def progress_elements(view, *, details=False):
    from .message_facts import progress_rows
    batch = view.get('batch', {})
    total = batch.get('total_files', 0)
    if not total:
        return []
    result = []
    if details:
        result.append(markdown(f"**{total} 个文件** · {_size(batch.get('total_bytes'))}"))
    for row in progress_rows(view):
        bar, value = progress_bar(row['percent'])
        phase = row['phase']
        result.append(markdown(f"**{row['label']}**　{bar}　**{value:g}%** · {row['done']} / {total}"))
        if phase and phase not in ('导入中', '上传中', '已完成', '已停止', '等待处理'):
            result.append(note(phase))
        stage = view.get('stages', {}).get(row['name'], {})
        if details and not _stopped(view, row['name']) and stage.get('current_file'):
            result.append(paragraph(str(stage['current_file'])))
            if stage.get('speed_bytes_per_second'):
                text = _size(stage['speed_bytes_per_second']) + '/s'
                if stage.get('eta_seconds') is not None:
                    text += ' · 预计剩余 ' + _duration(stage['eta_seconds'])
                result.append(note(text))
    return result

def attention_elements(view):
    from .notifications import PROBLEMS
    if view.get('selected_import', {}).get('archive_verification_pending'):
        return [paragraph('已有归档待核验')]
    for stage in view.get('stages', {}).values():
        issue = stage.get('issue') or {}
        if issue.get('code') in PROBLEMS and (stage.get('phase', '').startswith('waiting') or stage.get('phase') == 'failed'):
            title, action = PROBLEMS[issue['code']]
            return [paragraph(title + '\n' + action)]
    if view.get('health', {}).get('cache', {}).get('available') is False:
        return [paragraph('缓存盘不可用，请连接原缓存盘')]
    if any(i.get('cache_state') == 'unverified' for i in view.get('issues', [])):
        return [paragraph('完整缓存待核验，请查看详情')]
    if view.get('attention', {}).get('required'):
        return [paragraph('部分任务需要处理，请查看详情。')]
    if view.get('issues'):
        return [paragraph(f"{len(view['issues'])} 个文件待处理")]
    return []

def backlog_elements(view):
    backlog = view.get('backlog', {})
    if not backlog.get('source_removed_files'):
        return []
    return [note(f"{backlog['source_removed_files']} 个待归档文件已无相机副本，请保留完整缓存。")]


def cleanup_text(outcome, ignored=0):
    source = []
    for key, label in (('source_deleted', '已删除'), ('source_absent', '未找到'), ('source_pending', '待清理')):
        if outcome.get(key):
            source.append(f"{label} {outcome[key]} 个")
    untouched = max(0, outcome.get('source_not_deleted', 0) - outcome.get('source_pending', 0))
    if untouched:
        source.append(f'未执行删除 {untouched} 个')
    cache = []
    for key, label in (('cache_cleared', '已清理'), ('cache_pending', '待清理'), ('cache_unknown', '待核验')):
        if outcome.get(key):
            cache.append(f"{label} {outcome[key]} 个")
    lines = []
    if source:
        lines.append('相机原片：' + '；'.join(source))
    if cache:
        lines.append('本地缓存：' + '；'.join(cache))
    previews = [f'未归档 {ignored} 个'] if ignored else []
    for key, label in (('preview_deleted', '已删除'), ('preview_absent', '未找到'), ('preview_pending', '待清理')):
        if outcome.get(key):
            previews.append(f"{label} {outcome[key]} 个")
    if previews:
        lines.append('辅助文件：' + '；'.join(previews))
    return '\n'.join(lines)


def _result_elements(result):
    outcome = result.get('cleanup', {})
    elements = [markdown(f"**{result['file_count']} 个文件** · {_size(result.get('byte_count'))}")]
    lines = cleanup_text(outcome, result.get('ignored_preview_files', 0)).splitlines()
    core = [line for line in lines if not line.startswith('辅助文件：')]
    if core:
        elements.append(paragraph('\n'.join(core)))
    for line in lines:
        if line.startswith('辅助文件：'):
            elements.append(note(line))
    return elements


def _selection_notes(view):
    selection, batch = view.get('selection', {}), view.get('batch', {})
    if view.get('selected_import'):
        parts = ['仅处理本次所选文件；其他任务保留', '本次保留相机原片']
        if batch.get('excluded_preview_files') and not round_complete(view):
            parts.append(f"辅助文件未归档 {batch['excluded_preview_files']} 个")
        return [note('；'.join(parts))]
    parts = []
    if selection.get('scope', 'all') != 'all':
        parts.append('素材范围：' + MEDIA_SCOPES.get(selection['scope'], '所选类别'))
    if selection.get('excluded_source_files'):
        parts.append(f"范围外 {selection['excluded_source_files']} 个文件保留")
    if selection.get('cached_outside_scope_files'):
        parts.append(f"含其他类别的 {selection['cached_outside_scope_files']} 个既有缓存")
    if batch.get('excluded_preview_files') and not round_complete(view):
        parts.append(f"辅助文件未归档 {batch['excluded_preview_files']} 个")
    return [note('；'.join(parts))] if parts else []


def _overview(view):
    result = progress_elements(view) + connection_elements(view) + attention_elements(view)
    if not view.get('batch', {}).get('total_files'):
        pending = view.get('work', {}).get('pending_files', 0)
        result.append(paragraph(f'已登记任务 {pending} 个' if pending else '没有待处理文件' if view.get('batch', {}).get('scanned') else '尚未扫描'))
    result += headline_elements(view)
    if round_complete(view):
        text = cleanup_summary(view.get('cleanup', {}))
        if text:
            result.append(paragraph(text))
    return result

def progress_card(view, interval, keyword='', updated_at=None, *, notifications_enabled=True):
    title, color = _title(view)
    elements = ([note(keyword)] if keyword else []) + _overview(view)
    elements.append(note('更新 ' + format_time(updated_at or view.get('observed_at'))))
    return card(title, elements, color, shared=True)

def event_card(kind, title, text, view, *, keyword='', updated_at=None, locations=None, result=None):
    color = {'completed': 'green', 'failed': 'orange', 'backlog': 'orange', 'recovered': 'green'}.get(kind, 'blue')
    title = title.removeprefix('素材导入助手 · ')
    elements = [note(keyword)] if keyword else []
    if kind in ('connected', 'disconnected'):
        title = '相机已连接' if kind == 'connected' else '相机已断开'
        mode = {'usb': 'USB', 'wifi': 'Wi-Fi'}.get(view.get('health', {}).get('source', {}).get('transport'), '')
        elements.append(paragraph((mode + ' · ' if mode else '') + ('备份已开启' if view.get('configuration', {}).get('sync_enabled') else '备份已关闭')))
    elif kind == 'completed':
        title = '归档完成'
        if result:
            elements.append(markdown(f"**{result['file_count']} 个文件** · {_size(result.get('byte_count'))}"))
            cleanup = cleanup_summary(result.get('cleanup', {}))
            destination = result.get('destination_name') or view.get('archive', {}).get('name')
            if destination:
                elements.append(paragraph('目的地：' + destination))
            if cleanup:
                elements.append(paragraph(cleanup))
            if _cleanup_pending(result.get('cleanup', {})):
                color = 'orange'
        else:
            elements.append(paragraph(text))
    elif kind == 'imported':
        title = '导入完成'
        batch = view.get('batch', {})
        elements.append(markdown(f"**{batch.get('imported_files', 0)} 个文件** · 待归档 {_pending_archive(view)} 个"))
        elements += headline_elements(view)
    elif kind == 'backlog':
        title = '归档长时间无进展'
        backlog = view.get('backlog', {})
        elements.append(paragraph(f"待归档 {backlog.get('files', 0)} 个 · {_size(backlog.get('bytes'))}\n请检查存储连接和缓存盘"))
        elements += backlog_elements(view)
    elif kind == 'test':
        title = '通知测试成功'
    elif kind == 'recovered':
        elements.append(paragraph('按当前备份设置继续处理'))
    else:
        elements.append(paragraph(text))
    return card(title, elements, color, shared=True)

def status_content(view):
    from .message_facts import current_work
    # Query cards lead with the current control state; progress receipts retain
    # their own terminal result. Both still use the same durable progress facts.
    active = current_work(view)
    enabled = view.get('configuration', {}).get('sync_enabled', False)
    title, color = _title(view)
    healthy = view.get('running') and not any(view.get('configuration', {}).get(k) for k in ('error', 'pending'))
    stopping = any(c.get('state') == 'pausing' for c in view.get('controls', {}).values())
    pending_identity = view.get('source_connection', {}).get('state') in ('pending', 'declined')
    if healthy and not active and not stopping and not pending_identity:
        if not enabled: title, color = '备份已停止', 'grey'
        elif round_complete(view): title, color = '自动备份待机', 'blue'
    elements = [paragraph('自动备份：' + ('开启' if enabled else '关闭'))] if active or enabled or pending_identity else []
    if round_complete(view) and not active:
        elements += connection_elements(view) + attention_elements(view)
        batch = view['batch']
        elements.append(paragraph(f"最近结果：已归档 {batch['completed_files']} 个文件 · {_size(batch.get('total_bytes'))}"))
        summary = cleanup_summary(view.get('cleanup', {}))
        if summary: elements.append(note(summary))
        pending = view.get('work', {}).get('pending_files', 0)
        if pending: elements.append(paragraph(f'待处理 {pending} 个文件'))
    else:
        elements += _overview(view)
    return title, color, elements + [note('更新 ' + format_time(view.get('observed_at')))]

def format_time(value, *, now=None):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return '时间未知'
        zone = timezone(timedelta(hours=8))
        stamp = stamp.astimezone(zone)
        current = (now or datetime.now(zone)).astimezone(zone)
        pattern = '%H:%M' if stamp.date() == current.date() else '%m-%d %H:%M' if stamp.year == current.year else '%Y-%m-%d %H:%M'
        return stamp.strftime(pattern)
    except (ValueError, TypeError, OverflowError):
        return '时间未知'


def cleanup_summary(outcome):
    parts = []
    if outcome.get('cache_pending'):
        parts.append(f"缓存待清理 {outcome['cache_pending']} 个")
    elif outcome.get('cache_unknown'):
        parts.append('缓存状态待核验')
    elif outcome.get('cache_cleared'):
        parts.append('缓存已清理')
    deleted = outcome.get('source_deleted', 0)
    absent = outcome.get('source_absent', 0)
    pending = outcome.get('source_pending', 0)
    if deleted: parts.append(f'原片已删除 {deleted} 个')
    if absent: parts.append(f'原片未找到 {absent} 个')
    if pending: parts.append(f'原片待清理 {pending} 个')
    retained = max(0, outcome.get('source_not_deleted', 0) - outcome.get('source_pending', 0))
    if retained: parts.append(f'原片保留 {retained} 个')
    for key, label in (('preview_deleted', '已删除'), ('preview_absent', '未找到'), ('preview_pending', '待清理')):
        if outcome.get(key): parts.append(f"辅助文件{label} {outcome[key]} 个")
    return ' · '.join(parts)


def detail_content(view):
    elements = progress_elements(view, details=True) + connection_elements(view)
    if view.get('batch', {}).get('total_files') and not view['batch'].get('scanned'):
        elements.append(note('相机清单尚未复核'))
    for error in view.get('selected_import', {}).get('archive_errors', [])[:8]:
        elements.append(paragraph(str(error.get('file', '')) + '\n' + str(error.get('reason', ''))))
    elements += attention_elements(view) + _selection_notes(view) + backlog_elements(view)
    disk = view.get('disk', {})
    if disk.get('available'):
        elements.append(paragraph('缓存占用 ' + _size(disk.get('cache_used_bytes')) + ' · 磁盘可用 ' +
                                  (_size(disk['free_bytes']) if disk.get('free_bytes') is not None else '未知')))
    for path in view.get('archive', {}).get('locations') or view.get('archive', {}).get('planned_locations', []):
        elements.append(paragraph('归档位置：' + path))
    for issue in view.get('issues', [])[:8]:
        reason = issue.get('cache_reason') or issue.get('error') or issue.get('source_cleanup_error') or issue.get('cache_cleanup_error') or '等待重试'
        elements.append(paragraph(str(issue.get('path', '')) + '\n' + str(reason)))
    attention = view.get('attention', {})
    if attention.get('message'): elements.append(paragraph(attention['message']))
    for stage in view.get('stages', {}).values():
        issue = stage.get('issue') or {}
        if issue.get('message'): elements.append(paragraph(issue['message']))
    cleanup = cleanup_text(view.get('cleanup', {}), view.get('batch', {}).get('excluded_preview_files', 0))
    if cleanup: elements.append(paragraph(cleanup))
    if view.get('temporary_cleanup'):
        elements.append(paragraph(f"未完成临时文件待清理 {len(view['temporary_cleanup'])} 个"))
    # Avoid repeating the same diagnostic from stage, attention and file facts.
    import json
    unique = {json.dumps(el, ensure_ascii=False, sort_keys=True): el for el in elements}
    elements = list(unique.values())
    elements.append(note('更新 ' + format_time(view.get('observed_at'))))
    return '备份详情', 'blue', elements
