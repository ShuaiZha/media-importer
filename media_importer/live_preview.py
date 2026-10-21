"""One explicitly owned preview session, started at a source IO boundary."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

from .model import BackupError, ConfigError, now
from .source_connection import fresh

KEY = 'live_preview'
ACTIVE = {'queued', 'connecting', 'starting', 'ready', 'stopping'}


def component():
    base = Path(sys.executable).parent / 'components' if getattr(sys, 'frozen', False) else (
        Path(__file__).resolve().parents[1] / '.build/components')
    path = base / 'go2rtc'
    if not path.is_file():
        raise ConfigError('安装包缺少预览组件，请更新 Mac 原生版；源码运行请先执行 python scripts/preview_bundle.py')
    return path


def available():
    from .connections import wireless_supported
    if not wireless_supported():
        raise ConfigError('实时预览目前仅支持 Mac 原生版与已适配相机的 Wi-Fi 连接')
    component()


def active(store):
    from .camera_coordinator import preview_active
    return preview_active(store)


def requested(store):
    return store.get_meta(KEY, {}).get('state') == 'queued'


def request(store, settings, owner, key):
    available()
    item = settings.read()
    if item.get('error'):
        raise ConfigError('配置不可用，请先检查配置')
    if item['config']['backup']['source_transport'] == 'usb':
        raise ConfigError('请先在「相机连接」选择自动或无线，使用独立供电连接相机 Wi-Fi')
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, owner + ':preview:' + key).hex
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        previous = store.get_meta(KEY, {})
        if previous.get('cleanup_pending'):
            previous.update(state='queued', stop_requested=True)
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (KEY, json.dumps(previous)))
            return previous
        if active(store) or previous.get('id') == identifier:
            return previous
        device = store.get_meta('source_selection', {})
        if not device.get('serial_number'):
            raise ConfigError('请先连接相机，并在飞书确认设备身份')
        from .adapters import source_preview_support
        support = source_preview_support({**device, 'transport': 'wifi'})
        if not support['supported']:
            raise ConfigError(support['reason'])
        state = {'id': identifier, 'owner': owner, 'state': 'queued', 'created': time.time(),
                 'serial': device['serial_number'], 'type': device['type'], 'observed_at': now(),
                 'viewer_token': uuid.uuid4().hex + uuid.uuid4().hex}
        # Do not use Store.set_meta inside this transaction (it commits).
        store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (KEY, json.dumps(state)))
    return state


def stop_request(store, owner, identifier):
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        state = store.get_meta(KEY, {})
        if state.get('id') != identifier or state.get('owner') != owner:
            raise ConfigError('此预览已结束或已被替换，请从菜单重新打开')
        if active(store):
            state.update(stop_requested=True, url=None, local_url=None, observed_at=now())
            if state.get('cleanup_pending'): state['state'] = 'queued'
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (KEY, json.dumps(state)))
    return state


def update(store, identifier, **values):
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        state = store.get_meta(KEY, {})
        if state.get('id') != identifier:
            return state
        state.update(values, observed_at=now())
        store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (KEY, json.dumps(state)))
    return state


def restore(store):
    state = store.get_meta(KEY, {})
    if active(store):
        # The orphan watchdog closes the old camera stream. Never restart it on boot.
        update(store, state['id'], state='queued', stop_requested=True,
               cleanup_pending=bool(state.get('source')), reason='后台曾重启，正在确认预览已结束', url=None, local_url=None)


def process_one(service, settings):
    manager = getattr(service.control, 'camera', None)
    if manager is not None:
        return manager.start_preview(service, settings)
    # Standalone isolated service callers retain a bounded synchronous owner.
    return _run_session(service, settings)


def _run_session(service, settings):
    store = service.store
    state = store.get_meta(KEY, {})
    if state.get('state') != 'queued':
        return False
    # An enabled transfer must start the two-lane pipeline before preview takes
    # the import lane, so already cached files keep uploading.
    if getattr(service, 'stage', None) is None and not getattr(service.control, 'camera', None):
        from .selected_imports import active as selection_active
        selection = selection_active(store.db)
        if service.allowed() or selection and selection['state'] in ('queued_transfer', 'running', 'waiting'):
            return False
    identifier = state['id']
    process, reader, error, final = None, None, None, None
    started = False
    try:
        if state.get('stop_requested') and not state.get('cleanup_pending'):
            update(store, identifier, state='stopped', reason='预览已结束', url=None)
            return True
        available()
        update(store, identifier, state='connecting', reason='正在核验相机连接', url=None)
        until = time.time() + 150
        # An uncertain stop also needs a connection, even with sync disabled.
        # A cancelled request without a started stream returned above already.
        store.set_meta('source_connect_request', {'until': until, 'preview_id': identifier})
        connection = {}
        while not service.stop.is_set():
            current = store.get_meta(KEY, {})
            if current.get('stop_requested') and not state.get('cleanup_pending'):
                break
            connection = store.get_meta('source_connection', {})
            device = connection.get('device', {})
            if (connection.get('state') == 'approved' and fresh(connection) and device.get('http_ready', True)
                    and device.get('serial_number') == state['serial'] and device.get('type') == state['type']):
                break
            if time.time() >= until:
                raise ConfigError('相机连接超时，请保持相机开机、开启无线连接后重新打开预览')
            service.stop.wait(.25)
        current = store.get_meta(KEY, {})
        if service.stop.is_set() or (current.get('stop_requested') and not state.get('cleanup_pending')):
            update(store, identifier, state='stopped', reason='预览已结束', url=None)
            return True
        device = connection['device']
        from .adapters import source_preview_support
        support = source_preview_support(device)
        if not support['supported']:
            raise ConfigError(support['reason'])
        source = {**settings.read()['config']['source'], 'url': device['url'], 'transport': device['transport'],
                  'serial': state['serial'], 'timeout_seconds': 3, 'request_attempts': 1}
        update(store, identifier, state='starting', source=source, reason='正在准备实时画面')
        store.status(preview_work=True)
        service.report('waiting_preview', '实时预览期间暂停相机导入与清理，完整缓存继续上传',
                       current_file=None, current_file_id=None, current_bytes=0, speed_bytes_per_second=0)
        command = ([sys.executable, '_worker', 'live-preview'] if getattr(sys, 'frozen', False) else
                   [sys.executable, '-m', 'media_importer.preview_worker'])
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        process.stdin.write(json.dumps({'source': source, 'cleanup_only': bool(state.get('cleanup_pending')), 'public_url': settings.read()['config']['preview']['public_url']}) + '\n')
        process.stdin.flush()
        started = True
        events = queue.Queue()
        def receive():
            try:
                for line in process.stdout:
                    try: events.put(json.loads(line))
                    except (ValueError, TypeError): pass
            finally:
                events.put(None)
        reader = threading.Thread(target=receive, daemon=True); reader.start()
        heartbeat, stopping_at = 0, None
        while True:
            current = store.get_meta(KEY, {})
            live_connection = store.get_meta('source_connection', {})
            stopping = (service.stop.is_set() or current.get('stop_requested') or
                        live_connection.get('id') != connection.get('id') or not fresh(live_connection))
            if stopping and process.stdin and not process.stdin.closed:
                stopping_at = time.monotonic()
                update(store, identifier, state='stopping', reason='正在结束预览', url=None)
                process.stdin.close()  # EOF also stops the child after a parent crash.
            elif process.poll() is None and time.monotonic() - heartbeat >= 2 and process.stdin and not process.stdin.closed:
                process.stdin.write('ping\n'); process.stdin.flush(); heartbeat = time.monotonic()
            if stopping_at and time.monotonic() - stopping_at > 20:
                raise ConfigError('预览组件未及时退出，正在回收；相机停止状态待核验')
            try: event = events.get(timeout=.25)
            except queue.Empty: continue
            if event is None: break
            if event.get('event') == 'ready' and not stopping:
                from .preview_access import links
                urls = links(current, settings.read()['config'])
                update(store, identifier, state='ready', reason='实时画面已就绪',
                       upstream=event['url'], webrtc=bool(event.get('webrtc')), **urls)
            elif event.get('event') == 'closed':
                final = event
            elif event.get('event') == 'error':
                error = event.get('message') or '预览组件异常退出，请重新打开预览'
        if reader: reader.join(timeout=1)
        if not final:
            error = error or '预览组件异常退出，停止结果待确认；请重新打开预览核验'
        clean = bool(final and final.get('camera_stopped'))
        update(store, identifier, state='error' if error or not clean else 'stopped', url=None, local_url=None,
               reason=error or (final or {}).get('reason') or '预览已结束', cleanup_pending=not clean)
    except (BackupError, OSError, ValueError, KeyError) as exc:
        safe = str(exc) if isinstance(exc, BackupError) else '预览组件暂不可用，请检查连接后重新打开'
        update(store, identifier, state='error', url=None, local_url=None, reason=safe,
               cleanup_pending=bool(state.get('cleanup_pending') or (started and not final)))
    finally:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            if store.get_meta('source_connect_request', {}).get('preview_id') == identifier:
                store.db.execute('DELETE FROM meta WHERE key=?', ('source_connect_request',))
        if process:
            if process.stdin and not process.stdin.closed: process.stdin.close()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait(timeout=3)
            # A killed worker may have left its owned go2rtc child behind.
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            if reader: reader.join(timeout=1)
            process.stdout.close()
        store.status(preview_work=False)
        if service.control:
            service.control.source_interrupt.clear()
            service.control.changed.set()
    return True
