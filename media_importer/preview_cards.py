"""Feishu is the entry point; video uses a revocable playback-only URL."""
import json
import re
import time
from urllib.parse import urlencode

from .bot_cards import card, paragraph, note, plain, format_time
from .live_preview import KEY, ACTIVE, request, stop_request
from .model import ConfigError
from .store import Store


def supported(value):
    if not isinstance(value, dict) or value.get('version') != 1:
        return False
    if value.get('action') in ('open', 'panel'):
        return set(value) == {'version', 'action'}
    return (value.get('action') == 'stop' and set(value) == {'version', 'action', 'id'} and
            isinstance(value['id'], str) and re.fullmatch('[0-9a-f]{32}', value['id']) is not None)


def button(bot, label, action, identifier=None):
    value = {'version': 1, 'action': action}
    if identifier: value['id'] = identifier
    return bot.button(label, {'live_preview': value})


def panel(bot, state):
    phase = state.get('state', 'stopped')
    title = {'queued': '正在准备预览', 'connecting': '正在连接相机', 'starting': '正在准备画面',
             'ready': '实时预览进行中', 'stopping': '正在结束预览', 'error': '实时预览异常'}.get(phase, '实时预览已结束')
    elements = [paragraph(state.get('reason') or '仅在需要观看时启动预览。')]
    buttons = []
    if phase == 'ready' and state.get('url') and not state.get('stop_requested'):
        url = 'https://applink.feishu.cn/client/web_url/open?' + urlencode(
            {'mode': 'window', 'url': state['url'], 'width': '1000', 'height': '700'})
        buttons.append({'tag': 'button', 'text': plain('打开画面'), 'type': 'primary', 'url': url})
    if phase in ACTIVE or state.get('cleanup_pending'):
        buttons.append(button(bot, '核验并结束' if state.get('cleanup_pending') else '结束预览', 'stop', state['id']))
    else:
        buttons.append(button(bot, '开启预览', 'open'))
    buttons.append(bot.button('预览设置', {'panel': 'preview_settings'}))
    elements.append(bot.buttons(*buttons))
    elements.append(bot.buttons(bot.button('拍摄控制', {'capture': {'version': 1, 'action': 'panel'}})))
    from .camera_web import button as workspace_button
    store = Store(bot.directory)
    try:
        workspace = workspace_button(bot, store)
        if workspace: elements.append(bot.buttons(workspace))
    finally: store.close()
    if phase in ACTIVE:
        if phase == 'ready':
            elements.append(paragraph((state.get('remote_reason') or '远程入口已配置 · 观看设备需连接 Tailscale') if state.get('remote_configured')
                                      else '仅本机观看 · 外网观看需配置 Tailscale 播放入口'))
        enabled = bool((bot.settings.read().get('config') or {}).get('backup', {}).get('enabled'))
        elements.append(note('关闭播放窗口不会结束预览。\n' +
            ('本地导入与原片清理等待；完整缓存继续归档。' if enabled else '同步已关闭；预览独立运行，不处理相机原片。')))
    if state.get('observed_at'):
        elements.append(note('更新于 ' + format_time(state['observed_at'])))
    return card(title, elements, 'green' if phase == 'ready' else 'orange' if phase == 'error' else 'blue', shared=True)


def process_job(bot, job, payload):
    store = Store(bot.directory)
    waiting = False
    try:
        action = payload['live_preview']
        if not payload.get('preview_id'):
            if action['action'] == 'open':
                state = request(store, bot.settings, bot.scope, job['id'])
            elif action['action'] == 'stop':
                state = stop_request(store, bot.scope, action['id'])
            else:
                state = store.get_meta(KEY, {})
            payload['preview_id'] = state.get('id')
        state = store.get_meta(KEY, {})
        access = store.get_meta('preview_access', {})
        if payload.get('preview_id') and state.get('id') != payload['preview_id']:
            if payload.get('preview_error_alert'):
                with bot.db() as db:
                    db.execute("UPDATE bot_jobs SET state='done' WHERE id=?", (job['id'],))
                return True
            state = {'state': 'stopped', 'reason': '此预览会话已结束。'}
        if state.get('state') == 'error' and state.get('owner') == bot.scope and not payload.get('preview_error_alert'):
            # A source failure deserves one separate alert; viewer reconnects do not.
            bot.enqueue('preview:error:' + state['id'], {
                'live_preview': {'version': 1, 'action': 'panel'},
                'preview_id': state['id'], 'preview_error_alert': True})
        waiting = state.get('state') in ACTIVE
        fingerprint = [state.get('id'), state.get('state'), state.get('reason'), state.get('url'),
                       access.get('remote_state'), bool(bot.settings.read()['config']['backup']['enabled'])]
        if job['state'] == 'waiting' and payload.get('preview_observation') == fingerprint:
            with bot.db() as db:
                db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + 2, job['id']))
            return False
        payload['preview_observation'] = fingerprint
        response = panel(bot, {**state, 'remote_reason': access.get('remote_reason')})
    except ConfigError as exc:
        response = card('实时预览未开启', [paragraph(str(exc))], 'orange', shared=True)
    finally:
        store.close()
    # Persist intent before sending. A retry must not open a second preview.
    with bot.db() as db:
        db.execute('UPDATE bot_jobs SET payload=? WHERE id=?', (json.dumps(payload), job['id']))
    return bot.reply(job, payload, response, waiting)
