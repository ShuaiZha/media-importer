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
    backup, notifications = changes.get('backup', {}), changes.get('notifications', {})
    if 'enabled' in backup:
        title = '同步已开启' if backup['enabled'] else '正在停止同步' if stopping else '同步已关闭'
        detail = '正在扫描素材。' if backup['enabled'] else '正在结束操作，完整缓存保留。' if stopping else '任务和完整缓存已保留。'
    elif 'delete_source_after_import' in backup:
        title = '已开启导入后删除原片' if backup['delete_source_after_import'] else '已设置保留相机原片'
        detail = ''
    elif 'media_scope' in backup:
        title, detail = '同步内容：' + MEDIA_SCOPES[backup['media_scope']], '已有完整缓存继续上传。'
    elif 'archive_previews' in backup:
        title = '归档预览文件已' + ('开启' if backup['archive_previews'] else '关闭')
        detail = '新一轮生效；已登记任务和完整缓存按原规则处理。'
    elif 'source_transport' in backup:
        title = '连接方式：' + {'usb': 'USB 直连', 'wifi': '手动 Wi-Fi'}[backup['source_transport']]
        detail = '插线并开启相机。' if backup['source_transport'] == 'usb' else '请先在电脑连接相机热点。'
    elif backup.get('stop_after_current'):
        title, detail = '传完当前文件后停止', '两路不再启动新文件，原片删除也停止。'
    elif 'enabled' in notifications:
        title, detail = ('传输通知已开启' if notifications['enabled'] else '传输通知已关闭'), ''
    elif 'connection_events' in notifications:
        title, detail = ('连接提醒已开启' if notifications['connection_events'] else '连接提醒已关闭'), ''
    else:
        title, detail = ('传输设置已保存' if 'limits' in changes else '通知设置已保存'), ''
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
        if not round_complete(view):
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
    label = '历史记录' if batch.get('historical') else '本轮任务'
    selection = view.get('selection', {})
    if selection.get('scope', 'all') != 'all' and not batch.get('historical'):
        label += ' · ' + selection.get('label', MEDIA_SCOPES[selection['scope']])
    result = [note(f"{label} · {total} 个文件 · {_size(batch.get('total_bytes'))}")]
    if selection.get('cached_outside_scope_files'):
        result.append(note(f"含此前其他类别的 {selection['cached_outside_scope_files']} 个完整缓存，继续上传。"))
    if not batch.get('scanned') and not batch.get('historical'):
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
        return ('✅ 历史素材已归档' if view.get('batch', {}).get('historical') else '✅ 本轮已归档'), 'green'
    if any(s.get('phase') in ('uploading', 'publishing') for s in view.get('stages', {}).values()):
        return '🔄 正在归档', 'blue'
    if view.get('stages', {}).get('import', {}).get('phase') == 'downloading':
        return '🔄 正在导入', 'blue'
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
        result.append(note(f"{len(view['issues'])} 个文件待处理，详情见归档结果。"))
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
    elements = _overview(view)
    elements.append(note('查询于 ' + format_time(view.get('observed_at'))))
    return title + ' · 运行总览', color, elements


def _known_size(value):
    return _size(value) if value is not None else '未知'


def device_content(view, *, details=False):
    health, disk = view['health'], view['disk']
    source, target = health['source'], health['destination']
    elements = [markdown('📷 **相机 · ' + _connection(source) + '**')]
    mode = {'usb': 'USB 直连', 'wifi': '手动 Wi-Fi'}.get(source.get('transport'), '')
    elements.append(paragraph(' · '.join(x for x in (source.get('model_name'), mode) if x) or '尚未读取相机身份'))
    telemetry = source.get('telemetry', {})
    if source.get('online') and not source.get('stale') and telemetry.get('available') and not telemetry.get('stale'):
        percent = telemetry.get('battery_percent')
        battery = '未安装电池' if telemetry.get('battery_present') is False else f'{percent}%' if percent is not None else '未知'
        charging = {True: '正在充电', False: '未充电'}.get(telemetry.get('charging'), '充电状态未知')
        work = '正在录像' if telemetry.get('recording') else '忙碌' if telemetry.get('busy') else (
            '就绪' if telemetry.get('ready') and telemetry.get('recording') is False and telemetry.get('busy') is False else '状态未知')
        temp = '过热告警' if telemetry.get('overheating') else '过冷告警' if telemetry.get('cold') else (
            '无温度告警' if telemetry.get('overheating') is False and telemetry.get('cold') is False else '温度告警未知')
        storage = {'ready': '正常', 'full': '已满', 'missing': '未插入', 'format_error': '格式异常', 'busy': '忙碌', 'error': '异常'}.get(telemetry.get('storage_status'), '未知')
        elements.append(paragraph(f"电量 {battery} · {charging}\n{work} · {temp}\n存储卡 {storage} · 剩余 {_known_size(telemetry.get('storage_remaining_bytes'))}"))
    else:
        elements.append(note('电量、温度告警与存储卡状态待更新。'))
    if source.get('stale') and source.get('checked_at'):
        elements.append(note('相机数据已过期 · 最后观测 ' + format_time(source['checked_at'])))
    elements += [{'tag': 'hr'}, markdown('💾 **缓存盘 · ' + _connection(health['cache'], True) + '**')]
    if health['cache'].get('available'):
        quota = _known_size(disk.get('quota_bytes')) if disk.get('quota_bytes') != 0 else '不限'
        elements.append(paragraph(f"缓存 {_known_size(disk.get('cache_used_bytes'))} / 额度 {quota}\n磁盘可用 {_known_size(disk.get('free_bytes'))} · 预留 {_known_size(disk.get('reserve_bytes'))}"))
        if disk.get('unverified_cache_files'):
            elements.append(paragraph(f"{disk['unverified_cache_files']} 个缓存待核验"))
        for key, label in (('extra_bytes_needed', '磁盘还需释放 '), ('quota_extra_bytes', '缓存额度还需腾出 ')):
            if disk.get(key):
                elements.append(paragraph(label + _size(disk[key])))
    else:
        elements.append(paragraph(str(disk.get('reason') or '缓存盘不可用') + '\n' + str(disk.get('action') or '请接回原缓存盘并检查挂载。')))
    elements += [{'tag': 'hr'}, markdown('📁 **SMB · ' + _connection(target) + '**')]
    issue = target.get('issue') or {}
    if issue or target.get('reason'):
        elements.append(paragraph(str(issue.get('action') or target.get('reason'))[:180]))
    if target.get('stale') and target.get('checked_at'):
        elements.append(note('目的地数据已过期 · 最后观测 ' + format_time(target['checked_at'])))
    if details:
        elements += [{'tag': 'hr'}, markdown('**连接详情**'),
                     paragraph('序列号：' + str(source.get('serial_number') or '未知') + '\n固件：' + str(source.get('firmware_version') or '未知'))]
        location = view.get('storage', {}).get('cache', {})
        elements.append(paragraph('本机缓存：' + str(location.get('host') or '未知') + '\n容器挂载：' + str(location.get('path') or disk.get('path') or '未知')))
        elements.append(paragraph('完整缓存 ' + _known_size(disk.get('complete_cache_bytes')) + ' · 未完成文件 ' + _known_size(disk.get('partial_cache_bytes'))))
        elements.extend(paragraph(path) for path in view.get('archive', {}).get('planned_locations', [])[:1])
        checked = view.get('preflight')
        if checked:
            elements.append(note('最近自检：' + {'ok': '通过', 'waiting': '有等待项', 'error': '需要处理', 'checking': '检查中'}.get(checked.get('state'), '未知') + ' · ' + format_time(checked.get('finished_at') or checked.get('started_at'))))
    elements.append(note('查询于 ' + format_time(view.get('observed_at'))))
    return '设备与存储' + (' · 详情' if details else ''), 'orange' if view.get('attention', {}).get('required') else 'blue', elements


def archive_content(view, *, details=False):
    recent = view.get('recent_result')
    elements = []
    if recent:
        label = '历史归档汇总' if recent.get('historical') else '最近一轮已归档' if recent['complete'] else '本轮尚未完成'
        elements.append(markdown(f"**{label}**\n已正式归档 **{recent['archived_files']} / {recent['total_files']}** 个 · {_size(recent.get('total_bytes'))}"))
        if recent['pending_files'] or recent['failed_files']:
            elements.append(paragraph(f"待归档 {recent['pending_files']} 个 · 异常 {recent['failed_files']} 个"))
        if recent.get('excluded_preview_files'):
            elements.append(note(f"按设置忽略 {recent['excluded_preview_files']} 个预览文件。"))
        outcome = cleanup_text(recent.get('cleanup', {}))
        if outcome:
            elements.append(paragraph(outcome))
        if recent.get('finished_at'):
            elements.append(note('完成于 ' + format_time(recent['finished_at'])))
    else:
        elements.append(paragraph('尚无任务结果，等待扫描素材。'))
    archive, totals = view.get('archive', {}), view['totals']
    actual = archive.get('locations', [])
    elements += [{'tag': 'hr'}, markdown('**实际归档目录**' if actual else '**计划位置 · 尚无已归档文件**')]
    elements.extend(paragraph(path) for path in (actual or archive.get('planned_locations', []))[:3])
    if details and recent:
        for item in recent.get('archives', [])[:5]:
            original, final = item['source'].rsplit('/', 1)[-1], item['location'].rsplit('/', 1)[-1]
            elements.append(paragraph(original + (' → ' + final if original != final else '') + '\n' + item['location']))
        if len(recent.get('archives', [])) > 5:
            elements.append(note('展示 5 个文件，完整文件见归档目录。'))
    if view.get('issues') or view.get('temporary_cleanup'):
        elements.append(paragraph(f"待处理 {len(view.get('issues', []))} 个文件 · 临时文件待清理 {len(view.get('temporary_cleanup', []))} 个"))
    elements.append(note(f"累计归档 {totals.get('uploaded_files', 0)} 个 · {_size(totals.get('uploaded_bytes'))}；仍待归档 {totals.get('pending_files', 0)} 个"))
    elements.append(note('查询于 ' + format_time(view.get('observed_at'))))
    return '归档结果' + (' · 文件详情' if details else ''), 'orange' if view.get('issues') else 'green' if recent and recent['complete'] and not totals.get('pending_files') else 'blue', elements


def format_time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return '时间未知'
        return stamp.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S 北京时间')
    except (ValueError, TypeError, OverflowError):
        return '时间未知'
