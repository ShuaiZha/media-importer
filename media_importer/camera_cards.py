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
    if kind in ('confirm', 'cancel', 'result', 'details', 'verify'):
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
    return card('清理原片', [markdown('**清理范围：' + MODES[mode] + '**'), paragraph(text),
        note('本次操作不改变同步和自动删除开关。先核验并预览，确认后才删除；关联预览文件计入范围。'),
        bot.buttons(primary, button(bot, '改为' + MODES[other], 'select', mode=other))], shared=True)


def _source(identity):
    t = identity.get('telemetry', {})
    valid = t.get('available')
    battery = f"{t['battery_percent']}%" if valid and t.get('battery_percent') is not None else '未知'
    charge = ('充电中' if t['charging'] else '未充电') if valid and t.get('charging') is not None else '未知'
    alarm = ('过热' if t.get('overheating') else '过冷' if t.get('cold') else
             '未报告异常' if valid and t.get('overheating') is False and t.get('cold') is False else '未知')
    activity = ('录像中' if t.get('recording') else '忙碌' if t.get('busy') else
                '就绪' if valid and t.get('ready') and t.get('recording') is False and t.get('busy') is False else '未知')
    storage = {'ready': '正常', 'full': '已满', 'missing': '未插入', 'busy': '忙碌',
               'format_error': '格式异常', 'error': '异常'}.get(t.get('storage_status'), '未知') if valid else '未知'
    remaining = _size(t['storage_remaining_bytes']) if valid and t.get('storage_remaining_bytes') is not None else '未知'
    return [paragraph((identity.get('model_name') or '未知相机') + '\n序列号：' + (identity.get('serial_number') or '未知') +
                      '\n固件：' + (identity.get('firmware_version') or '未知')),
            paragraph(f'电量 {battery} · 充电状态 {charge}\n设备 {activity} · 温度告警 {alarm}\n存储卡 {storage} · 剩余 {remaining}')]


def render(bot, op, page=None):
    result = json.loads(op['result'] or '{}')
    plan = json.loads(op['plan'] or '{"groups":[],"retained":[]}')
    identifier = op['id']
    if op['state'] in ACTIVE:
        title = ('正在停止相机操作' if op['cancelled'] else
                 '正在清理原片' if op['state'] == 'deleting' else
                 '正在核验相机' if op['state'] in ('running', 'verifying') else '相机操作已排队')
        detail = ('停止后将复查本次结果，未执行的原片保留。' if op['cancelled'] else
                  '将逐组核验并清理，结果会更新到本卡片。' if op['state'] == 'deleting' else
                  '正在复查已发出删除请求的结果，不会继续删除。' if op['state'] == 'verifying' else
                  '正在读取相机清单与核验副本，本阶段不删除文件。' if op['state'] == 'running' else
                  '等待当前相机导入操作结束后执行，上传按原设置继续。本卡片会自动更新。')
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
        elements = _source(result.get('identity', {})) if result.get('identity') else []
        if result.get('scanned'):
            labels = {'video': '视频', 'photo': '照片', 'preview': '关联预览文件', 'other': '其他文件'}
            elements += [markdown(f"**相机文件：{result['files']} 个 · {_size(result['bytes'])}**"),
                         paragraph('\n'.join(f"{labels[k]}：{v['files']} 个 · {_size(v['bytes'])}"
                                             for k, v in result['counts'].items()))]
        if result.get('error') or result.get('reason'):
            elements.append(paragraph(result.get('error') or result['reason']))
        elements += [note('统计按实际文件计数，包含分段视频和附属文件。'),
                     note('观测于 ' + format_time(result.get('observed_at') or result.get('identity', {}).get('checked_at'))),
                     bot.buttons(button(bot, '刷新相机状态', 'inspect'))]
        return card('相机状态' if not result.get('error') else '相机状态暂不可用', elements,
                    'orange' if result.get('error') else 'blue', shared=True)
    if op['state'] == 'ready' and not op['cancelled']:
        elements = [markdown('**清理范围：' + MODES[op['mode']] + '**'),
                    paragraph(f"符合条件 {result['eligible_groups']} 组 · {result['eligible_files']} 个文件\n"
                              f"预计释放 {_size(result['eligible_bytes'])}"),
                    paragraph('相机：' + (result.get('identity', {}).get('model_name') or '未知') +
                              ' · ' + (result.get('identity', {}).get('serial_number') or '未知'))]
        if result.get('pending_archive'):
            elements.append(markdown(f"**其中 {result['pending_archive']} 个文件尚未归档，本地缓存可能是唯一副本。**"))
        retained = sum(g['files'] for g in plan['retained'])
        if retained:
            elements.append(paragraph(f'另有 {retained} 个文件保留；原因见文件清单。'))
        elements.append(note('确认后仅处理本次清单及已确认关联的预览文件。确认有效期 5 分钟；新增或变化素材保留。'))
        actions = [button(bot, '查看文件清单', 'details', id=identifier)]
        if result['eligible_files']:
            actions.append(button(bot, '确认清理原片', 'confirm', id=identifier))
        actions.append(button(bot, '取消', 'cancel', id=identifier))
        return card('原片清理预览', elements + [bot.buttons(*actions)], 'orange', shared=True)
    elements = []
    if 'deleted' in result:
        elements += [markdown(f"**已删除 {result['deleted']} 个 · 释放 {_size(result['released_bytes'])}**"),
                     paragraph(f"原片已不存在 {result['absent']} 个 · 保留 {result['retained']} 个 · 待核验 {result['unknown']} 个")]
    if result.get('error') or result.get('reason'):
        elements.append(paragraph(result.get('error') or result['reason']))
    if op['cancelled'] and 'deleted' not in result:
        elements.append(paragraph('本次操作已取消，未执行删除。'))
    elements.append(note('同步与自动删除开关保持原设置。'))
    actions = [button(bot, '重新预览', 'preview', mode=op['mode'])]
    if result.get('unknown'):
        actions.insert(0, button(bot, '复查删除结果', 'verify', id=identifier))
    if result.get('details'):
        actions.insert(0, button(bot, '查看明细', 'details', id=identifier))
    attention = result.get('error') or result.get('retained') or result.get('unknown') or op['cancelled']
    return card('原片清理已结束' if 'deleted' in result else '本次清理未执行',
                elements + [bot.buttons(*actions)], 'orange' if attention else 'green', shared=True)


def process_job(bot, job, payload):
    from .camera_operations import enqueue, confirm, cancel, get, verify
    action = payload['camera']
    waiting = False
    store = Store(bot.directory)
    try:
        kind = action['action']
        if kind == 'select':
            response = selection(bot, action['mode'])
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
            response = render(bot, op, action.get('page', 0) if kind == 'details' else None)
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
