"""Owned source stream: explicit stop, parent loss and source faults close it."""
import http.client
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.request import urlopen
import uuid

from .adapters import create_source
from .live_preview import component
from .model import BackupError


def emit(event, **values):
    print(json.dumps({'event': event, **values}), flush=True)


def run(source, stop, emit_event=emit, *, cleanup_only=False):
    client = create_source(SimpleNamespace(source=source))
    client.identify()
    if cleanup_only:
        client.preview_stop()
        emit_event('closed', camera_stopped=True, reason='已确认预览结束；如需观看，请重新打开')
        return
    spec = client.preview_info()
    process, udp, connection, relay = None, None, None, None
    relay_stop = threading.Event()
    started, stopped = False, True
    reason, issue = '预览已结束', None
    with tempfile.TemporaryDirectory(prefix='media-importer-preview-') as temporary:
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind(('', spec['port']))
            udp.settimeout(.3)
            with socket.socket() as port_socket:
                port_socket.bind(('127.0.0.1', 0)); port = port_socket.getsockname()[1]
            token = uuid.uuid4().hex
            base = f'http://127.0.0.1:{port}/{token}'
            config = Path(temporary) / 'go2rtc.json'
            # JSON is valid YAML. Only the current machine can reach this endpoint.
            config.write_text(json.dumps({'api': {'listen': f'127.0.0.1:{port}', 'base_path': '/' + token},
                                         'rtsp': {'listen': ''}, 'webrtc': {'listen': ''},
                                         'streams': {'preview': []}}))
            process = subprocess.Popen([str(component()), '-config', str(config)], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 10
            while not stop.is_set():
                try:
                    with urlopen(base + '/api', timeout=1) as response:
                        if response.status == 200: break
                except OSError: pass
                if time.monotonic() >= deadline or process.poll() is not None:
                    raise BackupError('预览组件启动失败，请检查端口或重新安装当前版本')
                stop.wait(.1)
            if stop.is_set(): return
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.putrequest('POST', '/' + token + '/api/stream.ts?dst=preview')
            connection.putheader('Transfer-Encoding', 'chunked'); connection.endheaders()
            received = {'at': time.monotonic(), 'error': False}
            def forward():
                try:
                    while not relay_stop.is_set() and not stop.is_set():
                        try: data, address = udp.recvfrom(65535)
                        except socket.timeout: continue
                        if address[0] != spec['host']: continue
                        connection.send(('%X\r\n' % len(data)).encode() + data + b'\r\n')
                        received['at'] = time.monotonic()
                except OSError:
                    received['error'] = True
            relay = threading.Thread(target=forward, daemon=True); relay.start()
            started, stopped = True, False  # Persist uncertainty before sending the start request.
            client.preview_start()
            started_at = time.monotonic()
            ready, keep_at = False, 0
            while not stop.is_set():
                stamp = time.monotonic()
                if process.poll() is not None or received['error']:
                    raise BackupError('预览组件已退出，请重新打开预览')
                if stamp - received['at'] > 12:
                    raise BackupError('相机视频流已中断，预览已结束；请检查无线连接')
                if stamp - keep_at >= 3:
                    client.keep_alive(); keep_at = stamp
                with urlopen(base + '/api/streams', timeout=3) as response: streams = json.load(response)
                stream = streams.get('preview', {})
                if not ready and any('H264' in media for p in stream.get('producers') or [] for media in p.get('medias', [])):
                    ready = True
                    emit_event('ready', url=base + '/stream.html?src=preview&mode=mse&media=video')
                if not ready and stamp - started_at > 20:
                    raise BackupError('未取得可播放的视频流，请检查相机模式后重新打开预览')
                stop.wait(.5)
        except Exception as exc:
            issue = str(exc) if isinstance(exc, BackupError) else '实时预览连接异常，已停止取流；请重新打开'
        finally:
            if started:
                try: client.preview_stop(); stopped = True
                except Exception:
                    issue = ((issue + '；') if issue else '') + '未能确认相机预览停止，请恢复连接后重新打开预览核验'
            relay_stop.set()
            if relay: relay.join(timeout=4)
            if connection: connection.close()
            if udp: udp.close()
            if process and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
            if issue: emit_event('error', message=issue)
            emit_event('closed', camera_stopped=stopped, reason=reason)


def main():
    stop = threading.Event()
    payload = {}
    for signum in (signal.SIGTERM, signal.SIGINT): signal.signal(signum, lambda *_: stop.set())
    try:
        payload = json.loads(sys.stdin.readline(16384))
        heartbeat = {'at': time.monotonic()}
        def owner():
            for _ in sys.stdin:
                heartbeat['at'] = time.monotonic()
            stop.set()
        def watchdog():
            while not stop.wait(1):
                if time.monotonic() - heartbeat['at'] > 15: stop.set()
        threading.Thread(target=owner, daemon=True).start()
        threading.Thread(target=watchdog, daemon=True).start()
        run(payload['source'], stop, cleanup_only=bool(payload.get('cleanup_only')))
        return 0
    except Exception as exc:
        emit('error', message=str(exc) if isinstance(exc, BackupError) else '预览组件无法启动，请检查连接或安装包')
        emit('closed', camera_stopped=not bool(payload.get('cleanup_only')), reason='预览未开启')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
