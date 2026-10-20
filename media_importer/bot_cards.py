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
        return {'title': '设置已保存 · 等待生效', 'detail': '传输服务应用后，此消息将自动更新。', 'level': 'warning'}
    if 'enabled' in backup:
        if backup['enabled']:
            return {'title': '同步已开启', 'detail': '将自动检测并处理符合设置的素材。', 'level': 'success'}
        if stopping:
            return {'title': '正在停止同步', 'detail': '正在结束当前操作，任务与完整缓存保留。', 'level': 'warning'}
        return {'title': '同步已停止', 'detail': '导入、上传及原片删除均已停止，任务与完整缓存保留。', 'level': 'success'}
    if 'stop_after_current' in backup:
        return {'title': '将在当前文件完成后停止', 'detail': '两路不再启动新文件；当前操作结束后关闭同步。', 'level': 'success'}
    if 'source_options' in changes:
        return {'title': '相机连接设置已生效', 'detail': '后台按新提示重新发现相机；识别到其他设备仍需确认。同步与原片处理开关保持不变。', 'level': 'success'}
    intervals = (f"扫描间隔：{backup['poll_seconds']:g} 秒\n重试基础间隔：{backup['retry_seconds']:g} 秒\n"
                 f"清单复查间隔：{backup['stability_seconds']:g} 秒") if 'poll_seconds' in backup else ''
    if intervals and 'limits' not in changes:
        return {'title': '运行间隔已生效', 'detail': intervals, 'level': 'success'}
    if 'archive_previews' in backup:
        enabled = backup['archive_previews']
        return {'title': '预览文件将随原片归档' if enabled else '预览文件不再归档',
                'detail': '正在进行的这一轮保持原设置。之后新扫描到的素材才按新设置处理。', 'level': 'success'}
    if 'media_scope' in backup:
        from .media import MEDIA_SCOPES
        return {'title': '素材范围已改为' + MEDIA_SCOPES[backup['media_scope']],
                'detail': '之后的扫描只导入所选类别。已经开始传输的文件会继续完成。', 'level': 'success'}
    notes = changes.get('notifications', {})
    if 'limits' in changes:
        limits = changes['limits']
        return {'title': '传输与缓存已生效' if intervals else '传输设置已生效', 'detail':
                f"上传限速：{format(limits['upload_mib_per_second'], 'g') + ' MiB/s' if limits['upload_mib_per_second'] else '不限速'}\n"
                f"缓存额度：{format(limits['max_cache_gib'], 'g') + ' GiB' if limits['max_cache_gib'] else '不限额'}\n"
                f"磁盘预留：{limits['min_free_disk_gib']:g} GiB" + ('\n\n' + intervals if intervals else ''), 'level': 'success'}
    if 'archive' in changes:
        from .bot_settings import location
        return {'title': '归档位置已生效', 'detail': location(changes['archive']) + '\n已有归档保持原位置。', 'level': 'success'}
    if notes and set(notes) != {'enabled'}:
        return {'title': '通知设置已生效', 'detail':
                ('自动通知：开启' if notes['enabled'] else '自动通知：关闭') +
                f"\n进度更新间隔：{notes['progress_interval_seconds']} 秒\n连接提醒：" +
                ('开启' if notes['connection_events'] else '关闭'), 'level': 'success'}
    if 'enabled' in notes:
        return {'title': '自动通知已开启' if notes['enabled'] else '自动通知已关闭',
                'detail': '进度、完成和异常会发到这个会话。' if notes['enabled'] else '确认相机、查看状态和设置操作仍会回复。',
                'level': 'success'}
    enabled = backup.get('delete_source_after_import')
    return {'title': '原片删除已启用' if enabled else '原片删除已停用',
            'detail': '整组素材完成本地导入与核验后自动删除原片，不等待远端归档。' if enabled else '后续导入保留相机原片。',
            'level': 'success'}


def _connection(state, local=False):
    if state.get('stale') or (state.get('available') if local else state.get('online')) is None:
        return '待确认'
    available = state.get('available') if local else state.get('online')
    return ('可用' if local else '在线') if available else ('不可用' if local else '未连接')


def _stopped(view, name):
    control = view.get('controls', {}).get(name, {})
    return bool(not view.get('running') or control.get('requested_stop') or
                control.get('state') in ('paused', 'pausing') or
                not view.get('transfer_enabled', view.get('configuration', {}).get('sync_enabled', True)))


def _stage_label(view, name):
    control = view.get('controls', {}).get(name, {})
    if control.get('state') == 'pausing':
        return '正在停止'
    if _stopped(view, name):
        return '已停止' if view.get('running') else '状态待确认'
    phase = view.get('stages', {}).get(name, {}).get('phase')
    return {'downloading': '导入中', 'uploading': '上传中', 'publishing': '确认归档',
            'scanning': '扫描中', 'verifying': '核验中', 'draining': '等待当前操作结束',
            'waiting_source': '等待相机', 'waiting_confirmation': '等待相机确认', 'waiting_destination': '等待存储',
            'waiting_cache': '等待缓存盘', 'waiting_space': '等待可用空间',
            'waiting_retry': '等待重试', 'failed': '等待重试'}.get(phase, '等待处理')


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
        return ('正在停止选定素材传输' if view.get('selected_import') else '正在停止同步'), 'orange'
    if configuration.get('pending'):
        return '设置等待生效', 'orange'
    if view.get('attention', {}).get('required'):
        return '任务需要处理', 'orange'
    selection = view.get('selected_import', {})
    if selection:
        if selection.get('state') == 'paused':
            return '选定素材传输已停止', 'grey'
        if selection.get('state') == 'done' or round_complete(view):
            return ('选定素材归档完成 · 缓存待清理', 'orange') if _cleanup_pending(view.get('cleanup', {})) else ('选定素材归档完成', 'green')
        stages = view.get('stages', {})
        importing = stages.get('import', {}).get('phase') == 'downloading'
        uploading = stages.get('upload', {}).get('phase') in ('uploading', 'publishing')
        if importing and uploading:
            return '选定素材导入与归档进行中', 'blue'
        if uploading:
            return '选定素材归档进行中', 'blue'
        if importing:
            return '选定素材导入进行中', 'blue'
        return ('选定素材等待重试', 'orange') if selection.get('state') == 'waiting' else ('选定素材等待传输', 'blue')
    if view.get('source_connection', {}).get('state') in ('pending', 'declined'):
        uploading = view.get('stages', {}).get('upload', {}).get('phase') in ('uploading', 'publishing')
        return ('远端归档进行中 · 相机待确认' if uploading else '相机待确认'), 'blue'
    if not configuration.get('sync_enabled', True):
        return '同步已停止', 'grey'
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
    if stages.get('import', {}).get('phase') in ('scanning', 'verifying') or view.get('status', {}).get('source_work'):
        return '正在核验素材', 'blue'
    if any(s.get('phase') == 'failed' for s in stages.values()) or view.get('issues'):
        return '部分文件等待重试', 'orange'
    if total or view.get('work', {}).get('pending_files'):
        if not view.get('health', {}).get('cache', {}).get('available', True):
            return '等待缓存盘恢复', 'orange'
        if batch.get('imported_files', 0) == total and total:
            return '等待远端归档', 'blue'
        if view.get('health', {}).get('source', {}).get('online') is False:
            return '等待相机连接', 'blue'
        return '等待传输', 'blue'
    return '同步待机', 'blue'


def headline_elements(view):
    unplug, batch = view.get('unplug', {}), view.get('batch', {})
    source = view.get('health', {}).get('source', {})
    ready = (view.get('running') and source.get('online') is True and not source.get('stale')
             and unplug.get('ready') and unplug.get('can_disconnect', True))
    # Access guidance is secondary to the transfer result, never the card title.
    if ready and batch.get('total_files'):
        text = ('本次选定素材' if view.get('selected_import') else '本轮') + '导入已完成，可断开相机连接。'
        if _pending_archive(view):
            text += '远端归档完成前，请保持电脑与缓存盘可用。'
        return [paragraph(text)]
    if ready:
        return []
    if view.get('running') and unplug.get('can_disconnect') and _stopped(view, 'import') and batch.get('total_files'):
        return [paragraph('相机读取已停止，可断开连接；本轮导入尚未完成。')]
    if view.get('running') and (view.get('status', {}).get('source_work') or
            not _stopped(view, 'import') and view.get('stages', {}).get('import', {}).get('phase') in ('downloading', 'scanning', 'verifying')):
        return [paragraph('相机正在执行读写操作，请保持连接。')]
    return []


def progress_elements(view):
    batch = view.get('batch', {})
    total = batch.get('total_files', 0)
    if not total:
        return []
    result = [markdown(f"**{total} 个文件** · {_size(batch.get('total_bytes'))}")]
    if not batch.get('scanned'):
        result.append(note('相机清单尚未复核；以下为已登记任务。'))
    for name, label, count, percent in (('import', '本地导入', 'imported_files', 'import_percent'),
                                        ('upload', '远端归档', 'completed_files', 'percent')):
        done = batch.get(count, 0)
        stage = view.get('stages', {}).get(name, {})
        if done == total:
            result.append(markdown(f"**{label}**　已完成 **{done} / {total}**"))
            continue
        bar, value = progress_bar(batch.get(percent))
        phase = _stage_label(view, name)
        if value == 100 and not _stopped(view, name):
            phase = '等待导入核验' if name == 'import' else '确认归档'
        percent_text = f'{math.floor(value * 10) / 10:.1f}'
        result.append(markdown(f"**{label}　{percent_text}%** · {phase}\n{bar}　已完成 **{done} / {total}**"))
        if not _stopped(view, name) and stage.get('current_file') and stage.get('phase') in ('downloading', 'uploading', 'publishing'):
            result.append(paragraph(str(stage['current_file']).rsplit('/', 1)[-1]))
            if stage.get('speed_bytes_per_second'):
                detail = _size(stage['speed_bytes_per_second']) + '/s'
                if stage.get('eta_seconds') is not None:
                    detail += ' · 本文件预计剩余 ' + _duration(stage['eta_seconds'])
                result.append(note(detail))
    return result


def attention_elements(view):
    attention = view.get('attention', {})
    if attention.get('required'):
        return [markdown('**需要处理**'), paragraph(attention.get('message', '').removeprefix('需要处理：'))]
    selected = view.get('selected_import', {})
    if selected.get('archive_verification_pending'):
        errors = selected.get('archive_errors', [])
        detail = ('\n' + str(errors[0].get('file', '')) + '\n' + str(errors[0].get('reason', ''))[:180]
                  if errors else '\n正在核对目的地文件，核验通过后计入完成。')
        return [paragraph(f"{selected['archive_verification_pending']} 个已有归档待核验" + detail)]
    # One waiting reason per message. Per-file errors do not hide working queues.
    for stage in view.get('stages', {}).values():
        if stage.get('phase', '').startswith('waiting') and stage.get('issue'):
            return [paragraph(stage['issue'].get('action') or '连接恢复后自动重试。')]
    issues = view.get('issues', [])
    if issues:
        first = issues[0]
        reason = first.get('cache_reason') or first.get('error') or first.get('source_cleanup_error') or first.get('cache_cleanup_error') or '等待重试'
        return [paragraph(f"待处理文件：{len(issues)} 个\n" + str(first.get('path', '')) + '\n' + str(reason)[:180])]
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
        lines.append('预览文件：' + '；'.join(previews))
    return '\n'.join(lines)


def _result_elements(result):
    outcome = result.get('cleanup', {})
    elements = [markdown(f"**{result['file_count']} 个文件** · {_size(result.get('byte_count'))}")]
    lines = cleanup_text(outcome, result.get('ignored_preview_files', 0)).splitlines()
    core = [line for line in lines if not line.startswith('预览文件：')]
    if core:
        elements.append(paragraph('\n'.join(core)))
    for line in lines:
        if line.startswith('预览文件：'):
            elements.append(note(line))
    return elements


def _selection_notes(view):
    selection, batch = view.get('selection', {}), view.get('batch', {})
    if view.get('selected_import'):
        parts = ['仅处理本次选定素材；其他任务保留', '本次保留相机原片']
        if batch.get('excluded_preview_files') and not round_complete(view):
            parts.append(f"预览文件未归档 {batch['excluded_preview_files']} 个")
        return [note('；'.join(parts))]
    parts = []
    if selection.get('scope', 'all') != 'all':
        parts.append('素材范围：' + MEDIA_SCOPES.get(selection['scope'], '所选类别'))
    if selection.get('excluded_source_files'):
        parts.append(f"范围外 {selection['excluded_source_files']} 个文件保留")
    if selection.get('cached_outside_scope_files'):
        parts.append(f"含其他类别的 {selection['cached_outside_scope_files']} 个既有缓存")
    if batch.get('excluded_preview_files') and not round_complete(view):
        parts.append(f"预览文件未归档 {batch['excluded_preview_files']} 个")
    return [note('；'.join(parts))] if parts else []


def _overview(view):
    result = attention_elements(view)
    result += progress_elements(view)
    if not view.get('batch', {}).get('total_files'):
        message = '当前无待处理任务。' if view.get('batch', {}).get('scanned') else '尚未完成素材扫描。'
        if view.get('work', {}).get('pending_files'):
            message = '已登记任务等待处理，尚未完成素材扫描。'
        if view.get('source_connection', {}).get('state') in ('pending', 'declined'):
            message = '请确认当前相机后开始备份；确认前不导入或删除素材。'
        result.append(paragraph(message))
    result += headline_elements(view)
    if round_complete(view):
        text = cleanup_text(view.get('cleanup', {}), view.get('batch', {}).get('excluded_preview_files', 0))
        if text:
            result.append(paragraph(text))
    result += _selection_notes(view) + backlog_elements(view)
    if view.get('temporary_cleanup'):
        result.append(note(f"待清理临时文件：{len(view['temporary_cleanup'])} 个，连接恢复后自动处理。"))
    return result


def progress_card(view, interval, keyword='', updated_at=None, *, notifications_enabled=True):
    title, color = _title(view)
    elements = ([note(keyword)] if keyword else []) + _overview(view)
    if not notifications_enabled:
        elements.append(note('自动进度更新已关闭；同步按当前设置运行。'))
    elif not view.get('transfer_enabled', view.get('configuration', {}).get('sync_enabled', True)):
        elements.append(note('自动进度更新已停止。'))
    elements.append(note('更新于 ' + format_time(updated_at or view.get('observed_at'))))
    return card(title, elements, color, shared=True)


def event_card(kind, title, text, view, *, keyword='', updated_at=None, locations=None, result=None):
    color = {'connected': 'blue', 'disconnected': 'grey', 'completed': 'green', 'imported': 'blue',
             'failed': 'orange', 'backlog': 'orange', 'recovered': 'green', 'test': 'blue'}.get(kind, 'blue')
    title = title.removeprefix('素材导入助手 · ')
    elements = [note(keyword)] if keyword else []
    enabled = view.get('configuration', {}).get('sync_enabled', False)
    if kind in ('connected', 'disconnected'):
        title = '相机连接已建立' if kind == 'connected' else '相机连接不可用'
        if kind == 'connected':
            elements.append(paragraph('USB 直连已就绪，绑定身份核对通过。' + ('同步已开启，将自动检测新素材。' if enabled else '同步已停止，需要导入时请开启同步。')))
        else:
            elements.append(paragraph('暂时无法通过 USB 网络访问相机。' + ('导入等待重连，完整缓存可继续上传。' if enabled else '同步已停止，任务与完整缓存保留。')))
    elif kind == 'completed':
        pending = _cleanup_pending(result.get('cleanup', {})) if result else any(word in text for word in ('待处理', '待确认', '待清理', '待核验'))
        title, color = ('归档完成 · 清理待处理', 'orange') if pending else ('归档完成', 'green')
        elements += _result_elements(result) if result else [paragraph(text)]
        for location in (locations or [])[:2]:
            elements.append(note('归档位置\n' + location))
    elif kind == 'imported':
        title = '本地导入完成'
        batch = view.get('batch', {})
        elements.append(markdown(f"**{batch.get('imported_files', 0)} 个文件** · {_size(batch.get('total_bytes'))}"))
        source = view.get('health', {}).get('source', {})
        if (view.get('running') and source.get('online') is True and not source.get('stale') and
                view.get('unplug', {}).get('ready') and view.get('unplug', {}).get('can_disconnect', True)):
            elements.append(paragraph('本轮素材已完整导入，可断开相机连接。'))
        else:
            elements.append(paragraph('相机状态尚未确认，请保持连接。'))
        pending = _pending_archive(view)
        if pending:
            elements.append(note(f'待归档：{pending} 个文件。归档完成前，请保持电脑与缓存盘可用。'))
    elif kind == 'backlog':
        title = '归档进度长时间未更新'
        backlog = view.get('backlog', {})
        elements.append(markdown(f"**{backlog.get('files', 0)} 个文件待归档** · {_size(backlog.get('bytes'))}"))
        elements.append(paragraph(text))
        elements += backlog_elements(view)
    else:
        if kind == 'failed':
            elements.append(markdown('**处理建议**'))
        elements.append(paragraph(text))
    elements.append(note(format_time(updated_at or view.get('observed_at'))))
    return card(title, elements, color, shared=kind in ('failed', 'backlog', 'recovered', 'completed'))


def status_content(view):
    title, color = _title(view)
    elements = _overview(view)
    health, disk = view.get('health', {}), view.get('disk', {})
    source, target = health.get('source', {}), health.get('destination', {})
    telemetry = source.get('telemetry', {})
    battery = ''
    if (source.get('online') and not source.get('stale') and telemetry.get('available')
            and not telemetry.get('stale') and telemetry.get('battery_percent') is not None):
        battery = ' · 电量 ' + str(telemetry['battery_percent']) + '%'
    elements.append({'tag': 'hr'})
    source_label = '已发现 · 等待确认' if source.get('confirmation') in ('pending', 'declined') and not source.get('stale') else _connection(source)
    elements.append(paragraph('相机：' + source_label + battery + '\n本地缓存：' +
        _connection(health.get('cache', {}), True) + ' · SMB：' + _connection(target)))
    if disk.get('available'):
        elements.append(note('缓存占用 ' + _size(disk.get('cache_used_bytes')) + ' · 磁盘可用 ' + (_size(disk['free_bytes']) if disk.get('free_bytes') is not None else '未知')))
    archive = view.get('archive', {})
    for path in (archive.get('locations') or archive.get('planned_locations', []))[:1]:
        elements.append(note('归档位置\n' + path))
    elements.append(note('原片策略：' + ('导入后自动删除' if view.get('policy', {}).get('delete_source') else '保留原片') +
                         ('（当前型号或固件未通过删除验收）' if view.get('policy', {}).get('delete_source_requested') and not view.get('policy', {}).get('delete_source') else '') +
                         '\n更新于 ' + format_time(view.get('observed_at'))))
    return title, color, elements


def format_time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return '时间未知'
        return stamp.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S 北京时间')
    except (ValueError, TypeError, OverflowError):
        return '时间未知'
