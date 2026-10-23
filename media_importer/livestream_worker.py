"""Owned RTMP ingress and audio relay. Camera protocol stays in its adapter."""
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid
from urllib.request import build_opener, ProxyHandler

from .live_preview import component
from .model import BackupError, ConfigError
from .settings import atomic_write


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def stop_process(process):
    if process and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=3)


class Relay:
    def __init__(self, network, port, folder, public_url):
        from .preview_rtc import endpoint
        self.rtc = endpoint(public_url)
        self.network, self.port, self.folder = network, port, Path(folder)
        self.api, self.rtsp = free_port(), free_port()
        self.name, self.token = uuid.uuid4().hex, uuid.uuid4().hex
        self.base = f'http://127.0.0.1:{self.api}/{self.token}'
        self.proc = self.audio = None

    @property
    def publish_url(self):
        return f"rtmp://{self.network['address']}:{self.port}/{self.name}/live"

    def streams(self):
        with build_opener(ProxyHandler({})).open(self.base + '/api/streams', timeout=3) as response:
            return json.load(response)

    def start(self):
        with socket.socket() as sock:
            try: sock.bind((self.network['address'], self.port))
            except OSError: raise ConfigError('RTMP 接收端口不可用，请检查直播网络和端口占用') from None
        config = {'api': {'listen': f'127.0.0.1:{self.api}', 'base_path': '/' + self.token},
                  'rtsp': {'listen': f'127.0.0.1:{self.rtsp}'},
                  'rtmp': {'listen': f"{self.network['address']}:{self.port}"},
                  'webrtc': self.rtc or {'listen': '', 'ice_servers': []},
                  'streams': {self.name: []}}
        path = self.folder / 'go2rtc.json'
        path.write_text(json.dumps(config))
        self.proc = subprocess.Popen([str(component()), '-config', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        until = time.monotonic() + 8
        while time.monotonic() < until:
            if self.proc.poll() is not None: break
            try: self.streams(); return
            except (OSError, ValueError): time.sleep(.1)
        raise ConfigError('直播接收组件未就绪，请检查端口')

    def healthy(self):
        return bool(self.proc and self.proc.poll() is None and (self.audio is None or self.audio.poll() is None))

    async def wait_media(self, codec, stop, timeout=15):
        until = time.monotonic() + timeout
        while not stop.is_set() and time.monotonic() < until:
            if not self.healthy(): raise ConfigError('直播组件已退出')
            stream = (await asyncio.to_thread(self.streams)).get(self.name, {})
            if any(codec in m for p in stream.get('producers') or [] for m in p.get('medias', [])): return
            await asyncio.sleep(.2)
        raise ConfigError('直播已取消' if stop.is_set() else '未收到直播音视频，请检查相机与 Mac 的网络连接')

    def start_audio(self):
        url = f'rtsp://127.0.0.1:{self.rtsp}/{self.name}'
        self.audio = subprocess.Popen([str(component('ffmpeg')), '-hide_banner', '-nostdin', '-loglevel', 'error',
            '-rtsp_transport', 'tcp', '-timeout', '8000000', '-fflags', 'nobuffer', '-flags', 'low_delay',
            '-probesize', '32768', '-analyzeduration', '100000', '-max_delay', '0',
            '-i', url + '?audio=aac', '-map', '0:a:0', '-vn', '-c:a', 'libopus', '-b:a', '64000',
            '-application', 'lowdelay', '-frame_duration', '10', '-flush_packets', '1',
            '-max_interleave_delta', '100000', '-muxdelay', '0', '-f', 'rtsp', '-rtsp_transport', 'tcp', url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self):
        stop_process(self.audio); stop_process(self.proc)


async def session(source, stop, emit, *, network, secret, recovery_path, cleanup_only=False, public_url=''):
    from .adapters import livestream_camera
    from .livestream_network import discover
    recovery_path = Path(recovery_path)
    before, relay, camera, clean, error = None, None, None, True, None
    phase = 'recovery'
    try:
        if recovery_path.exists():
            clean = False
            saved = json.loads(recovery_path.read_text())
            if saved['serial'] != source['serial']: raise ConfigError('直播恢复记录与当前相机不符')
            before = saved['before']; cleanup_only = True; clean = False
        elif cleanup_only:
            # No durable intent means no camera/network write was allowed.
            return
        with tempfile.TemporaryDirectory(prefix='media-importer-live-') as folder:
            if not cleanup_only:
                phase = 'receiver'
                address = await asyncio.to_thread(discover)
                relay = Relay(address, network['port'], folder, public_url)
                await asyncio.to_thread(relay.start)
            camera = livestream_camera(source['type'], source['serial'])
            phase = 'bluetooth'
            emit('phase', reason='正在连接相机蓝牙')
            await asyncio.wait_for(camera.__aenter__(), 45)
            if not cleanup_only:
                phase = 'camera_state'
                before = await asyncio.wait_for(camera.before(), 35)
                # Crash recovery includes only original settings, never Wi-Fi secrets.
                atomic_write(recovery_path, json.dumps({'serial': source['serial'], 'before': before}))
                clean = False
                phase = 'network'
                emit('phase', reason='正在连接直播网络')
                await camera.join(network['ssid'], secret, stop)
                secret = None
                phase = 'livestream'
                await camera.start_live(relay.publish_url, stop)
                phase = 'media'
                await relay.wait_media('H264', stop)
                await asyncio.to_thread(relay.start_audio)
                await relay.wait_media('OPUS', stop)
                emit('ready', url=relay.base + '/stream.html?src=' + relay.name,
                     stream=relay.name, webrtc=bool(relay.rtc), audio=True)
                phase = 'playing'
                seen, received, checked = time.monotonic(), -1, 0
                while not stop.is_set():
                    if not relay.healthy(): raise ConfigError('直播组件已退出')
                    stream = (await asyncio.to_thread(relay.streams)).get(relay.name, {})
                    total = sum(p.get('bytes_recv', 0) for p in stream.get('producers') or [])
                    if total != received: seen, received = time.monotonic(), total
                    if time.monotonic() - seen > 15: raise ConfigError('直播数据已中断，正在恢复相机连接')
                    if time.monotonic() - checked > 3:
                        await camera.keep()
                        if not await camera.is_live(): raise ConfigError('相机直播状态已变化，正在停止直播')
                        checked = time.monotonic()
                    await asyncio.sleep(.3)
    except Exception as exc:
        if not stop.is_set():
            error = str(exc) if isinstance(exc, BackupError) else {
                'bluetooth': '蓝牙连接超时或中断，请确认相机开机并退出手机 Quik 后重试',
                'camera_state': '相机状态读取未完成，请重试连接',
            }.get(phase, '直播连接异常，请检查蓝牙与网络后重试')
            # Phase and exception class aid diagnosis without retaining SDK text,
            # addresses, SSIDs or passwords.
            emit('diagnostic', phase=phase, error_type=type(exc).__name__)
    finally:
        secret = None
        async def close_ingress():
            if relay: await asyncio.to_thread(relay.close)
        if before and not clean:
            emit('phase', reason='正在停止直播并恢复相机')
            for attempt in range(2):
                try:
                    if not camera or not camera.client or not camera.client.is_connected:
                        if camera: await camera.close()
                        camera = livestream_camera(source['type'], source['serial']); await camera.__aenter__()
                    await asyncio.wait_for(camera.restore(before, close_ingress), 145)
                    clean = True
                    recovery_path.unlink(missing_ok=True)
                    break
                except Exception:
                    if camera: await camera.close()
                    if attempt == 1: error = '直播停止或连接恢复尚未确认，请恢复相机供电后再次结束直播'
        await close_ingress()
        if camera: await camera.close()
        if error: emit('error', message=error)
        emit('closed', camera_stopped=clean, reason='有声直播已结束' if clean else error)


def run(source, stop, emit, **options):
    asyncio.run(session(source, stop, emit, **options))
