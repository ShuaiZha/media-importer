"""Owner capability for the camera workspace; existing viewer URLs stay read-only."""
import asyncio
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlencode

from .model import BackupError, ConfigError
from .source_connection import fresh
from .store import Store

KEY = 'camera_workspace_access'


def issue(store, owner):
    binding = store.get_meta('source_selection', {})
    if not binding.get('serial_number') or not binding.get('type'):
        raise ConfigError('请先连接并确认相机')
    current = store.get_meta(KEY, {})
    if (current.get('owner') != owner or current.get('serial') != binding['serial_number'] or
            current.get('type') != binding['type'] or current.get('until', 0) < time.time() + 300):
        current = {'token': secrets.token_hex(32), 'owner': owner, 'serial': binding['serial_number'],
                   'type': binding['type'], 'until': time.time() + 3600}
        store.set_meta(KEY, current)
    return current


def authorize(store, token):
    value = store.get_meta(KEY, {})
    binding = store.get_meta('source_selection', {})
    if (not isinstance(token, str) or not isinstance(value.get('token'), str) or
            not hmac.compare_digest(value['token'], token) or value.get('until', 0) <= time.time() or
            (value.get('serial'), value.get('type')) != (binding.get('serial_number'), binding.get('type'))):
        raise ConfigError('控制入口已失效，请从飞书重新打开相机页面')
    return value


def button(bot, store):
    from .bot_cards import plain
    from .preview_access import configuration
    access = store.get_meta('preview_access', {})
    if access.get('state') != 'ready': return None
    try:
        token = issue(store, bot.scope)['token']
        config = configuration(bot.settings.read()['config'])
        url = (config['public'] or config['local']) + '/camera/' + token + '/'
        link = 'https://applink.feishu.cn/client/web_url/open?' + urlencode(
            {'mode': 'window', 'url': url, 'width': 1180, 'height': 860})
        return {'tag': 'button', 'text': plain('打开相机页面'), 'type': 'primary', 'url': link}
    except ConfigError:
        return None


class Workspace:
    def __init__(self, directory, settings, control):
        self.directory, self.settings, self.control = directory, settings, control

    def snapshot(self, store, owner):
        from .capture import observation, allowed_commands
        from .capture_forms import read_catalog
        connection = store.get_meta('source_connection', {})
        device = connection.get('device', {})
        observed = observation(store, device)
        preview = store.get_meta('live_preview', {})
        try:
            catalog = read_catalog(store, connection.get('id'))
            if not observed.get('stale') and observed.get('preset') != catalog.get('preset'):
                catalog = None
        except ConfigError:
            catalog = None
        # Never expose component URLs, network credentials or identity internals.
        return {'device': {'name': device.get('model_name', '未连接相机'), 'transport': device.get('transport'),
                           'online': connection.get('state') == 'approved' and fresh(connection)},
                'session_id': connection.get('id'), 'state': observed,
                'commands': allowed_commands(store, connection),
                'catalog': {k: catalog[k] for k in ('preset', 'presets', 'parameters', 'observed_at') if k in catalog} if catalog else None,
                'preview': {'state': preview.get('state', 'stopped'), 'id': preview.get('id'),
                            'mode': preview.get('mode', 'preview'),
                            'reason': preview.get('reason'), 'cleanup_pending': bool(preview.get('cleanup_pending')),
                            'path': '/view/' + preview['viewer_token'] + '/' if preview.get('state') == 'ready'
                            and not preview.get('stop_requested') and preview.get('owner') == owner else None},
                'sync_enabled': bool(self.settings.read()['config']['backup']['enabled'])}

    async def dispatch(self, token, value):
        if not isinstance(value, dict) or not isinstance(value.get('action'), str):
            raise ConfigError('请求格式无效')
        action = value['action']
        shapes = {'status': {'action'}, 'capture': {'action', 'command', 'session_id', 'nonce', 'parameters'},
                  'result': {'action', 'id'}, 'preview_start': {'action', 'nonce'}, 'live_start': {'action', 'nonce'}, 'preview_stop': {'action', 'id'},
                  'library_list': {'action', 'session_id'}, 'library_touch': {'action', 'session_id'},
                  'library_release': {'action', 'session_id'}}
        if action not in shapes or set(value) != shapes[action]: raise ConfigError('请求参数无效')
        if 'nonce' in value and (not isinstance(value['nonce'], str) or not re.fullmatch('[0-9a-f]{32}', value['nonce'])):
            raise ConfigError('操作标识无效')
        db = Store(self.directory)
        future = None
        try:
            access = authorize(db, token)
            owner = access['owner']
            connection = db.get_meta('source_connection', {})
            if action == 'status': return self.snapshot(db, owner)
            if action == 'result':
                from .capture import get
                if not isinstance(value['id'], str) or not re.fullmatch('[0-9a-f]{32}', value['id']):
                    raise ConfigError('操作标识无效')
                result = get(db.db, value['id'], owner)
                if not result: raise ConfigError('未找到此操作，请核对相机状态')
                return {'state': result['state'], 'result': json.loads(result['result'])}
            from .camera_library import touch
            if action.startswith('library_'):
                if value['session_id'] != connection.get('id') or not fresh(connection) or connection.get('state') != 'approved':
                    raise ConfigError('相机连接已变化，请刷新页面')
                from .camera_coordinator import operation_reason
                if action != 'library_release' and operation_reason(db, 'browse'):
                    raise ConfigError('请先结束实时预览，再浏览素材')
                was_active = db.get_meta('camera_library', {}).get('until', 0) > time.time()
                touch(db, value['session_id'], release=action == 'library_release')
                if not was_active and action != 'library_release': self.control.source_interrupt.set()
                if action == 'library_list': future = self.control.library.submit(value['session_id'], 'list')
                else: return {'ok': True}
            elif action == 'capture':
                from .capture import enqueue, READ_COMMANDS
                command = value['command']
                if command not in READ_COMMANDS: touch(db, connection.get('id'), release=True)
                result = enqueue(db, owner, 'workspace:' + value['nonce'], command, value['session_id'], value['parameters'])
                return {'id': result['id'], 'state': result['state']}
            elif action in ('preview_start', 'live_start'):
                from .live_preview import request
                touch(db, connection.get('id'), release=True)
                result = request(db, self.settings, owner, 'workspace:' + value['nonce'], mode='rtmp' if action == 'live_start' else 'preview')
                return {'id': result['id'], 'state': result['state']}
            elif action == 'preview_stop':
                from .live_preview import stop_request
                result = stop_request(db, owner, value['id'])
                return {'id': result['id'], 'state': result['state']}
        finally:
            self.control.changed.set()
            db.close()
        if future:
            try:
                return await asyncio.wait_for(asyncio.wrap_future(future), 35)
            except asyncio.TimeoutError:
                raise ConfigError('相机读取等待超时，请稍后刷新；当前传输记录已保留')

    async def media(self, token, resource, identifier, header):
        db = Store(self.directory)
        try:
            authorize(db, token)
            connection = db.get_meta('source_connection', {})
            from .camera_library import active
            if not active(db): raise ConfigError('浏览会话已结束，请刷新素材')
            future = self.control.library.submit(connection.get('id'), resource, identifier, header)
        finally: db.close()
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(future), 35)
        except asyncio.TimeoutError:
            raise ConfigError('素材读取超时，请重试')
        db = Store(self.directory)
        try: authorize(db, token)  # Revocation also applies to an in-flight read.
        finally: db.close()
        return result

    async def stream(self, client):
        from websockets.exceptions import ConnectionClosed
        try:
            async for raw in client:
                identifier = None
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict) or set(value) != {'id', 'request'} or type(value['id']) is not int:
                        raise ConfigError('请求无效')
                    identifier = value['id']
                    result = await self.dispatch(client.camera_token, value['request'])
                    response = {'id': identifier, 'result': result}
                except (BackupError, ConfigError, ValueError, TypeError) as exc:
                    response = {'id': identifier, 'error': str(exc) if isinstance(exc, (BackupError, ConfigError)) else '请求无效'}
                await client.send(json.dumps(response, ensure_ascii=False))
        except ConnectionClosed:
            pass
