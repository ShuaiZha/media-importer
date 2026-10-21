"""Compact owner-only camera control, with actual-state receipts."""
import json
import re
import time
import uuid
from datetime import datetime, timezone

from .bot_cards import card, paragraph, note, markdown, format_time
from .capture import COMMANDS, LABELS, capabilities, allowed_commands, PREVIEW_COMMANDS
from .model import ConfigError
from .source_connection import fresh
from .store import Store


def supported(action):
    if not isinstance(action, dict) or action.get('version') != 1:
        return False
    if action.get('action') in ('panel', 'connect', 'settings'):
        return set(action) == {'version', 'action'}
    if action.get('action') == 'import_shoot':
        return set(action) == {'version', 'action', 'id'} and isinstance(action['id'], str) and bool(re.fullmatch('[0-9a-f]{32}', action['id']))
    kind = action.get('action')
    extra = {'key', 'expected_preset'} if kind == 'apply_parameter' else {'key'} if kind == 'parameter' else set()
    if 'key' in extra and (not isinstance(action.get('key'), str) or not re.fullmatch('[a-z_]{1,40}', action['key'])):
        return False
    if 'expected_preset' in extra and (type(action.get('expected_preset')) is not int or not 0 <= action['expected_preset'] <= 0xffffffff):
        return False
    return (kind in COMMANDS | {'apply_preset', 'apply_parameter', 'parameter'} and set(action) == {'version', 'action', 'session_id', 'nonce'} | extra and
            all(isinstance(action[k], str) and re.fullmatch('[0-9a-f]{32}', action[k]) for k in ('session_id', 'nonce')))


def button(bot, label, action, session=None, **extra):
    value = {'version': 1, 'action': action, **extra}
    if session:
        value.update(session_id=session, nonce=uuid.uuid4().hex)
    return bot.button(label, {'capture': value})


def panel(bot, store, receipt=None, *, waiting=False):
    connection = store.get_meta('source_connection', {})
    device = connection.get('device', {})
    support = capabilities(device)
    commands = allowed_commands(store, connection)
    from .capture import observation
    observed = observation(store, device)
    if receipt is not None:
        observed = {**receipt, **observed} if not observed.get('stale') else receipt
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
        label = LABELS.get(receipt.get('command'), '相机状态已更新')
        if (receipt.get('command') in ('start_recording', 'start_timelapse', 'preview_record', 'preview_timelapse')
                and current and observed.get('recording') is False):
            label = '拍摄已结束'
        if receipt.get('parameter_label'):
            label += '：' + receipt['parameter_label'] + ' → ' + receipt.get('value_label', '未知')
        elements.append(markdown('**' + label + '**'))
    if current and 'recording' in observed:
        mode = observed.get('mode')
        state_label = ('延时拍摄中' if mode == 'timelapse' else '录像中') if observed['recording'] else '待机'
        elements.append(markdown('**' + state_label + '** · ' + {'video': '视频', 'photo': '照片', 'timelapse': '延时摄影', 'other': '其他'}.get(mode, '未知模式')))
        if observed.get('specification'):
            elements.append(paragraph(observed['specification']))
        from .notifications import _size, _duration
        details = []
        if observed['recording'] and observed.get('recording_seconds') is not None:
            details.append('已录制 ' + _duration(observed['recording_seconds']))
        if observed.get('battery_percent') is not None: details.append(f"电量 {observed['battery_percent']}%")
        if observed.get('storage_remaining_bytes') is not None: details.append('卡剩余 ' + _size(observed['storage_remaining_bytes']))
        if details: elements.append(paragraph(' · '.join(details)))
        if observed.get('overheating'):
            elements.append(markdown('**相机报告过热，请停止拍摄并检查散热。**'))
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
            mode = observed.get('mode')
            action = 'photo' if mode == 'photo' else 'start_timelapse' if mode == 'timelapse' else 'start_recording'
            label = '拍照' if mode == 'photo' else '开始延时摄影' if mode == 'timelapse' else '开始录像'
            if previewing:
                action = {'photo': 'preview_photo', 'start_timelapse': 'preview_timelapse', 'start_recording': 'preview_record'}[action]
                label = '结束预览并' + ('拍照' if mode == 'photo' else '开始延时' if mode == 'timelapse' else '录像')
            labels = [('stop_recording', '停止拍摄' if mode == 'timelapse' else '停止录像')] if recording else [(action, label)]
            controls = []
            for action, label in labels:
                if PREVIEW_COMMANDS.get(action, action) in commands:
                    primary = button(bot, label, action, connection['id']); primary['type'] = 'primary'
                    controls.append(primary)
            controls.append(button(bot, '刷新状态', 'state', connection['id']))
            if not current and 'stop_recording' in commands:
                controls.append(button(bot, '停止录像', 'stop_recording', connection['id']))
            elements.append(bot.buttons(*controls))
            if recording and mode == 'video' and 'hilight' in commands:
                elements.append(bot.buttons(button(bot, '添加 HiLight', 'hilight', connection['id'])))
            if 'mode_video' in commands and not recording and not previewing:
                elements.append(bot.buttons(*(button(bot, label, action, connection['id']) for action, label in
                    [('mode_video', '视频模式'), ('mode_photo', '照片模式')] if action in commands)))
            if not recording:
                elements.append(bot.buttons(button(bot, '拍摄设置', 'settings'),
                    bot.button('查看实时画面' if previewing else '开启实时预览', {'live_preview': {'version': 1, 'action': 'panel' if previewing else 'open'}})))
        else:
            elements.append(bot.buttons(button(bot, '连接无线相机', 'connect'), button(bot, '刷新连接', 'panel')))
        elements.append(note('拍摄优先于导入；完整缓存按同步设置归档。停止同步不会停止录像。'))
        shoot = store.get_meta('capture_session', {})
        if shoot.get('owner') == bot.scope and shoot.get('serial') == device.get('serial_number'):
            if shoot.get('state') == 'ready':
                elements += [paragraph('最近拍摄已保存到相机，尚不代表已完成归档。'),
                             bot.buttons(button(bot, '导入本次素材', 'import_shoot', id=shoot['id']))]
            elif shoot.get('state') in ('awaiting_media', 'uncertain', 'empty', 'not_started'):
                elements.append(note(shoot.get('reason') or '正在核验拍摄结果'))
    return card(title, elements, 'orange' if observed.get('uncertain') or observed.get('error') else 'blue', shared=True)


def process_job(bot, job, payload):
    from .capture import enqueue, get
    action = payload['capture']
    waiting = watching = False
    store = Store(bot.directory)
    try:
        kind = action['action']
        if job['state'] == 'queued':
            store.set_meta('capture_card_watch', {'job': job['id'], 'until': time.time() + 30})
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
        elif kind == 'parameter':
            from .capture_forms import parameter_panel
            response = parameter_panel(bot, store, action)
        elif kind == 'import_shoot':
            from .capture_sessions import selection
            from .selected_cards import render
            op = selection(store, bot.settings, bot.scope, action['id'])
            response = render(bot, op)
        else:
            if kind == 'settings':
                connection = store.get_meta('source_connection', {})
                action = {**action, 'session_id': connection.get('id'), 'nonce': job['id']}
            command = {'settings': 'catalog', 'apply_preset': 'load_preset', 'apply_parameter': 'set_parameter'}.get(kind, kind)
            identifier = payload.get('capture_request')
            if not identifier:
                op = enqueue(store, bot.scope, action['nonce'], command, action['session_id'], payload.get('capture_parameters'))
                identifier = payload['capture_request'] = op['id']
                with bot.db() as db:
                    db.execute('UPDATE bot_jobs SET payload=? WHERE id=?', (json.dumps(payload), job['id']))
            op = get(store.db, identifier, bot.scope)
            if not op:
                raise ConfigError('相机操作已过期，请重新打开控制卡')
            waiting = op['state'] in ('queued', 'running')
            result = {**json.loads(op['result']), 'command': op['command']}
            result.setdefault('observed_at', datetime.fromtimestamp(op['updated'], timezone.utc).isoformat())
            from .capture import observation
            observed = observation(store, store.get_meta('source_connection', {}).get('device', {}))
            watch = store.get_meta('capture_card_watch', {})
            watching = (not waiting and command not in ('catalog', 'load_preset', 'set_parameter') and
                        watch.get('job') == job['id'] and watch.get('until', 0) > time.time() and
                        (observed.get('recording') is True or store.get_meta('capture_session', {}).get('state') in ('capturing', 'awaiting_media')))
            if (waiting or watching) and watch.get('job') == job['id']:
                store.set_meta('capture_card_watch', {'job': job['id'], 'until': time.time() + 30})
            elif not waiting and watch.get('job') == job['id']:
                store.set_meta('capture_card_watch', {})
            fingerprint = [op['state'], op['updated'], observed, store.get_meta('capture_session', {}).get('state'), watching]
            if (waiting or watching) and job['state'] == 'waiting' and payload.get('capture_observation') == fingerprint:
                with bot.db() as db:
                    db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + (10 if watching else 1), job['id']))
                return False
            payload['capture_observation'] = fingerprint
            if command == 'catalog' and not waiting and not result.get('error'):
                from .capture_forms import settings_panel
                response = settings_panel(bot, store)
            else:
                response = panel(bot, store, result, waiting=waiting)
            waiting = waiting or watching
    except ConfigError as exc:
        response = card('相机操作未执行', [paragraph(str(exc)),
            bot.buttons(button(bot, '刷新相机控制', 'panel'))], 'orange', shared=True)
    finally:
        store.close()
    delivered = bot.reply(job, payload, response, waiting)
    with bot.db() as db:
        db.execute('UPDATE bot_jobs SET response=?,payload=? WHERE id=?', (json.dumps(response), json.dumps(payload), job['id']))
        if watching:
            db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + 10, job['id']))
    return delivered
