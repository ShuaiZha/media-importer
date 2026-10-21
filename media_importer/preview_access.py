"""Loopback playback gateway. Only session-scoped, read-only media crosses it."""
import asyncio
import hmac
import json
import logging
import re
import time
from http import HTTPStatus
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

from .deployment import environment, number, text
from .model import ConfigError, now
from .preview_player import PAGE
from .source_connection import read


def configuration(settings_values=None):
    values = environment()
    port = number(values, 'PREVIEW_LOCAL_PORT', 18765, integer=True)
    if not 1024 <= port <= 65535:
        raise ConfigError('PREVIEW_LOCAL_PORT 必须在 1024–65535 之间')
    if settings_values is None:
        import os
        from .config import Config
        settings_values = Config.load(os.environ.get('MEDIA_IMPORTER_CONFIG', '/config/settings.json')).values
    from .preview_settings import normalize_url
    public = normalize_url(settings_values.get('preview', {}).get('public_url', ''))
    return {'port': port, 'local': f'http://127.0.0.1:{port}', 'public': public}


def links(state, settings_values=None):
    config = configuration(settings_values)
    token = state.get('viewer_token')
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{64}', token):
        raise ConfigError('旧预览会话已结束，请重新开启预览')
    path = '/view/' + token + '/'
    return {'url': (config['public'] or config['local']) + path,
            'local_url': config['local'] + path, 'remote_configured': bool(config['public'])}


def playable(state, token):
    return (state.get('state') == 'ready' and not state.get('stop_requested') and
            not state.get('cleanup_pending') and isinstance(state.get('viewer_token'), str) and
            hmac.compare_digest(state['viewer_token'], token))


def upstream_base(state):
    parsed = urlsplit(state.get('upstream', ''))
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or
            parsed.username or not re.fullmatch('/[0-9a-f]{32}/stream.html', parsed.path)):
        raise ValueError('Invalid internal stream')
    return f'http://127.0.0.1:{parsed.port}' + parsed.path.rsplit('/', 1)[0]


def media_request(message, *, webrtc=False):
    """Never pass arbitrary go2rtc WS commands, source names or URLs upstream."""
    if not isinstance(message, str) or len(message) > 32768:
        raise ValueError('Invalid message')
    value = json.loads(message)
    if not isinstance(value, dict) or set(value) != {'type', 'value'}:
        raise ValueError('Unsupported media command')
    if value['type'] == 'mse':
        if not isinstance(value['value'], str) or not re.fullmatch(r'[a-zA-Z0-9.,_-]{1,512}', value['value']):
            raise ValueError('Invalid codec')
    elif webrtc:
        from .preview_rtc import signaling
        signaling(value['type'], value['value'])
    else:
        raise ValueError('Unsupported media command')
    return json.dumps(value)

class Gateway:
    def __init__(self, directory, config, settings=None, control=None):
        from .preview_hls import HLS
        self.directory, self.config, self.settings = directory, config, settings
        self.clients = set()
        self.scripts = {}
        self.hls = HLS()
        from .camera_web import Workspace
        self.workspace = Workspace(directory, settings, control) if control else None

    def state(self):
        return read(self.directory, 'live_preview')

    @staticmethod
    def response(status, body, content_type='text/plain; charset=utf-8', *, extra=None, frame=False):
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        data = body.encode() if isinstance(body, str) else body
        headers = Headers({'Content-Type': content_type, 'Content-Length': str(len(data)),
                           'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                           'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'SAMEORIGIN' if frame else 'DENY',
                           'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; img-src 'self' blob:; connect-src 'self'; frame-ancestors " + ("'self'" if frame else "'none'")})
        for key, value in (extra or {}).items(): headers[key] = value
        return Response(status, HTTPStatus(status).phrase, headers, data)

    async def request(self, connection, request):
        hosts = {urlsplit(self.config['local']).netloc}
        if self.config['public']: hosts.add(urlsplit(self.config['public']).netloc)
        if request.headers.get('Host') not in hosts:
            return self.response(403, '访问地址未授权')
        if request.path == '/health':
            return self.response(200, 'media-importer-preview')
        camera = re.fullmatch(r'/camera/([0-9a-f]{64})/(|control|thumbnail/[0-9a-f]{32}|media/[0-9a-f]{32})', request.path)
        if camera and self.workspace:
            from .camera_web import authorize
            from .model import BackupError
            from .store import Store
            token, resource = camera.groups()
            db = Store(self.directory)
            try:
                authorize(db, token)
            except ConfigError:
                return self.response(410, '控制入口已失效，请从飞书重新打开相机页面')
            finally: db.close()
            if resource == 'control':
                # Unlike a receive-only player, controls require a same-origin
                # browser handshake, even for loopback connections.
                if request.headers.get('Origin') not in {self.config['local'], self.config['public']}:
                    return self.response(403, '控制来源未授权')
                connection.camera_token = token
                return None
            if not resource:
                from .camera_page import PAGE as CAMERA_PAGE
                return self.response(200, CAMERA_PAGE, 'text/html; charset=utf-8')
            try:
                kind, identifier = resource.split('/')
                body, content_type, headers = await self.workspace.media(token, kind, identifier, request.headers.get('Range'))
                return self.response(206 if kind == 'media' else 200, body, content_type, extra=headers)
            except ConfigError as exc:
                return self.response(409, str(exc))
            except (BackupError, OSError):
                return self.response(503, '素材读取失败，请刷新相机连接后重试')
        match = re.fullmatch(r'/view/([0-9a-f]{64})/(|player\.js|status|socket|hls/master\.m3u8|hls/[A-Za-z0-9]{8}/(?:playlist\.m3u8|segment\.ts)(?:\?n=[0-9]{1,12})?)', request.path)
        if not match:
            return self.response(404, '页面不存在')
        token, resource = match.groups()
        state = self.state()
        if not playable(state, token):
            return self.response(410, '预览已结束，请从飞书重新开启。')
        if resource == 'socket':
            origin = request.headers.get('Origin')
            if origin and origin not in {self.config['local'], self.config['public']}:
                return self.response(403, '播放来源未授权')
            connection.preview_token = token
            return None
        if not resource:
            return self.response(200, PAGE, 'text/html; charset=utf-8', frame=True)
        if resource == 'status':
            return self.response(200, json.dumps({'state': 'ready', 'webrtc': bool(state.get('webrtc'))}), 'application/json')
        base = upstream_base(state)
        if resource.startswith('hls/'):
            try:
                data, content_type = await asyncio.to_thread(self.hls.load, base, token, resource)
            except ValueError:
                return self.response(404, '播放片段不可用')
            except OSError:
                return self.response(503, '正在重新连接画面')
            # Stop revokes even a segment that was already being fetched.
            if not playable(self.state(), token):
                return self.response(410, '预览已结束')
            return self.response(200, data, content_type)
        if base not in self.scripts:
            def load():
                with build_opener(ProxyHandler({})).open(base + '/video-rtc.js', timeout=3) as response:
                    return response.read(256 * 1024)
            try:
                self.scripts = {base: await asyncio.to_thread(load)}
            except OSError:
                return self.response(503, '播放组件暂不可用')
        return self.response(200, self.scripts[base], 'text/javascript; charset=utf-8')

    async def stream(self, client):
        if self.workspace and getattr(client, 'camera_token', None):
            await self.workspace.stream(client)
            return
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        token = client.preview_token
        state = self.state()
        if not playable(state, token):
            await client.close(code=1008); return
        self.clients.add(client)
        jobs = []
        try:
            # A fixed local upstream; no URL or stream selector from the client.
            base = upstream_base(state)
            async with connect('ws' + base[4:] + '/api/ws?src=preview', proxy=None,
                               max_size=8 * 1024 * 1024, max_queue=4, compression=None,
                               open_timeout=5, close_timeout=1) as upstream:
                async def incoming():
                    reported = 0
                    negotiated, candidates = False, 0
                    async for message in client:
                        value = json.loads(message) if isinstance(message, str) else None
                        if isinstance(value, dict) and value.get('type') == 'viewer-stats':
                            frames, seconds = value.get('frames'), value.get('seconds')
                            if (not {'type','frames','seconds'} <= set(value) <= {'type','frames','seconds','mode','buffer_seconds','jitter_buffer_ms'} or type(frames) is not int or
                                    not 0 <= frames < 2**53 or type(seconds) not in (int, float) or not 0 <= seconds < 1e9):
                                raise ValueError('Invalid statistics')
                            if 'mode' in value and value['mode'] not in ('hls', 'mse', 'webrtc'):
                                raise ValueError('Invalid playback mode')
                            for key, limit in (('buffer_seconds', 3600), ('jitter_buffer_ms', 60000)):
                                if key in value and (type(value[key]) not in (int, float) or not 0 <= value[key] < limit):
                                    raise ValueError('Invalid playback statistics')
                            if time.monotonic() - reported >= 5:
                                from .store import Store
                                db = Store(self.directory)
                                try: db.set_meta('preview_viewer', {'session_id': state['id'], 'reported_frames': frames,
                                                                  'reported_seconds': seconds, 'observed_at': now(),
                                                                  **{key: value[key] for key in ('mode','buffer_seconds','jitter_buffer_ms') if key in value}})
                                finally: db.close()
                                reported = time.monotonic()
                            continue
                        command = media_request(message, webrtc=bool(state.get('webrtc')))
                        if value['type'] in ('mse', 'webrtc/offer'):
                            if negotiated: raise ValueError('Already negotiated')
                            negotiated = True
                        elif value['type'] == 'webrtc/candidate':
                            candidates += 1
                            if candidates > 64: raise ValueError('Too many candidates')
                        await upstream.send(command)
                async def outgoing():
                    async for message in upstream:
                        if not playable(self.state(), token): return
                        await client.send(message)
                async def revoke():
                    while playable(self.state(), token): await asyncio.sleep(.25)
                jobs = [asyncio.create_task(f()) for f in (incoming, outgoing, revoke)]
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for job in done: job.result()
        except (ConnectionClosed, OSError, ValueError, KeyError, asyncio.TimeoutError):
            pass  # URLs and credentials never reach diagnostics.
        finally:
            for job in jobs: job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            self.clients.discard(client)
            await client.close(code=1000)

    async def run(self, stop):
        from websockets.asyncio.server import serve
        from .store import Store
        from .host_network import preview_mapping
        logger = logging.getLogger('media-importer.preview-http')
        logger.disabled = True  # Library errors may include the session URL.
        async with serve(self.stream, '127.0.0.1', self.config['port'], process_request=self.request,
                         logger=logger, max_size=32768, max_queue=4, compression=None,
                         close_timeout=1, server_header=None):
            db = Store(self.directory)
            try:
                checked, refreshed, saved_revision = -60, -1, None
                while not stop.is_set():
                    if self.settings and time.monotonic() - refreshed >= 1:
                        item = self.settings.read()
                        if not item['error'] and item['revision'] != saved_revision:
                            self.config['public'] = item['config']['preview']['public_url']
                            saved_revision, checked = item['revision'], -60
                        refreshed = time.monotonic()
                    if time.monotonic() - checked >= 60:
                        remote = await asyncio.to_thread(preview_mapping, self.config['public'], self.config['port'])
                        db.set_meta('preview_access', {'state': 'ready', 'remote_configured': bool(self.config['public']),
                                                     'port': self.config['port'], 'settings_revision': saved_revision, **remote, 'observed_at': now()})
                        checked = time.monotonic()
                    await asyncio.sleep(.25)
            finally:
                db.set_meta('preview_access', {'state': 'stopped', 'observed_at': now()})
                db.close()


def serve(directory, stop, control=None):
    import os
    import sys
    if sys.platform != 'darwin' or os.environ.get('MEDIA_IMPORTER_RUNTIME') != 'native':
        stop.wait(); return
    from .settings import Settings
    settings = Settings(os.environ.get('MEDIA_IMPORTER_CONFIG', '/config/settings.json'), directory)
    asyncio.run(Gateway(directory, configuration(settings.read()['config']), settings, control).run(stop))
