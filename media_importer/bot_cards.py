"""Feishu command-card presentation; no transfer or settings mutations."""
from datetime import datetime
import math

from .notifications import _size, _duration


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
    elif "enabled" in notifications:
        title = "自动通知已开启" if notifications["enabled"] else "自动通知已关闭"
    else:
        title = "传输设置已保存" if "limits" in changes else "通知设置已保存"
    if not applied:
        return {"title": "设置已保存 · 等待生效", "detail": "已保存到 JSON，后台尚未确认应用；稍后点击底部「状态」查看。", "level": "warning"}
    detail = "已保存到 JSON，后台已应用。"
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
            "scanning": "扫描中", "verifying": "核对中", "paused": "准备继续"}.get(phase, "等待中")


def progress_bar(value):
    """Ten fixed-width blocks; partial progress must never look fully complete."""
    value = float(value or 0)
    value = min(100., max(0., value)) if math.isfinite(value) else 0.
    filled = min(10 if value == 100 else 9, int(value / 10))
    return "█" * filled + "░" * (10 - filled), value


def headline_elements(view):
    unplug = view.get('unplug', {})
    if unplug.get('ready'):
        title = '✅ **本轮素材已收齐 · 相机可以拔走**'
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
    label = '⚠️ **需要处理**' if attention['required'] else 'ℹ️ **当前提示**' if '无需处理' not in attention['message'] else '✅ **无需处理**'
    return [markdown(title), paragraph(unplug.get('message', '请从「状态」菜单确认相机读取是否已结束。')),
            markdown(label), paragraph(attention['message'])]


def progress_elements(view):
    """Shared by queried status cards and periodically updated progress cards."""
    batch = view.get('batch', {})
    if not batch.get('total_files'):
        elements = [markdown('**没有待处理素材**' if batch.get('scanned') else '**尚未扫描素材**')]
        if any(stage.get('phase') == 'paused' and not view.get('controls', {}).get(name, {}).get('requested_stop')
               and view.get('controls', {}).get(name, {}).get('state') == 'running'
               for name, stage in view.get('stages', {}).items()):
            elements.append(note('已收到继续指令，等待队列更新。'))
        return elements
    label = '本轮素材' if batch.get('scanned') else '已记录素材 · 尚未重新扫描相机'
    elements = [markdown(f"**{label}**　{batch['total_files']} 个文件 · {_size(batch.get('total_bytes'))}")]
    for name, label, count, percent, verb in (
            ('import', '💾 导入到缓存', 'imported_files', 'import_percent', '已完整导入'),
            ('upload', '📁 归档到存储', 'completed_files', 'percent', '已正式归档')):
        bar, value = progress_bar(batch.get(percent))
        done, total = batch.get(count, 0), batch['total_files']
        stage = view.get('stages', {}).get(name, {})
        control = view.get('controls', {}).get(name, {})
        stopped = control.get('requested_stop') or control.get('state') in ('paused', 'pausing') or not view.get('running')
        state_label = _stage_label(view, name)
        if done == total:
            state_label = '导入完成' if name == 'import' else '归档完成'
        elif value == 100 and not stopped:
            state_label = '等待导入核验' if name == 'import' else '正在确认归档'
        elements.append(markdown(f"**{label} · {value:.1f}%**\n{bar}\n{verb} **{done} / {total}** 个 · {state_label}"))
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
    if batch.get('percent', 0) >= 100 and batch.get('completed_files', 0) < batch['total_files']:
        elements.append(note('上传字节已到达，仍需大小核对与正式改名；当前尚未全部归档。'))
    return elements


def progress_card(view, interval, keyword='', updated_at=None, *, notifications_enabled=True):
    batch = view.get('batch', {})
    enabled = view.get('configuration', {}).get('sync_enabled', True)
    complete = bool(batch.get('total_files') and batch.get('completed_files') == batch['total_files'])
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
    if complete:
        elements.append(note('本轮更新结束；归档结果另行提醒。'))
    elif not enabled or not notifications_enabled:
        elements.append(note('自动进度更新已停止；任务和完整缓存保留。'))
    else:
        elements.append(note(f'每 {_duration(interval)} 更新此卡片；完成和失败单独提醒。底部「状态」可随时查询。'))
    elements.append(note('更新于 ' + format_time(updated_at or view.get('observed_at'))))
    return card(title, elements, color, shared=True)


def event_card(kind, title, text, view, *, keyword='', updated_at=None, locations=None):
    icon, color = {'completed': ('✅', 'green'), 'imported': ('📷', 'green'),
                   'failed': ('⚠️', 'orange'), 'test': ('✅', 'blue')}.get(kind, ('ℹ️', 'blue'))
    title = title.removeprefix('素材导入助手 · ')
    elements = [note(keyword)] if keyword else []
    if kind == 'completed':
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
    else:
        elements += [markdown('**通知连接成功**'), paragraph(text)]
    elements.append(note('通知时间 ' + format_time(updated_at or view.get('observed_at')) + ' · 底部「状态」查看最新进展'))
    return card(icon + ' ' + title, elements, color)


def status_content(view):
    batch, health, disk = view["batch"], view["health"], view["disk"]
    enabled, running = view["configuration"]["sync_enabled"], view["running"]
    stopping = any(c["state"] == "pausing" for c in view["controls"].values())
    complete = bool(batch["total_files"] and batch["completed_files"] == batch["total_files"])
    if not running:
        title, color, summary = "传输状态 · 服务待检查", "orange", "传输服务未运行或心跳已过期，请在本机检查 Docker。"
    elif stopping:
        title, color, summary = "传输状态 · 正在停止", "orange", "正在结束当前操作并清理半文件；完整缓存保留。"
    elif not enabled:
        title, color, summary = "传输状态 · 同步已关闭", "grey", "任务与完整缓存已保留。可从底部「同步」菜单开启。"
    elif complete:
        title, color, summary = "传输状态 · 本轮已归档", "green", "本轮素材已全部正式归档，等待新素材。"
    else:
        title, color, summary = "传输状态 · 同步已开启", "blue", "相机导入与目的地上传分别运行，等待原因见下方。"
    attention = view.get('attention', {'required': False, 'message': '暂无需要处理的问题。'})
    if attention['required']:
        title, color = '传输状态 · 需要处理', 'orange'
    elif enabled and view['unplug']['ready'] and not complete:
        title, color = '传输状态 · 相机可以带走', 'green'
    title = ('⚠️ ' if color == 'orange' else '✅ ' if color == 'green' else '⏸ ' if color == 'grey' else '🔄 ') + title
    elements = headline_elements(view)
    if enabled or stopping:
        elements.append(note('正在结束当前操作' if stopping else '同步已开启 · 持续监听新素材'))
    elements += [{'tag': 'hr'}, markdown("**连接状态**"), paragraph("  ·  ".join(
        label + " " + _connection(health[key], key == "cache")
        for key, label in (("source", "相机"), ("cache", "缓存盘"), ("destination", "目的地"))))]
    checked = view.get('preflight')
    if checked:
        labels = {'ok': '通过', 'waiting': '有等待项', 'error': '有待处理项', 'checking': '检查中'}
        elements.append(note('最近启动自检：' + labels.get(checked['state'], '未知') + ' · ' +
                             format_time(checked.get('finished_at') or checked.get('started_at')) + '；当前连接以上方为准'))
        for check in checked.get('checks', {}).values():
            if check['state'] != 'ok':
                elements.append(paragraph(check['message']))
    if disk.get("free_bytes") is not None:
        used = _size(disk["cache_used_bytes"]) if disk.get("cache_used_bytes") is not None else "待更新"
        quota = _size(disk["quota_bytes"]) if disk.get("quota_bytes") else "不限额"
        elements.append(note(f"缓存占用 {used} / {quota} · 磁盘可用 {_size(disk['free_bytes'])}"))
        if disk.get('unverified_cache_files'):
            elements.append(paragraph(f"其中 {disk['unverified_cache_files']} 个缓存（{_size(disk['unverified_cache_bytes'])}）待核验；未归档文件核验前不计为导入完成，已归档数量保留。"))
    elements.append({"tag": "hr"})
    elements += progress_elements(view)
    cleanup = view["totals"]
    pending = [f"{label} {count}" for label, count in (
        ("相机原片", cleanup["pending_cleanup"]), ("完整缓存", cleanup["cache_cleanup_pending"]),
        ("半文件", len(view["temporary_cleanup"]))) if count]
    if pending:
        elements.append(paragraph("待清理：" + " · ".join(pending)))
    errors = []
    if not health["cache"].get("available") and disk.get("reason"):
        errors.append(str(disk["reason"]) + "；" + str(disk.get("action") or "请检查缓存盘连接和挂载"))
    for item in view["issues"][:3]:
        errors.append(item["path"] + "：" + str(item.get("cache_reason") if item.get('cache_state') == 'unverified' else
                      item.get("error") or item.get("source_cleanup_error") or item.get("cache_cleanup_error")))
    for item in view["temporary_cleanup"][:2]:
        if item.get("error"):
            errors.append("半文件清理：" + str(item["error"]))
    if errors:
        elements += [{"tag": "hr"}, markdown("**需要留意**")]
        elements.extend(paragraph(error[:360]) for error in errors)
    recent = view.get('recent_result')
    elements += [{'tag': 'hr'}, markdown('**最近一次结果**')]
    if recent:
        label = '归档完成' if recent['complete'] else '尚未完成 · 没有已完成批次'
        elements.append(paragraph(label + (f"\n已归档 {recent['archived_files']} 个 · 异常 {recent['failed_files']} 个" if recent['complete'] else
                                          f"\n待归档 {recent['pending_files']} 个 · 异常 {recent['failed_files']} 个")))
        if recent.get('finished_at'):
            elements.append(note('完成时间 ' + format_time(recent['finished_at'])))
        if recent['complete'] or recent['source_deleted'] or recent['cache_cleanup_pending']:
            elements.append(note(f"原片已清理或不在相机 {recent['source_deleted']} 个 · 已归档缓存待清理 {recent['cache_cleanup_pending']} 个"))
        for item in recent['archives'][:2]:
            elements.append(paragraph('已归档 ' + item['source'].rsplit('/', 1)[-1] + '\n' + item['location']))
        if len(recent['archives']) > 2:
            elements.append(note(f"另有 {len(recent['archives']) - 2} 个已归档文件，保存在下方目录。"))
    else:
        elements.append(paragraph('尚无任务结果。未扫描不代表相机没有素材。'))
    archive = view.get('archive', {})
    actual = archive.get('locations', [])
    elements.append(markdown('**实际归档目录**' if actual else '**归档目的地 · 尚无已归档文件**'))
    elements.extend(paragraph(location) for location in (actual or archive.get('planned_locations', []))[:3])
    source_policy = "完整导入后删除原片" if view["policy"]["delete_source"] else "保留相机原片"
    elements.append(note(source_policy + " · 正式归档后清理对应缓存"))
    updated = format_time(view['observed_at'])
    elements.append(note("查询快照 · " + updated + " · 底部「状态」可刷新"))
    return title, color, elements


def format_time(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.strftime('%m-%d %H:%M ') + (stamp.tzname() or '时区未知')
    except (ValueError, TypeError):
        return '时间未知'
