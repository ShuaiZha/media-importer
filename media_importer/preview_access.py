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
from .source_connection import read


def configuration():
    values = environment()
    port = number(values, 'PREVIEW_LOCAL_PORT', 18765, integer=True)
    public = text(values, 'PREVIEW_PUBLIC_URL', '').rstrip('/')
    if not 1024 <= port <= 65535:
        raise ConfigError('PREVIEW_LOCAL_PORT 必须在 1024–65535 之间')
    if public:
        parsed = urlsplit(public)
        try: parsed.port
        except ValueError: raise ConfigError('PREVIEW_PUBLIC_URL 端口无效') from None
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
                parsed.path or parsed.query or parsed.fragment):
            raise ConfigError('PREVIEW_PUBLIC_URL 必须是 HTTPS 基础地址，不含路径、账号或查询参数')
    return {'port': port, 'local': f'http://127.0.0.1:{port}', 'public': public}


def links(state):
    config = configuration()
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


def media_request(message):
    """Never pass arbitrary go2rtc WS commands, source names or URLs upstream."""
    if not isinstance(message, str) or len(message) > 2048:
        raise ValueError('Invalid message')
    value = json.loads(message)
    if (not isinstance(value, dict) or set(value) != {'type', 'value'} or value['type'] != 'mse' or
            not isinstance(value['value'], str) or not re.fullmatch(r'[a-zA-Z0-9.,_-]{1,512}', value['value'])):
        raise ValueError('Unsupported media command')
    return json.dumps({'type': 'mse', 'value': value['value']})


PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>实时预览 · Media Importer</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif;background:#111827;color:#f3f4f6}
body{margin:0;padding:24px;max-width:1100px;margin:auto}header{display:flex;align-items:center;justify-content:space-between;gap:16px}
h1{font-size:20px;font-weight:600}#state{font-size:14px;color:#a5b4fc}viewer-video{display:block;background:#030712;border-radius:12px;overflow:hidden;aspect-ratio:16/9}
video{width:100%;height:100%;object-fit:contain}#screen{aspect-ratio:16/9;background:#030712;border-radius:12px;overflow:hidden}p{font-size:14px;color:#cbd5e1;line-height:1.7}button{padding:10px 16px;background:#334155;color:white;border:0;border-radius:8px;cursor:pointer}
@media(max-width:600px){body{padding:12px}header{align-items:flex-start;flex-direction:column;gap:0}}
</style><header><h1>实时预览</h1><span id="state" role="status" aria-live="polite">正在连接画面</span></header>
<div id="screen"></div><p id="detail">关闭窗口不会结束预览。请在飞书卡片中点击「结束预览」。</p>
<button id="retry" type="button">播放／重新连接</button>
<script type="module">
import {VideoRTC} from './player.js';
customElements.define('viewer-video', VideoRTC);
const screen=document.querySelector('#screen'), label=document.querySelector('#state'), detail=document.querySelector('#detail');
const native=document.createElement('video').canPlayType('application/vnd.apple.mpegurl');
const url=new URL('socket', location.href).href.replace(/^http/,'ws');
let player,stats,ended=false,lastTime=0,lastFrames=0,unchanged=0,started=0;
function video(){return native?player:player?.video;}
function connect(){
 if(ended)return;
 if(player){if(native){player.pause();player.removeAttribute('src');player.load();}player.remove();}
 if(stats)stats.close();
 lastTime=0;lastFrames=0;unchanged=0;started=Date.now();label.textContent='正在连接画面';
 if(native){
  player=document.createElement('video');player.controls=true;player.muted=true;player.playsInline=true;player.autoplay=true;
  screen.replaceChildren(player);player.src=new URL('hls/master.m3u8',location.href).href;
  player.play().catch(()=>{label.textContent='请点击播放按钮';});
  stats=new WebSocket(url);
 }else{
  player=document.createElement('viewer-video');player.mode='mse';player.media='video';player.background=false;
  screen.replaceChildren(player);player.src=url;
 }
}
document.querySelector('#retry').onclick=connect;
connect();
setInterval(async()=>{
 if(ended)return;
 try{
  const response=await fetch('status',{cache:'no-store'});
  if(response.status===410){ended=true;if(native){player.pause();player.removeAttribute('src');player.load();}player.remove();stats?.close();label.textContent='预览已结束';detail.textContent='请从飞书「相机 → 实时预览」重新开启。';return;}
  if(!response.ok)throw Error();
  const current=video(), frames=current?.getVideoPlaybackQuality?.().totalVideoFrames||0;
  if(current?.readyState>=2 && (current.currentTime>lastTime || frames>lastFrames)){
   label.textContent='画面播放中';unchanged=0;
   const report={type:'viewer-stats',frames,seconds:current.currentTime};
   if(native){if(stats.readyState===WebSocket.OPEN)stats.send(JSON.stringify(report));}else player.send(report);
  }else if(++unchanged>=3){label.textContent='等待画面 · 可尝试重新连接';}
  lastTime=current?.currentTime??0;lastFrames=frames;
  if(native && document.visibilityState==='visible' && unchanged>=8 && Date.now()-started>15000)connect();
 }catch{label.textContent='连接中断，正在重试；相机预览保持开启';}
},2000);
</script></html>'''


class Gateway:
    def __init__(self, directory, config):
        from .preview_hls import HLS
        self.directory, self.config = directory, config
        self.clients = set()
        self.scripts = {}
        self.hls = HLS()

    def state(self):
        return read(self.directory, 'live_preview')

    @staticmethod
    def response(status, body, content_type='text/plain; charset=utf-8'):
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        data = body.encode() if isinstance(body, str) else body
        headers = Headers({'Content-Type': content_type, 'Content-Length': str(len(data)),
                           'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                           'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                           'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"})
        return Response(status, HTTPStatus(status).phrase, headers, data)

    async def request(self, connection, request):
        hosts = {urlsplit(self.config['local']).netloc}
        if self.config['public']: hosts.add(urlsplit(self.config['public']).netloc)
        if request.headers.get('Host') not in hosts:
            return self.response(403, '访问地址未授权')
        if request.path == '/health':
            return self.response(200, 'media-importer-preview')
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
            return self.response(200, PAGE, 'text/html; charset=utf-8')
        if resource == 'status':
            return self.response(200, json.dumps({'state': 'ready'}), 'application/json')
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
                    async for message in client:
                        value = json.loads(message) if isinstance(message, str) else None
                        if isinstance(value, dict) and value.get('type') == 'viewer-stats':
                            frames, seconds = value.get('frames'), value.get('seconds')
                            if (set(value) != {'type', 'frames', 'seconds'} or type(frames) is not int or
                                    not 0 <= frames < 2**53 or type(seconds) not in (int, float) or not 0 <= seconds < 1e9):
                                raise ValueError('Invalid statistics')
                            if time.monotonic() - reported >= 5:
                                from .store import Store
                                db = Store(self.directory)
                                try: db.set_meta('preview_viewer', {'session_id': state['id'], 'reported_frames': frames,
                                                                  'reported_seconds': seconds, 'observed_at': now()})
                                finally: db.close()
                                reported = time.monotonic()
                            continue
                        await upstream.send(media_request(message))
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
                         logger=logger, max_size=2048, max_queue=4, compression=None,
                         close_timeout=1, server_header=None):
            db = Store(self.directory)
            try:
                checked = -60
                while not stop.is_set():
                    if time.monotonic() - checked >= 60:
                        remote = await asyncio.to_thread(preview_mapping, self.config['public'], self.config['port'])
                        db.set_meta('preview_access', {'state': 'ready', 'remote_configured': bool(self.config['public']),
                                                     'port': self.config['port'], **remote, 'observed_at': now()})
                        checked = time.monotonic()
                    await asyncio.sleep(.25)
            finally:
                db.set_meta('preview_access', {'state': 'stopped', 'observed_at': now()})
                db.close()


def serve(directory, stop):
    import os
    import sys
    if sys.platform != 'darwin' or os.environ.get('MEDIA_IMPORTER_RUNTIME') != 'native':
        stop.wait(); return
    asyncio.run(Gateway(directory, configuration()).run(stop))
