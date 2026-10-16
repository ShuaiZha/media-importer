"""Feishu command-card presentation; no transfer or settings mutations."""
from datetime import datetime
import math

from .notifications import _size, _duration
from .media import MEDIA_SCOPES


def plain(text):
    return {"tag": "plain_text", "content": str(text)}


def paragraph(text):
    # File names, errors and other external text must not become card markup.
    return {"tag": "div", "text": plain(text)}


def markdown(text):
    """Only use with application-owned labels and formatted numeric values."""
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def note(text):
    return {"tag": "note", "elements": [plain(text)]}


def columns(*texts):
    return {"tag": "div", "fields": [
        {"is_short": True, "text": {"tag": "lark_md", "content": text}} for text in texts]}


def card(title, elements, color="blue", *, shared=False):
    return {"config": {"wide_screen_mode": True, **({"update_multi": True} if shared else {})},
            "header": {"title": plain(title), "template": color}, "elements": elements}


def feedback(notice):
    if isinstance(notice, str):
        notice = {"title": "操作提示", "detail": notice, "level": "info"}
    icon = {"success": "✅", "warning": "⚠️", "error": "❌", "info": "ℹ️"}[notice["level"]]
    return [paragraph(icon + "  " + notice["title"]), note(notice["detail"]), {"tag": "hr"}]


def settings_result(changes, applied, stopping):
    backup, notifications = changes.get("backup", {}), changes.get("notifications", {})
    if "enabled" in backup:
        title = "同步已开启" if backup["enabled"] else "正在停止同步" if stopping else "同步已关闭"
    elif "delete_source_after_import" in backup:
        title = "已开启导入后删除原片" if backup["delete_source_after_import"] else "已设置保留相机原片"
    elif "media_scope" in backup:
        title = "同步内容已改为" + MEDIA_SCOPES[backup["media_scope"]]
    elif "source_transport" in backup:
        title = "连接方式已改为" + {"usb": "USB 直连", "wifi": "手动 Wi-Fi"}[backup["source_transport"]]
    elif backup.get('stop_after_current'):
        title = '传完当前文件后停止'
    elif "enabled" in notifications:
        title = "传输通知已开启" if notifications["enabled"] else "传输通知已关闭"
    elif "connection_events" in notifications:
        title = "连接提醒已开启" if notifications["connection_events"] else "连接提醒已关闭"
    else:
        title = "传输设置已保存" if "limits" in changes else "通知设置已保存"
    if not applied:
        return {"title": "设置已保存 · 等待生效", "detail": "已保存到 JSON，后台尚未确认应用；稍后点击底部「状态」查看。", "level": "warning"}
    detail = "已保存到 JSON，后台已应用。"
    if backup.get('stop_after_current'):
        detail += '两路只完成各自当前文件，不再启动新文件或新的原片删除。需要取消时点击「继续同步」。'
    if stopping:
        detail += "正在结束当前操作并清理半文件，完整缓存保留。"
    return {"title": title, "detail": detail, "level": "warning" if stopping else "success"}


def _connection(state, local=False):
    if not local and (state.get("stale") or state.get("online") is None):
        return "待更新"
    available = state.get("available") if local else state.get("online")
    return ("可用" if local else "在线") if available else "不可用"


def _stage_label(view, name):
    control = view.get("controls", {}).get(name, {})
    state = control.get("state", "paused" if control.get("requested_stop") else "running")
    phase = view.get("stages", {}).get(name, {}).get("phase")
    if state == "pausing":
        return "正在停止"
    if state == "paused":
        return "已停止" if view.get("running") else "服务未运行"
    return {"downloading": "导入中", "uploading": "上传中", "publishing": "确认归档",
            "scanning": "扫描中", "verifying": "核对中", "paused": "准备继续", "draining": "本路已停止，等待另一条传输"}.get(phase, "等待中")


def progress_bar(value):
    """Ten fixed-width blocks; partial progress must never look fully complete."""
    value = float(value or 0)
    value = min(100., max(0., value)) if math.isfinite(value) else 0.
    filled = min(10 if value == 100 else 9, int(value / 10))
    return "█" * filled + "░" * (10 - filled), value


def headline_elements(view):
    unplug = view.get('unplug', {})
    if unplug.get('ready'):
        subject = '本轮素材' if view.get('selection', {}).get('scope', 'all') == 'all' else '所选类别素材'
        title = '✅ **' + subject + '已收齐 · 相机可以拔走**'
    elif unplug.get('can_disconnect'):
        title = '📷 **当前可以断开相机 · 素材尚未确认收齐**'
    elif not view.get('running'):
        title = '⚠️ **相机是否可以拔走：暂不能确认**'
    elif view.get('health', {}).get('source', {}).get('online') is False:
        title = '📷 **相机未连接 · 完整缓存仍可上传**'
    elif view.get('health', {}).get('source', {}).get('online') is None or view.get('health', {}).get('source', {}).get('stale'):
        title = '📷 **相机状态待确认**'
    else:
        title = '📷 **导入尚未完成 · 请保持相机连接**'
    attention = view.get('attention', {'required': False, 'message': '暂无需要处理的问题。'})
    elements = [markdown(title), paragraph(unplug.get('message', '请从「状态 → 运行总览」确认相机读取是否已结束。'))]
    selection = view.get('selection', {})
    if selection:
        label = '同步内容 · ' + selection['label']
        if not selection.get('applied', True):
            label += ' · 等待后台应用并扫描'
        elif selection.get('excluded_source_files'):
            label += f" · 本轮不导入 {selection['excluded_source_files']} 个其他文件"
        elements.append(note(label))
    if attention['required'] or '无需处理' not in attention['message']:
        label = '⚠️ **需要处理**' if attention['required'] else 'ℹ️ **当前等待**'
        elements += [markdown(label), paragraph(attention['message'].removeprefix('需要处理：'))]
    else:
        elements.append(note('无需处理 · ' + attention['message'].removeprefix('无需处理：')))
    return elements


def progress_elements(view):
    """Shared by queried status cards and periodically updated progress cards."""
    batch = view.get('batch', {})
    if not batch.get('total_files'):
        empty = '所选类别没有待导入素材' if view.get('selection', {}).get('scope', 'all') != 'all' else '没有待处理素材'
        elements = [markdown('**' + empty + '**' if batch.get('scanned') else '**尚未扫描素材**')]
        if any(stage.get('phase') == 'paused' and not view.get('controls', {}).get(name, {}).get('requested_stop')
               and view.get('controls', {}).get(name, {}).get('state') == 'running'
               for name, stage in view.get('stages', {}).items()):
            elements.append(note('已收到继续指令，等待队列更新。'))
        if not view.get('selection', {}).get('cached_outside_scope_files'):
            return elements
    else:
        label = '本轮所选素材' if view.get('selection', {}).get('scope', 'all') != 'all' else '本轮素材'
        label = label if batch.get('scanned') else '已记录素材 · 尚未重新扫描相机'
        elements = [markdown(f"**{label}**　{batch['total_files']} 个文件 · {_size(batch.get('total_bytes'))}")]
    for name, label, count, percent, verb in (
            ('import', '💾 导入到缓存', 'imported_files', 'import_percent', '已完整导入'),
            ('upload', '📁 归档到存储', 'completed_files', 'percent', '已正式归档')):
        bar, value = progress_bar(batch.get(percent))
        done, total = batch.get(count, 0), batch.get('total_files', 0)
        stage = view.get('stages', {}).get(name, {})
        control = view.get('controls', {}).get(name, {})
        stopped = control.get('requested_stop') or control.get('state') in ('paused', 'pausing') or not view.get('running')
        state_label = _stage_label(view, name)
        if total and done == total:
            state_label = '导入完成' if name == 'import' else '归档完成'
        elif value == 100 and not stopped:
            state_label = '等待导入核验' if name == 'import' else '正在确认归档'
        if total:
            elements.append(markdown(f"**{label} · {value:.1f}%**\n{bar}\n{verb} **{done} / {total}** 个 · {state_label}"))
        elif name == 'upload':
            elements.append(markdown(f"**{label} · {_stage_label(view, name)}**"))
        else:
            continue
        if name == 'upload' and view.get('selection', {}).get('cached_outside_scope_files'):
            elements.append(note('其他类别的完整缓存仍会上传，不计入上方所选类别进度。'))
        if stopped:
            continue
        queue = view.get('queues', {}).get(name, {})
        if stage.get('current_file') and stage.get('phase') in ('downloading', 'uploading', 'publishing', 'verifying'):
            elements.append(paragraph(str(stage['current_file']).rsplit('/', 1)[-1]))
            detail = f"本文件 {_size(stage.get('current_bytes'))} / {_size(stage.get('current_total_bytes'))}"
            if stage.get('speed_bytes_per_second'):
                detail += f" · {_size(stage['speed_bytes_per_second'])}/s · 本文件剩余 {_duration(stage.get('eta_seconds'))}"
            elements.append(note(detail))
        if stage.get('phase') == 'paused':
            elements.append(note('已收到继续指令，等待队列更新。'))
        elif stage.get('message') and stage.get('phase') not in ('downloading', 'uploading'):
            elements.append(paragraph(str(stage['message'])[:240]))
        if queue.get('pending_files'):
            elements.append(note(f"待处理 {queue['pending_files']} 个 · {_size(queue.get('remaining_bytes'))}"))
    if batch.get('percent', 0) >= 100 and batch.get('completed_files', 0) < batch.get('total_files', 0):
        elements.append(note('上传字节已到达，仍需大小核对与正式改名；当前尚未全部归档。'))
    return elements


def backlog_elements(view):
    backlog = view.get('backlog', {})
    if not backlog.get('files'):
        return []
    elements = [markdown(f"💾 **待归档缓存 {backlog['files']} 个 · {_size(backlog['bytes'])}**")]
    if backlog.get('source_removed_files'):
        elements.append(paragraph(f"其中 {backlog['source_removed_files']} 个的相机原片已清理或不存在；当前链路仅确认缓存盘有完整副本，请保持连接。"))
    if backlog.get('oldest_age_seconds') is not None:
        elements.append(note('最早一项已等待 ' + _duration(backlog['oldest_age_seconds'])))
    if backlog.get('unknown_age_files'):
        elements.append(note(f"{backlog['unknown_age_files']} 个历史缓存未记录导入时间；不估算其等待时长。"))
    if backlog.get('verified_files', 0) < backlog['files']:
        elements.append(note('部分缓存当前无法核验，请检查原缓存盘；不据此认定文件安全可用。'))
    return elements


def progress_card(view, interval, keyword='', updated_at=None, *, notifications_enabled=True):
    batch = view.get('batch', {})
    enabled = view.get('configuration', {}).get('sync_enabled', True)
    complete = bool(batch.get('total_files') and batch.get('completed_files') == batch['total_files']
                    and not view.get('selection', {}).get('cached_outside_scope_files'))
    title, color = ('✅ 本轮已归档', 'green') if complete else ('🔄 传输进度', 'blue')
    if view.get('attention', {}).get('required'):
        title, color = '⚠️ 传输需要处理', 'orange'
    if not enabled:
        title, color = '⏸ 同步已关闭', 'grey'
    elif not notifications_enabled:
        title, color = '🔕 自动通知已关闭', 'grey'
    elif not view.get('running'):
        title, color = '⚠️ 传输服务待检查', 'orange'
    elements = ([note(keyword)] if keyword else []) + headline_elements(view)
    elements += [{'tag': 'hr'}] + progress_elements(view)
    elements += backlog_elements(view)
    if view.get('configuration', {}).get('stop_after_current'):
        elements.append(markdown('⏳ **传完当前文件后停止，不再启动新文件或新的原片删除。**'))
    if complete:
        elements.append(note('本轮更新结束；归档结果另行提醒。'))
    elif not enabled or not notifications_enabled:
        elements.append(note('自动进度更新已停止；任务和完整缓存保留。'))
    else:
        elements.append(note(f'每 {_duration(interval)} 更新此卡片；完成和失败单独提醒。底部「状态」可随时查询。'))
    elements.append(note('更新于 ' + format_time(updated_at or view.get('observed_at'))))
    return card(title, elements, color, shared=True)


def event_card(kind, title, text, view, *, keyword='', updated_at=None, locations=None):
    icon, color = {'connected': ('📷', 'green'), 'disconnected': ('🔌', 'orange'),
                   'completed': ('✅', 'green'), 'imported': ('📷', 'green'),
                   'failed': ('⚠️', 'orange'), 'backlog': ('💾', 'orange'),
                   'recovered': ('✅', 'green'), 'test': ('✅', 'blue')}.get(kind, ('ℹ️', 'blue'))
    title = title.removeprefix('素材导入助手 · ')
    elements = [note(keyword)] if keyword else []
    if kind in ('connected', 'disconnected'):
        elements += [paragraph(text), {"tag": "hr"}]
        enabled = view.get('configuration', {}).get('sync_enabled', False)
        if kind == 'connected':
            elements.append(markdown('**同步已开启，将按当前类别导入。**' if enabled else '**同步已关闭，尚未开始导入。**'))
            if not enabled:
                elements.append(note('需要备份时点击「同步 → 开启同步」。'))
        else:
            elements.append(note('已有完整缓存仍可继续上传，未完成导入等待重新连接。' if enabled else '同步已关闭，任务与完整缓存保留。'))
        elements.append(note('「状态 → 设备与存储」查看当前连接；连接提醒可在「设置 → 通知设置」关闭。'))
    elif kind == 'completed':
        elements += [markdown('**素材已正式归档**'), paragraph(text), {'tag': 'hr'}, markdown('📁 **实际归档位置**')]
        elements.extend(paragraph(location) for location in (locations or [])[:3])
        if not locations:
            elements.append(note('归档记录中暂无可展示的路径，请从「状态」查看。'))
    elif kind == 'imported':
        elements += [markdown('**这批素材已完整导入或归档**'), paragraph(text), {'tag': 'hr'}]
        elements += headline_elements(view)[:2]
        elements.append(note('电脑与缓存盘请保持可用，完整缓存可以继续上传。'))
    elif kind == 'failed':
        elements += [markdown('**当前问题**'), paragraph(title), markdown('**如何处理**'), paragraph(text)]
        elements.append(note('已完成的文件与完整缓存保留；同一持续故障不重复提醒。'))
    elif kind == 'backlog':
        elements += [markdown('**缓存仍未归档**'), paragraph(text)] + backlog_elements(view)
    elif kind == 'recovered':
        elements += [markdown('**此问题已恢复**'), paragraph(text)]
    else:
        elements += [markdown('**通知连接成功**'), paragraph(text)]
    elements.append(note('通知时间 ' + format_time(updated_at or view.get('observed_at')) + ' · 底部「状态」查看最新进展'))
    return card(icon + ' ' + title, elements, color)


def status_content(view):
    """The default overview answers what is happening and whether to intervene."""
    batch, health = view["batch"], view["health"]
    enabled, running = view["configuration"]["sync_enabled"], view["running"]
    stopping = any(c["state"] == "pausing" for c in view["controls"].values())
    complete = bool(batch["total_files"] and batch["completed_files"] == batch["total_files"]
                    and not view.get('selection', {}).get('cached_outside_scope_files'))
    if not running:
        title, color = "运行总览 · 服务待检查", "orange"
    elif stopping:
        title, color = "运行总览 · 正在停止", "orange"
    elif view['configuration'].get('stop_after_current'):
        title, color = "运行总览 · 传完当前文件后停止", "blue"
    elif not enabled:
        title, color = "运行总览 · 同步已关闭", "grey"
    elif complete:
        title, color = "运行总览 · 本轮已归档", "green"
    elif view['stages'].get('upload', {}).get('phase') in ('uploading', 'publishing'):
        title, color = "运行总览 · 正在归档", "blue"
    elif view['stages'].get('import', {}).get('phase') == 'downloading':
        title, color = "运行总览 · 正在导入", "blue"
    else:
        title, color = "运行总览 · 同步已开启", "blue"
    attention = view.get('attention', {'required': False, 'message': '暂无需要处理的问题。'})
    if attention['required']:
        title, color = '运行总览 · 需要处理', 'orange'
    title = ('⚠️ ' if color == 'orange' else '✅ ' if color == 'green' else '⏸ ' if color == 'grey' else '🔄 ') + title
    elements = headline_elements(view)
    if enabled or stopping:
        elements.append(note('正在结束当前操作' if stopping else '不再启动新文件或新的原片删除' if view['configuration'].get('stop_after_current') else '同步已开启 · 持续监听新素材，重连后自动导入'))
    elements += [{'tag': 'hr'}, paragraph("  ·  ".join(
        label + " " + _connection(health[key], key == "cache")
        for key, label in (("source", "相机"), ("cache", "缓存盘"), ("destination", "目的地"))))]
    elements.append({"tag": "hr"})
    elements += progress_elements(view)
    elements += backlog_elements(view)
    cleanup = view["totals"]
    pending = [f"{label} {count}" for label, count in (
        ("相机原片", cleanup["pending_cleanup"]), ("完整缓存", cleanup["cache_cleanup_pending"]),
        ("半文件", len(view["temporary_cleanup"]))) if count]
    if pending:
        elements.append(paragraph("待清理：" + " · ".join(pending)))
    if view.get('issues'):
        elements.append(note(f"{len(view['issues'])} 个文件有异常或待清理；详情见「状态 → 归档结果」。"))
    elements.append(note(('完整导入后删除原片' if view['policy']['delete_source'] else '保留相机原片') +
                         ' · 正式归档后清理对应缓存'))
    elements.append(note('查询快照 · ' + format_time(view['observed_at']) + ' · 点击下方刷新'))
    return title, color, elements


def _known_size(value):
    return _size(value) if value is not None else '未知'


def _observation(state):
    return ('最近观测 · ' + format_time(state.get('checked_at')) +
            (' · 已过期，等待更新' if state.get('stale') else ''))


def device_content(view):
    """Connection diagnostics live here, without repeating transfer/history cards."""
    health, disk = view['health'], view['disk']
    source, target = health['source'], health['destination']
    elements = [markdown('📷 **相机 · ' + _connection(source) + '**')]
    identity = ' · '.join(str(source[key]) for key in ('model_name', 'serial_number') if source.get(key))
    elements.append(paragraph(identity or '尚未读取到绑定相机的身份'))
    if source.get('firmware_version'):
        elements.append(note('固件 ' + source['firmware_version']))
    if source.get('transport'):
        elements.append(note('连接方式 · ' + {'wifi': '手动 Wi-Fi', 'usb': 'USB 直连'}.get(source['transport'], '未知')))
    if source.get('wireless'):
        elements.append(paragraph(str(source['wireless'].get('message') if source['wireless'].get('state') != 'unavailable' else
                                       '请在电脑手动连接相机热点；这里显示相机的网络可访问状态，飞书不会切换电脑 Wi-Fi。')[:300]))
    telemetry = source.get('telemetry', {})
    fresh = bool(source.get('online') and not source.get('stale') and telemetry.get('available') and not telemetry.get('stale'))
    if fresh:
        battery = '未安装电池' if telemetry.get('battery_present') is False else (
            str(telemetry['battery_percent']) + '%' if telemetry.get('battery_percent') is not None else '未知 / 不支持')
        charging = {True: '正在充电', False: '未充电'}.get(telemetry.get('charging'), '充电状态未知')
        working = '正在录像' if telemetry.get('recording') else '忙碌' if telemetry.get('busy') else (
            '就绪' if telemetry.get('ready') is True and telemetry.get('recording') is False and telemetry.get('busy') is False else '状态未知')
        temperature = '过热告警' if telemetry.get('overheating') else '过冷告警' if telemetry.get('cold') else (
            '无温度告警' if telemetry.get('overheating') is False and telemetry.get('cold') is False else '温度告警状态未知')
        storage = {'ready': '正常', 'full': '已满', 'missing': '未插入', 'format_error': '格式异常',
                   'busy': '忙碌', 'error': '异常'}.get(telemetry.get('storage_status'), '未知')
        elements += [paragraph(f'电量 {battery} · {charging}\n{working} · {temperature}'),
                     paragraph(f"存储卡 {storage} · 剩余 {_known_size(telemetry.get('storage_remaining_bytes'))}")]
    else:
        elements.append(note('电量、充电、温度告警与存储卡状态待更新；不以旧数据判断正常。'))
    if source.get('reason'):
        elements.append(paragraph(str(source['reason'])[:300]))
    elements.append(note(_observation(source)))

    elements += [{'tag': 'hr'}, markdown('💾 **缓存与磁盘 · ' + _connection(health['cache'], True) + '**')]
    location = view.get('storage', {}).get('cache', {})
    elements.append(paragraph('本机目录\n' + str(location.get('host') or '未提供宿主机路径')))
    elements.append(note('容器挂载 ' + str(location.get('path') or disk.get('path') or '未知')))
    if health['cache'].get('available'):
        quota = _known_size(disk.get('quota_bytes')) if disk.get('quota_bytes') != 0 else '不限额'
        waiting = [f for f in view.get('files', []) if f.get('cache_available') and f.get('state') != 'uploaded']
        elements += [markdown(f"缓存占用 **{_known_size(disk.get('cache_used_bytes'))}** / 额度 {quota}"),
                     paragraph(f"完整待归档 {len(waiting)} 个 · {_size(sum(f['size'] for f in waiting))}\n未完成文件 {_known_size(disk.get('partial_cache_bytes'))}"),
                     markdown(f"磁盘可用 **{_known_size(disk.get('free_bytes'))}** · 至少预留 {_known_size(disk.get('reserve_bytes'))}")]
        if disk.get('unverified_cache_files'):
            elements.append(paragraph(f"⚠️ {disk['unverified_cache_files']} 个缓存待核验 · {_known_size(disk.get('unverified_cache_bytes'))}；未归档文件核验前不计入导入完成。"))
        if disk.get('extra_bytes_needed'):
            elements.append(paragraph('下一文件空间不足，还需释放 ' + _size(disk['extra_bytes_needed']) + '。'))
        if disk.get('quota_extra_bytes'):
            elements.append(paragraph('下一文件超过缓存额度，需先上传释放 ' + _size(disk['quota_extra_bytes']) + ' 或调整额度。'))
    else:
        elements.append(paragraph(str(disk.get('reason') or '缓存盘不可用') + '\n' +
                                  str(disk.get('action') or '请检查原缓存盘和 Docker 挂载；不会改用内置磁盘。')))

    elements += [{'tag': 'hr'}, markdown('📁 **SMB 目的地 · ' + _connection(target) + '**')]
    for location in view.get('archive', {}).get('planned_locations', [])[:1]:
        elements.append(paragraph(location))
    if target.get('issue'):
        issue = target['issue']
        elements.append(paragraph(str(issue.get('message') or target.get('reason') or '连接需要检查')[:300]))
        elements.append(note(str(issue.get('action') or '请检查连接配置。')[:300]))
        if not view['configuration']['sync_enabled']:
            elements.append(note('同步关闭中；开启后才会执行导入和上传。'))
    elif target.get('reason'):
        elements.append(paragraph(str(target['reason'])[:300]))
    elements.append(note(_observation(target)))
    checked = view.get('preflight')
    if checked:
        labels = {'ok': '通过', 'waiting': '有等待项', 'error': '有待处理项', 'checking': '检查中'}
        elements += [{'tag': 'hr'}, note('最近自检 ' + labels.get(checked['state'], '未知') + ' · ' +
                     format_time(checked.get('finished_at') or checked.get('started_at')) + '；当前连接以上方观测为准')]
    elements.append(note('查询快照 · ' + format_time(view['observed_at']) + ' · 点击下方刷新'))
    problematic = not health['cache'].get('available') or view.get('attention', {}).get('required')
    return '设备与存储', 'orange' if problematic else 'blue', elements


def archive_content(view):
    """Recorded outcomes are distinct from live bytes, current policy and settings."""
    elements = [markdown('📋 **最近一次结果**')]
    recent = view.get('recent_result')
    if recent:
        label = '最近完成批次' if recent['complete'] else '最近批次 · 尚未完成'
        elements.append(markdown(f"**{label}**\n已正式归档 **{recent['archived_files']} / {recent['total_files']}** 个 · 待归档 {recent['pending_files']} 个 · 异常 {recent['failed_files']} 个"))
        if recent.get('finished_at'):
            elements.append(note('完成时间 ' + format_time(recent['finished_at'])))
        elements.append(note(f"原片已清理或复查时不存在 {recent['source_deleted']} 个 · 已归档缓存待清理 {recent['cache_cleanup_pending']} 个"))
    else:
        elements.append(paragraph('尚无任务结果。未扫描不代表相机没有素材。'))
    archive = view.get('archive', {})
    actual = archive.get('locations', [])
    totals = view['totals']
    elements += [{'tag': 'hr'}, markdown(f"📁 **归档记录**\n累计已归档 **{totals.get('uploaded_files', 0)}** 个 · {_size(totals.get('uploaded_bytes'))}\n仍待归档 {totals.get('pending_files', 0)} 个"),
                 markdown('**实际归档目录**' if actual else '**计划归档位置 · 尚无已归档文件**')]
    elements.extend(paragraph(location) for location in (actual or archive.get('planned_locations', []))[:3])
    if recent and recent['archives']:
        elements.append(markdown('**最近批次归档示例**'))
        for item in recent['archives'][:3]:
            original, final = item['source'].rsplit('/', 1)[-1], item['location'].rsplit('/', 1)[-1]
            label = original + (' → ' + final if original != final else '')
            elements.append(paragraph(label + '\n' + item['location']))
        if len(recent['archives']) > 3:
            elements.append(note(f"本卡展示 3 个，共 {len(recent['archives'])} 个已归档文件；完整文件见上述目录。"))
    pending = totals.get('pending_cleanup', 0) + totals.get('cache_cleanup_pending', 0) + len(view['temporary_cleanup'])
    if pending or view['issues']:
        elements += [{'tag': 'hr'}, markdown('⚠️ **异常与待清理**'),
                     paragraph(f"相机原片待清理 {totals.get('pending_cleanup', 0)} 个 · 已归档缓存待清理 {totals.get('cache_cleanup_pending', 0)} 个\n未完成临时文件待清理 {len(view['temporary_cleanup'])} 个")]
        for item in view['issues'][:3]:
            reason = (item.get('cache_reason') if item.get('cache_state') == 'unverified' else
                      item.get('error') or item.get('source_cleanup_error') or item.get('cache_cleanup_error'))
            elements.append(paragraph(str(item['path']) + '\n' + str(reason)[:300]))
        if len(view['issues']) > 3:
            elements.append(note(f"另有 {len(view['issues']) - 3} 个异常，点击「查看全部异常」逐页查看。"))
        for item in view['temporary_cleanup'][:2]:
            if item.get('error'):
                elements.append(paragraph('临时文件清理：' + str(item['error'])[:300]))
        elements.append(note('归档成功不因缓存清理失败而倒退；尚未归档的完整缓存保留。'))
    source_policy = '完整导入后删除原片' if view['policy']['delete_source'] else '保留相机原片'
    elements.append(note('当前策略：' + source_policy + '；不代表历史原片的实际处理结果。'))
    elements.append(note('查询快照 · ' + format_time(view['observed_at']) + ' · 点击下方刷新'))
    complete = bool(recent and recent['complete'] and not totals.get('pending_files'))
    return '归档结果', 'orange' if view['issues'] else 'green' if complete else 'blue', elements


def format_time(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.strftime('%m-%d %H:%M:%S ') + (stamp.tzname() or '时区未知')
    except (ValueError, TypeError):
        return '时间未知'
