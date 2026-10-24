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


def cleanup_snapshot(op):
    if not op:
        return None
    plan, result = json.loads(op['plan'] or '{}'), json.loads(op['result'] or '{}')
    from .message_facts import cleanup_scope, cleanup_result_counts
    scope = cleanup_scope(plan)
    state = 'cancelled' if op['cancelled'] and op['state'] == 'ready' else op['state']
    return {**cleanup_result_counts(plan, result), 'id': op['id'], 'state': state, 'mode': op['mode'], 'cancelled': bool(op['cancelled']),
            'primary_files': scope['primary_files'], 'auxiliary_files': scope['auxiliary_files'],
            'eligible_files': result.get('eligible_files', 0), 'eligible_bytes': result.get('eligible_bytes', 0),
            'pending_archive': result.get('pending_archive', 0),
            'error': result.get('error'), 'deleted': result.get('deleted', 0),
            'absent': result.get('absent', 0), 'retained': result.get('retained', 0),
            'unknown': result.get('unknown', 0), 'released_bytes': result.get('released_bytes', 0),
            'files': [{'name': a['path'].rsplit('/', 1)[-1], 'size': a['size'], 'auxiliary': a['auxiliary']}
                      for a in scope['members']],
            'preserved': plan.get('retained', []), 'details': result.get('details', [])}


def backup_snapshot(store, op):
    """Expose selection facts, never the serialized source/credential records."""
    if not op:
        return None
    from .selected_imports import progress, archive_members
    from .message_facts import progress_rows, selected_view
    op = progress(store, op)
    plan, result = json.loads(op['plan'] or '{}'), json.loads(op['result'])
    complete = bool(result.get('total_files') and
                    result.get('uploaded_files') == result['total_files'] and
                    not result.get('archive_verification_pending'))
    reason = result.get('error') or result.get('reason') or ''
    reason = {'pending_files': '部分文件尚未完成，等待重试',
              'cache_cleanup': '归档已完成，缓存清理待重试',
              'archive_verification': '已有归档待核验'}.get(reason, reason)
    return {'id': op['id'], 'state': op['state'], 'choice_revision': op['choice_revision'],
            'confirmed': bool(plan.get('confirmed')), 'stopping': bool(op['cancelled'] or op['drain']),
            'complete': complete, 'reason': reason,
            'total_files': result.get('total_files', 0), 'total_bytes': result.get('total_bytes', 0),
            'already_archived': result.get('already_archived', 0),
            'excluded_preview_files': result.get('excluded_preview_files', 0),
            'cache_cleanup_pending': result.get('cache_cleanup_pending') or result.get('cleanup_pending') or 0,
            'destination': plan.get('archive_location', ''),
            'progress': progress_rows(selected_view(result, op['state'])),
            'files': [{'name': a['path'].rsplit('/', 1)[-1], 'path': a['path'], 'size': a['size']}
                      for i in plan.get('selected', [])
                      for a in archive_members(plan['groups'][i], plan['archive_previews'])]
                       if op['state'] == 'ready' else []}


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


def button(bot, store, *, media=False):
    from .bot_cards import plain
    from .preview_access import configuration
    access = store.get_meta('preview_access', {})
    if access.get('state') != 'ready': return None
    try:
        token = issue(store, bot.scope)['token']
        config = configuration(bot.settings.read()['config'])
        url = (config['public'] or config['local']) + '/camera/' + token + '/' + ('#media' if media else '')
        link = 'https://applink.feishu.cn/client/web_url/open?' + urlencode(
            {'mode': 'window', 'url': url, 'width': 1180, 'height': 860})
        return {'tag': 'button', 'text': plain('打开相机素材' if media else '打开画面与拍摄'), 'type': 'primary', 'url': link}
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
        from .camera_coordinator import state as camera_state
        camera = camera_state(store)
        observed = camera['observed']
        from .camera_coordinator import operation_reason
        preview = store.get_meta('live_preview', {})
        try:
            catalog = read_catalog(store, connection.get('id'))
            if not observed.get('stale') and observed.get('preset') != catalog.get('preset'):
                catalog = None
        except ConfigError:
            catalog = None
        selected = store.db.execute("""SELECT * FROM selected_imports WHERE owner=?
            AND json_extract(plan,'$.origin')='camera_workspace' ORDER BY created DESC LIMIT 1""", (owner,)).fetchone()
        cleanup = store.db.execute("""SELECT * FROM camera_operations WHERE owner=?
            AND json_extract(plan,'$.origin')='camera_workspace' ORDER BY created DESC LIMIT 1""", (owner,)).fetchone()
        from .config import Config
        from .destination_binding import identity
        config = Config.from_values(self.settings.read()['config'])
        library = getattr(self.control, 'library', None)
        # Never expose component URLs, network credentials or identity internals.
        return {'device': {'name': device.get('model_name', '未连接相机'), 'transport': device.get('transport'),
                           'online': camera['connection']['state'] == 'connected'},
                'camera': camera,
                'session_id': connection.get('id'), 'state': observed,
                'commands': allowed_commands(store, connection, facts=camera),
                'restrictions': {name: operation_reason(store, name, facts=camera) for name in ('preview', 'livestream', 'browse')},
                'catalog': {k: catalog[k] for k in ('preset', 'presets', 'parameters', 'observed_at') if k in catalog} if catalog else None,
                'preview': {'state': preview.get('state', 'stopped'), 'id': preview.get('id'),
                            'mode': preview.get('mode', 'preview'),
                            'reason': preview.get('reason'), 'cleanup_pending': bool(preview.get('cleanup_pending')),
                            'path': '/view/' + preview['viewer_token'] + '/' if preview.get('state') == 'ready'
                            and not preview.get('stop_requested') and preview.get('owner') == owner else None},
                'backup': backup_snapshot(store, dict(selected)) if selected else None,
                'cleanup': cleanup_snapshot(dict(cleanup)) if cleanup else None,
                'file_states': library.recorded_states(store, identity(store.db, config), connection.get('id')) if library else {},
                'sync_enabled': bool(self.settings.read()['config']['backup']['enabled'])}

    async def dispatch(self, token, value):
        if not isinstance(value, dict) or not isinstance(value.get('action'), str):
            raise ConfigError('请求格式无效')
        action = value['action']
        shapes = {'status': {'action'}, 'capture': {'action', 'command', 'session_id', 'nonce', 'parameters'},
                  'result': {'action', 'id'}, 'preview_start': {'action', 'nonce'}, 'live_start': {'action', 'nonce'}, 'preview_stop': {'action', 'id'},
                  'library_list': {'action', 'session_id'}, 'library_touch': {'action', 'session_id'},
                  'library_release': {'action', 'session_id'},
                  'backup_review': {'action', 'session_id', 'catalog', 'identifiers', 'nonce'},
                  'backup_confirm': {'action', 'id', 'choice_revision'},
                  'backup_status': {'action', 'id'}, 'backup_cancel': {'action', 'id'},
                  'backup_resume': {'action', 'id'}}
        shapes.update(cleanup_review={'action', 'session_id', 'catalog', 'identifiers', 'nonce', 'mode'},
                      **{name: {'action', 'id'} for name in ('cleanup_status', 'cleanup_confirm', 'cleanup_cancel', 'cleanup_verify')})
        if action not in shapes or set(value) != shapes[action]: raise ConfigError('请求参数无效')
        if 'nonce' in value and (not isinstance(value['nonce'], str) or not re.fullmatch('[0-9a-f]{32}', value['nonce'])):
            raise ConfigError('操作标识无效')
        if action.startswith(('backup_', 'cleanup_')):
            for key in ('id', 'catalog'):
                if key in value and (not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{32}', value[key])):
                    raise ConfigError('备份操作已失效，请重新选择文件')
        readonly = action in ('status', 'result', 'backup_status', 'cleanup_status')
        db = Store(self.directory, readonly=readonly)
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
            if action.startswith('cleanup_'):
                from . import camera_operations
                if action == 'cleanup_review':
                    op = camera_operations.review_library(db, self.settings, self.control.library, owner,
                        value['session_id'], value['catalog'], value['identifiers'], value['nonce'], value['mode'])
                else:
                    op = camera_operations.get(db.db, value['id'], owner)
                    if not op or json.loads(op['plan'] or '{}').get('origin') != 'camera_workspace':
                        raise ConfigError('未找到本次清理，请重新选择文件')
                    if action == 'cleanup_confirm':
                        op = camera_operations.confirm(db, self.settings, op['id'], owner)
                    elif action == 'cleanup_cancel':
                        with db.db:
                            camera_operations.cancel(db.db, op['id'], owner)
                        op = camera_operations.get(db.db, op['id'], owner)
                    elif action == 'cleanup_verify':
                        op = camera_operations.verify(db, op['id'], owner)
                if action in ('cleanup_review', 'cleanup_confirm', 'cleanup_verify'):
                    touch(db, connection.get('id'), release=True)
                    self.control.source_interrupt.set()
                return cleanup_snapshot(op)
            elif action.startswith('backup_'):
                from . import selected_imports
                if action == 'backup_review':
                    op = selected_imports.review_library(db, self.settings, self.control.library, owner,
                        value['session_id'], value['catalog'], value['identifiers'], value['nonce'])
                else:
                    op = selected_imports.get(db.db, value['id'], owner)
                    if not op or json.loads(op['plan'] or '{}').get('origin') != 'camera_workspace':
                        raise ConfigError('未找到本次备份，请重新选择文件')
                    if action == 'backup_confirm':
                        op = selected_imports.confirm(db, self.settings, op['id'], owner, value['choice_revision'])
                    elif action == 'backup_cancel':
                        with db.db:
                            selected_imports.cancel(db.db, op['id'], owner)
                        op = selected_imports.get(db.db, op['id'], owner)
                    elif action == 'backup_resume':
                        op = selected_imports.resume(db, self.settings, op['id'], owner)
                    if action in ('backup_confirm', 'backup_resume') and op['state'] in selected_imports.ACTIVE:
                        touch(db, connection.get('id'), release=True)
                        self.control.source_interrupt.set()
                return backup_snapshot(db, op)
            elif action.startswith('library_'):
                if value['session_id'] != connection.get('id') or not fresh(connection) or connection.get('state') != 'approved':
                    raise ConfigError('相机连接已变化，请刷新页面')
                from .camera_coordinator import operation_reason
                reason = operation_reason(db, 'browse')
                if action != 'library_release' and reason:
                    raise ConfigError(reason)
                was_active = db.get_meta('camera_library', {}).get('until', 0) > time.time()
                touch(db, value['session_id'], release=action == 'library_release')
                if not was_active and action != 'library_release': self.control.source_interrupt.set()
                if action == 'library_list': future = self.control.library.submit(value['session_id'], 'list')
                else: return {'ok': True}
            elif action == 'capture':
                from .capture import enqueue, READ_COMMANDS
                command = value['command']
                result = enqueue(db, owner, 'workspace:' + value['nonce'], command, value['session_id'], value['parameters'])
                if command not in READ_COMMANDS: touch(db, connection.get('id'), release=True)
                return {'id': result['id'], 'state': result['state']}
            elif action in ('preview_start', 'live_start'):
                from .live_preview import request
                result = request(db, self.settings, owner, 'workspace:' + value['nonce'], mode='rtmp' if action == 'live_start' else 'preview')
                touch(db, connection.get('id'), release=True)
                return {'id': result['id'], 'state': result['state']}
            elif action == 'preview_stop':
                from .live_preview import stop_request
                result = stop_request(db, owner, value['id'])
                return {'id': result['id'], 'state': result['state']}
        finally:
            if not readonly:
                self.control.changed.set()
            db.close()
        if future:
            try:
                return await asyncio.wait_for(asyncio.wrap_future(future), 35)
            except asyncio.TimeoutError:
                raise ConfigError('相机读取等待超时，请稍后刷新；当前传输记录已保留')

    async def media(self, token, resource, identifier, header):
        db = Store(self.directory, readonly=True)
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
        db = Store(self.directory, readonly=True)
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
