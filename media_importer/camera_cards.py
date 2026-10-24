"""Feishu entry points and receipts for one-shot camera operations."""
import json
import re
import time

from .bot_cards import card, markdown, note, paragraph, format_time
from .camera_operations import MODES, ACTIVE
from .model import ConfigError
from .notifications import _size
from .store import Store


def supported(action):
    if not isinstance(action, dict) or action.get('version') != 1:
        return False
    kind = action.get('action')
    if kind == 'inspect':
        return set(action) == {'version', 'action'}
    if kind in ('select', 'preview'):
        return set(action) == {'version', 'action', 'mode'} and action['mode'] in MODES
    if kind in ('confirm', 'cancel', 'result', 'details', 'device_details', 'verify'):
        return (set(action) <= {'version', 'action', 'id', 'page'} and
                isinstance(action.get('id'), str) and re.fullmatch('[0-9a-f]{32}', action['id']) is not None
                and type(action.get('page', 0)) is int and 0 <= action.get('page', 0) <= 100000)
    return False


def button(bot, label, action, **values):
    return bot.button(label, {'camera': {'version': 1, 'action': action, **values}},
                      danger=action == 'confirm')


def selection(bot, mode='archived'):
    other = 'imported' if mode == 'archived' else 'archived'
    text = ('仅清理已正式归档、且通过文件大小与身份核验的素材。' if mode == 'archived' else
            '清理已有完整本地缓存或正式归档的素材；未归档文件的本地缓存可能是唯一副本。')
    primary = button(bot, '预览清理范围', 'preview', mode=mode)
    primary['type'] = 'primary'
    return card('批量清理原片', [markdown('**清理范围：' + MODES[mode] + '**'), paragraph(text),
        bot.buttons(primary, button(bot, '改为' + MODES[other], 'select', mode=other))], shared=True)


def _source(identity, *, details=False, stale=False):
    t = identity.get('telemetry', {})
    valid = t.get('available') and not t.get('stale') and not stale
    battery = f"{t['battery_percent']}%" if valid and t.get('battery_percent') is not None else '未知'
    charge = ('充电中' if t['charging'] else '未充电') if valid and t.get('charging') is not None else '未知'
    alarm = ('未知' if not valid else '过热' if t.get('overheating') else '过冷' if t.get('cold') else
             '未报告异常' if valid and t.get('overheating') is False and t.get('cold') is False else '未知')
    activity = ('状态已过期' if stale else '未知' if not valid else '录像中' if t.get('recording') else '忙碌' if t.get('busy') else
                '就绪' if valid and t.get('ready') and t.get('recording') is False and t.get('busy') is False else '未知')
    storage = {'ready': '正常', 'full': '已满', 'missing': '未插入', 'busy': '忙碌',
               'format_error': '格式异常', 'error': '异常'}.get(t.get('storage_status'), '未知') if valid else '未知'
    remaining = _size(t['storage_remaining_bytes']) if valid and t.get('storage_remaining_bytes') is not None else '未知'
    if details:
        return [paragraph('序列号：' + (identity.get('serial_number') or '未知') +
                          '\n固件：' + (identity.get('firmware_version') or '未知')),
                paragraph(f'充电状态 {charge} · 温度告警 {alarm}\n存储卡 {storage}')]
    elements = [paragraph((identity.get('model_name') or '未知相机') + ' · ' +
                          {'usb': 'USB', 'wifi': 'Wi-Fi'}.get(identity.get('transport'), '连接方式未知')),
                paragraph(f'{activity} · 电量 {battery}\n存储卡剩余 {remaining}')]
    if valid and (t.get('overheating') or t.get('cold')): elements.append(paragraph('温度告警：' + alarm))
    if valid and storage not in ('正常', '未知'): elements.append(paragraph('存储卡：' + storage))
    return elements


def overview(bot, store):
    """A query reads current facts; detailed source scans remain explicit."""
    from .camera_coordinator import state
    camera = state(store)
    device = store.get_meta('source_connection', {}).get('device', {})
    observed = camera['observed']
    battery = str(observed['battery_percent']) + '%' if observed.get('battery_percent') is not None else '未知'
    remaining = _size(observed['storage_remaining_bytes']) if observed.get('storage_remaining_bytes') is not None else '未知'
    elements = [paragraph((device.get('model_name') or '尚未连接相机') + ' · ' +
                          {'usb': 'USB', 'wifi': 'Wi-Fi'}.get(device.get('transport'), '未知连接')),
                paragraph(camera['connection']['label'] + ' · ' + camera['activity']['label']),
                paragraph('电量 ' + battery + ' · 存储卡剩余 ' + remaining)]
    if observed.get('overheating'): elements.append(paragraph('相机报告过热'))
    if observed.get('observed_at'): elements.append(note('更新 ' + format_time(observed['observed_at'])))
    elements.append(bot.buttons(bot.button('刷新', {'panel': 'camera_status'}),
                               button(bot, '设备详情', 'inspect')))
    return card('相机状态', elements, 'orange' if camera['activity']['state'] == 'recovering' else 'blue', shared=True)


def render(bot, op, page=None, *, device_details=False):
    result = json.loads(op['result'] or '{}')
    plan = json.loads(op['plan'] or '{"groups":[],"retained":[]}')
    identifier = op['id']
    from .message_facts import cleanup_scope, cleanup_scope_text, cleanup_result_counts
    scope = cleanup_scope(plan)
    if op['state'] in ACTIVE:
        title = ('正在停止相机操作' if op['cancelled'] else
                 '正在清理原片' if op['state'] == 'deleting' else
                 '正在核验相机' if op['state'] in ('running', 'verifying') else '相机操作已排队')
        detail = ('正在核验停止结果' if op['cancelled'] or op['state'] == 'verifying' else
                  '正在核验副本' if op['state'] == 'running' else '')
        return card(title, [paragraph(detail),
                           bot.buttons(button(bot, '停止本次操作', 'cancel', id=identifier))], shared=True)
    if page is not None:
        entries = ([{'path': a['path'], 'reason': '符合清理条件'} for g in plan['groups'] for a in g['members']] +
                   [{'path': path, 'reason': group['reason']} for group in plan['retained'] for path in group['paths']])
        if op['state'] == 'done' and result.get('details'):
            labels = {'deleted': '已删除', 'absent': '原片已不存在', 'retained': '保留', 'unknown': '待核验'}
            entries = [{'path': item['path'], 'reason': labels[item['state']] +
                        ('：' + item['reason'] if item.get('reason') else '')} for item in result['details']]
        pages = max(1, (len(entries) + 11) // 12)
        page = min(page, pages - 1)
        elements = [paragraph('\n\n'.join(item['path'] + '\n' + item['reason']
                                         for item in entries[page * 12:(page + 1) * 12]) or '没有文件')]
        actions = []
        if page:
            actions.append(button(bot, '上一页', 'details', id=identifier, page=page - 1))
        if page + 1 < pages:
            actions.append(button(bot, '下一页', 'details', id=identifier, page=page + 1))
        actions.append(button(bot, '返回结果', 'result', id=identifier))
        return card(f'本次文件清单 · {page + 1}/{pages}', elements + [bot.buttons(*actions)], shared=True)
    if op['kind'] == 'inspect':
        from .status import age
        stamp = result.get('observed_at') or result.get('identity', {}).get('checked_at')
        stale = age(stamp) > 45
        elements = _source(result.get('identity', {}), details=device_details, stale=stale) if result.get('identity') else []
        if device_details and result.get('scanned'):
            labels = {'video': '视频', 'photo': '照片', 'preview': '关联辅助文件', 'other': '其他文件'}
            elements.append(paragraph('\n'.join(f"{labels[k]}：{v['files']} 个 · {_size(v['bytes'])}"
                                                   for k, v in result['counts'].items() if v['files'])))
        if result.get('error') or result.get('reason'):
            elements.append(paragraph(result.get('error') or result['reason']))
        elements.append(note('更新 ' + format_time(stamp)))
        if stale: elements.append(note('状态已过期'))
        elements.append(bot.buttons(button(bot, '刷新', 'inspect'),
            button(bot, '返回摘要' if device_details else '设备详情', 'result' if device_details else 'device_details', id=identifier)))
        return card('设备详情' if device_details else '相机状态', elements,
                    'orange' if result.get('error') else 'blue', shared=True)
    if op['state'] == 'ready' and not op['cancelled']:
        elements = [markdown('**清理范围：' + MODES[op['mode']] + '**'),
                    paragraph(cleanup_scope_text(scope['primary_files'], scope['auxiliary_files']) + '\n'
                              f"共 {result['eligible_files']} 个文件\n"
                              f"预计释放 {_size(result['eligible_bytes'])}"),
                    paragraph('相机：' + (result.get('identity', {}).get('model_name') or '未知') +
                              ' · ' + (result.get('identity', {}).get('serial_number') or '未知'))]
        if result.get('pending_archive'):
            elements.append(markdown(f"**其中 {result['pending_archive']} 个文件尚未归档，本地缓存可能是唯一副本。**"))
        retained = sum(g['files'] for g in plan['retained'])
        if retained:
            elements.append(paragraph(f'另有 {retained} 个文件保留；原因见文件清单。'))
        elements.append(note('确认有效期 5 分钟'))
        actions = [button(bot, '查看文件清单', 'details', id=identifier)]
        if result['eligible_files']:
            actions.append(button(bot, '确认清理原片', 'confirm', id=identifier))
        actions.append(button(bot, '取消', 'cancel', id=identifier))
        return card('原片清理预览', elements + [bot.buttons(*actions)], 'orange', shared=True)
    elements = []
    if 'deleted' in result:
        counts = cleanup_result_counts(plan, result)
        elements.append(markdown(f"**已删除 {result['deleted']} 个文件 · 释放 {_size(result['released_bytes'])}**"))
        if result['deleted'] and counts.get('deleted_primary', 0) + counts.get('deleted_auxiliary', 0) == result['deleted']:
            elements.append(paragraph(cleanup_scope_text(counts.get('deleted_primary', 0), counts.get('deleted_auxiliary', 0))))
        remaining = ' · '.join(f"{label} {result[key]} 个" for key, label in [('absent', '原片未找到'), ('retained', '保留'), ('unknown', '待核验')] if result[key])
        if remaining: elements.append(paragraph(remaining))
    if result.get('error') or result.get('reason'):
        elements.append(paragraph(result.get('error') or result['reason']))
    if op['cancelled'] and 'deleted' not in result:
        elements.append(paragraph('本次操作已取消，未执行删除。'))
    actions = [button(bot, '重新预览', 'preview', mode=op['mode'])]
    if result.get('unknown'):
        actions.insert(0, button(bot, '复查删除结果', 'verify', id=identifier))
    if result.get('details'):
        actions.insert(0, button(bot, '查看明细', 'details', id=identifier))
    attention = result.get('error') or result.get('retained') or result.get('unknown')
    return card('原片清理已结束' if 'deleted' in result else '本次清理未执行',
                elements + [bot.buttons(*actions)], 'orange' if attention else 'grey' if op['cancelled'] else 'green', shared=True)


def process_job(bot, job, payload):
    from .camera_operations import enqueue, confirm, cancel, get, verify
    from .camera_coordinator import operation_reason
    action = payload['camera']
    waiting = False
    store = Store(bot.directory)
    try:
        kind = action['action']
        if kind == 'select':
            response = selection(bot, action['mode'])
        elif kind == 'inspect' and operation_reason(store, 'browse'):
            response = overview(bot, store)
        else:
            identifier = payload.get('camera_request')
            if not identifier:
                if kind in ('inspect', 'preview'):
                    op = enqueue(store, bot.settings, job['id'], bot.scope, kind, action.get('mode', 'archived'))
                elif kind == 'confirm':
                    op = confirm(store, bot.settings, action['id'], bot.scope)
                elif kind == 'verify':
                    op = verify(store, action['id'], bot.scope)
                else:
                    op = get(store.db, action['id'], bot.scope)
                    if not op:
                        raise ConfigError('本次相机操作已过期，请重新打开菜单')
                    if kind == 'cancel':
                        with store.db:
                            cancel(store.db, op['id'], bot.scope)
                        op = get(store.db, op['id'], bot.scope)
                identifier = op['id']
                payload['camera_request'] = identifier
                # Save request identity before sending, so retry cannot recreate work.
                with bot.db() as db:
                    db.execute('UPDATE bot_jobs SET payload=? WHERE id=?', (json.dumps(payload), job['id']))
            op = get(store.db, identifier, bot.scope)
            if not op:
                raise ConfigError('本次相机操作已过期，请重新打开菜单')
            waiting = op['state'] in ACTIVE
            observation = [op['state'], op['updated'], op['cancelled']]
            if job['state'] == 'waiting' and payload.get('camera_observation') == observation:
                with bot.db() as db:
                    db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + 1, job['id']))
                return False
            response = render(bot, op, action.get('page', 0) if kind == 'details' else None, device_details=kind == 'device_details')
            payload['camera_observation'] = observation
    except ConfigError as exc:
        response = card('相机操作未执行', [paragraph(str(exc))], 'orange', shared=True)
    finally:
        store.close()
    # Save the observation only after successful delivery. A failed PATCH must retry.
    delivered = bot.reply(job, payload, response, waiting)
    with bot.db() as db:
        db.execute('UPDATE bot_jobs SET response=?,payload=? WHERE id=?',
                   (json.dumps(response), json.dumps(payload), job['id']))
    return delivered
