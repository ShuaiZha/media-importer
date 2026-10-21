"""Compact owner-only camera control, with actual-state receipts."""
import json
import re
import time
import uuid
from datetime import datetime, timezone

from .bot_cards import card, paragraph, note, markdown, format_time
from .capture import COMMANDS, LABELS, capabilities, allowed_commands
from .model import ConfigError
from .source_connection import fresh
from .store import Store


def supported(action):
    if not isinstance(action, dict) or action.get('version') != 1:
        return False
    if action.get('action') in ('panel', 'connect'):
        return set(action) == {'version', 'action'}
    return (action.get('action') in COMMANDS and set(action) == {'version', 'action', 'session_id', 'nonce'} and
            all(isinstance(action[k], str) and re.fullmatch('[0-9a-f]{32}', action[k]) for k in ('session_id', 'nonce')))


def button(bot, label, action, session=None):
    value = {'version': 1, 'action': action}
    if session:
        value.update(session_id=session, nonce=uuid.uuid4().hex)
    return bot.button(label, {'capture': value})


def panel(bot, store, receipt=None, *, waiting=False):
    connection = store.get_meta('source_connection', {})
    device = connection.get('device', {})
    support = capabilities(device)
    commands = allowed_commands(store, connection)
    from .capture import observation
    observed = receipt if receipt is not None else observation(store, device)
    try:
        age = time.time() - datetime.fromisoformat(observed['observed_at']).timestamp()
        current = 0 <= age < 45 and not observed.get('stale')
    except (KeyError, TypeError, ValueError):
        current = False
    title = '正在执行相机操作' if waiting else '操作结果待核验' if observed.get('uncertain') else '相机控制'
    elements = [paragraph((device.get('model_name') or '尚未连接相机') + ' · ' +
                         {'usb': 'USB', 'wifi': 'Wi-Fi'}.get(device.get('transport'), '未连接'))]
    if waiting:
        elements.append(paragraph('正在核验相机并执行操作，结果将更新到本卡片。完整缓存按同步设置归档。'))
    elif observed.get('error') or observed.get('reason'):
        elements.append(paragraph(observed.get('error') or observed['reason']))
    elif receipt is not None:
        elements.append(markdown('**' + LABELS.get(receipt.get('command'), '相机状态已更新') + '**'))
    if current and 'recording' in observed:
        elements.append(paragraph('拍摄：' + ('录像中' if observed['recording'] else '未录像') +
            ' · 模式：' + {'video': '视频', 'photo': '照片', 'other': '其他'}.get(observed.get('mode'), '未知')))
        elements.append(note('观测于 ' + format_time(observed['observed_at'])))
    elif not waiting:
        elements.append(note('当前拍摄状态尚未读取或已过期；操作前会重新核验。'))
    if support['reason']:
        elements.append(paragraph(support['reason']))
    if not fresh(connection) and not waiting:
        link = store.get_meta('source_link', {})
        if link.get('reason'):
            elements.append(paragraph('连接：' + link['reason']))
    if not waiting:
        if commands:
            from .camera_coordinator import preview_active
            previewing = preview_active(store)
            recording = current and observed.get('recording')
            labels = [('stop_recording', '停止录像')] if recording else (
                [('preview_record', '结束预览并录像')] if previewing else
                [('photo', '拍照')] if observed.get('mode') == 'photo' else [('start_recording', '开始录像')])
            controls = []
            for action, label in labels:
                if ('start_recording' if action == 'preview_record' else action) in commands:
                    primary = button(bot, label, action, connection['id']); primary['type'] = 'primary'
                    controls.append(primary)
            controls.append(button(bot, '刷新状态', 'state', connection['id']))
            if not current and 'stop_recording' in commands:
                controls.append(button(bot, '停止录像', 'stop_recording', connection['id']))
            elements.append(bot.buttons(*controls))
            if 'mode_video' in commands and not recording and not previewing:
                elements.append(bot.buttons(*(button(bot, label, action, connection['id']) for action, label in
                    [('mode_video', '视频模式'), ('mode_photo', '照片模式')] if action in commands)))
        else:
            elements.append(bot.buttons(button(bot, '连接无线相机', 'connect'), button(bot, '刷新连接', 'panel')))
        elements.append(note('拍摄优先于导入；完整缓存按同步设置归档。停止同步不会停止录像。'))
    return card(title, elements, 'orange' if observed.get('uncertain') or observed.get('error') else 'blue', shared=True)


def process_job(bot, job, payload):
    from .capture import enqueue, get
    action = payload['capture']
    waiting = False
    store = Store(bot.directory)
    try:
        kind = action['action']
        if kind == 'connect':
            from .connections import wireless_supported
            if not wireless_supported():
                raise ConfigError('无线自动连接仅支持 Mac 原生版')
            if bot.settings.read()['config']['backup']['source_transport'] == 'usb':
                raise ConfigError('当前配置为仅 USB；请先到「相机连接」改为自动或无线')
            store.set_meta('source_connect_request', {'until': time.time() + 150})
            response = card('正在连接无线相机', [paragraph('后台将核验蓝牙身份并连接相机热点，同步开关保持不变。'),
                bot.buttons(button(bot, '查看连接结果', 'panel'))], shared=True)
        elif kind == 'panel':
            response = panel(bot, store)
        else:
            identifier = payload.get('capture_request')
            if not identifier:
                op = enqueue(store, bot.scope, action['nonce'], kind, action['session_id'])
                identifier = payload['capture_request'] = op['id']
                with bot.db() as db:
                    db.execute('UPDATE bot_jobs SET payload=? WHERE id=?', (json.dumps(payload), job['id']))
            op = get(store.db, identifier, bot.scope)
            if not op:
                raise ConfigError('相机操作已过期，请重新打开控制卡')
            waiting = op['state'] in ('queued', 'running')
            result = {**json.loads(op['result']), 'command': op['command']}
            result.setdefault('observed_at', datetime.fromtimestamp(op['updated'], timezone.utc).isoformat())
            fingerprint = [op['state'], op['updated']]
            if job['state'] == 'waiting' and payload.get('capture_observation') == fingerprint:
                with bot.db() as db:
                    db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + 1, job['id']))
                return False
            payload['capture_observation'] = fingerprint
            response = panel(bot, store, result, waiting=waiting)
    except ConfigError as exc:
        response = card('相机操作未执行', [paragraph(str(exc)),
            bot.buttons(button(bot, '刷新相机控制', 'panel'))], 'orange', shared=True)
    finally:
        store.close()
    delivered = bot.reply(job, payload, response, waiting)
    with bot.db() as db:
        db.execute('UPDATE bot_jobs SET response=?,payload=? WHERE id=?', (json.dumps(response), json.dumps(payload), job['id']))
    return delivered
