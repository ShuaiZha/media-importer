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
    title = '正在执行相机操作' if waiting else '操作结果待核验' if observed.get('uncertain') else '相机页面'
    elements = [paragraph((device.get('model_name') or '尚未连接相机') + ' · ' +
                         {'usb': 'USB', 'wifi': 'Wi-Fi'}.get(device.get('transport'), '未连接'))]
    if waiting:
        elements.append(paragraph('正在核验操作结果'))
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
        if observed['recording'] and observed.get('recording_seconds') is not None:
            from .notifications import _duration
            elements.append(paragraph('已录制 ' + _duration(observed['recording_seconds'])))
        if observed.get('overheating'):
            elements.append(markdown('**相机报告过热，请停止拍摄并检查散热。**'))
        if receipt is None: elements.append(note('更新 ' + format_time(observed['observed_at'])))
    elif not waiting:
        elements.append(note('拍摄状态未知或已过期'))
    if support['reason']:
        elements.append(paragraph(support['reason']))
    if not fresh(connection) and not waiting:
        link = store.get_meta('source_link', {})
        if link.get('reason'):
            elements.append(paragraph('连接：' + link['reason']))
    if not waiting:
        from .camera_web import button as workspace_button
        workspace = workspace_button(bot, store)
        if workspace: elements.append(bot.buttons(workspace))
        controls = []
        if current and observed.get('recording') and 'stop_recording' in commands:
            controls.append(button(bot, '停止录像', 'stop_recording', connection['id']))
        from .live_preview import KEY, ACTIVE
        preview = store.get_meta(KEY, {})
        if preview.get('state') in ACTIVE or preview.get('cleanup_pending'):
            controls.append(bot.button('结束预览', {'live_preview': {'version': 1, 'action': 'stop', 'id': preview['id']}}))
        if controls: elements.append(bot.buttons(*controls))
        if not fresh(connection):
            elements.append(bot.buttons(button(bot, '连接相机', 'connect'), button(bot, '刷新', 'panel')))
        shoot = store.get_meta('capture_session', {})
        if shoot.get('owner') == bot.scope and shoot.get('serial') == device.get('serial_number'):
            if shoot.get('state') == 'ready':
                elements += [bot.buttons(button(bot, '导入本次素材', 'import_shoot', id=shoot['id']))]
            elif shoot.get('state') in ('awaiting_media', 'uncertain', 'empty', 'not_started'):
                elements.append(note(shoot.get('reason') or '正在核验拍摄结果'))
    return card(title, elements, 'orange' if observed.get('uncertain') or observed.get('error') else 'blue', shared=True)


def connection_result(bot, store, job, payload):
    """One bounded connection intent; never change backup or camera operations."""
    item = bot.settings.read()
    if item.get('error'):
        raise ConfigError(item['error'])
    mode = item['config']['backup']['source_transport']
    state = store.get_meta('source_connection', {})
    device = state.get('device', {})
    if fresh(state) and state.get('state') in ('pending', 'declined'):
        return bot.source_panel(item['config']), False
    if fresh(state) and state.get('state') == 'approved' and device.get('http_ready', True):
        return card('相机已连接', [paragraph((device.get('model_name') or '相机') + ' · ' +
            {'usb': 'USB', 'wifi': 'Wi-Fi'}.get(device.get('transport'), '未知')),
            bot.buttons(button(bot, '相机页面', 'panel'))], 'green', shared=True), False
    if mode == 'usb':
        return card('等待 USB 连接', [paragraph('请将已开机的相机通过 USB 连接到本机。'),
            bot.buttons(button(bot, '检查连接', 'connect'))], shared=True), False
    from .connections import wireless_supported
    if not wireless_supported():
        raise ConfigError('无线自动连接仅支持 Mac 原生版；此环境请使用 USB')
    if payload.get('connect_mode', mode) != mode:
        return card('连接已停止', [paragraph('连接方式已更改，请重新连接。')], 'grey', shared=True), False
    if 'connect_until' not in payload:
        request = store.get_meta('source_connect_request', {})
        if request.get('until', 0) <= time.time():
            request = {'id': job['id'], 'until': time.time() + 150}
            store.set_meta('source_connect_request', request)
        payload.update(connect_until=request['until'], connect_mode=mode)
    link = store.get_meta('source_link', {})
    if time.time() >= payload['connect_until']:
        return card('相机连接超时', [paragraph('请确认相机已开机、无线连接已开启。'),
            bot.buttons(button(bot, '重新连接', 'connect'))], 'orange', shared=True), False
    if link.get('phase') in ('unsupported', 'invalid_config'):
        raise ConfigError(link.get('reason') or '当前配置不支持连接相机')
    text = '暂未连接，正在重试。' if link.get('phase') == 'waiting_retry' else '请保持相机开机并开启无线连接。'
    return card('正在连接相机', [paragraph(text)], shared=True), True


def process_job(bot, job, payload):
    from .capture import enqueue, get
    action = payload['capture']
    waiting = watching = False
    store = Store(bot.directory)
    try:
        kind = action['action']
        if job['state'] == 'queued' and kind != 'connect':
            store.set_meta('capture_card_watch', {'job': job['id'], 'until': time.time() + 30})
        if kind == 'connect':
            if job['state'] == 'queued':
                with bot.db() as db:
                    other = db.execute("""SELECT 1 FROM bot_jobs WHERE scope=? AND id!=?
                        AND state='waiting' AND json_extract(payload,'$.capture.action')='connect' LIMIT 1""",
                        (bot.scope, job['id'])).fetchone()
                    if other:
                        db.execute("UPDATE bot_jobs SET state='done' WHERE id=?", (job['id'],))
                        return True
            response, waiting = connection_result(bot, store, job, payload)
            fingerprint = json.dumps(response, ensure_ascii=False, sort_keys=True)
            if waiting and payload.get('connect_observation') == fingerprint:
                with bot.db() as db:
                    db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + 3, job['id']))
                return False
            payload['connect_observation'] = fingerprint
            with bot.db() as db:
                db.execute('UPDATE bot_jobs SET payload=? WHERE id=?', (json.dumps(payload), job['id']))
        elif kind in ('panel', 'settings', 'parameter'):
            response = panel(bot, store)
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
