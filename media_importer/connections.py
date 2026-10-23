"""One active device, transport preference and bounded platform connection work."""
import os
import sys
import time

from .adapters import discover_source, source_link
from .source_connection import KEY, observe, fresh
from .model import now


def wireless_supported():
    return sys.platform == 'darwin' and os.environ.get('MEDIA_IMPORTER_RUNTIME') == 'native'


class ConnectionAttempt:
    """Cancel an automatic join when the user stops sync or changes transport."""
    def __init__(self, manager, mode, automatic, requested_until):
        self.manager, self.mode, self.automatic = manager, mode, automatic
        self.requested_until = requested_until
        self.checked_at, self.cancelled = 0, False

    def is_set(self):
        if self.cancelled or self.manager.stop.is_set():
            return True
        stamp = time.monotonic()
        if stamp >= self.checked_at:
            self.checked_at = stamp + .5
            from .camera_coordinator import live_radio_owned
            if live_radio_owned(self.manager.store):
                self.cancelled = True
                return True
            item = self.manager.settings.read()
            backup = (item.get('config') or {}).get('backup', {})
            self.cancelled = bool(item.get('error') or backup.get('source_transport') != self.mode or
                                  (not backup.get('enabled') if self.automatic else time.time() >= self.requested_until))
            if not self.automatic and not self.cancelled:
                self.cancelled = self.manager.store.get_meta('source_connect_request', {}).get('until', 0) < self.requested_until
        return self.cancelled


class ConnectionManager:
    def __init__(self, store, settings, stop):
        self.store, self.settings, self.stop = store, settings, stop
        self.scan_at = self.retry_at = 0
        self.request_seen = None

    def publish(self, phase, reason=None):
        old = self.store.get_meta('source_link', {})
        if old.get('phase') != phase or old.get('reason') != reason:
            failure = reason if phase == 'waiting_retry' else old.get('failure') if phase == 'connecting' else None
            self.store.set_meta('source_link', {'phase': phase, 'reason': reason, 'failure': failure, 'observed_at': now()})

    def connected(self, device):
        state = observe(self.store, device)
        pending = state.get('state') in ('pending', 'declined')
        self.publish('waiting_confirmation' if pending else 'connected',
                     '首次使用此相机，请在飞书确认；之后重连自动继续。' if pending else None)
        self.retry_at = 0

    def tick(self):
        from .camera_coordinator import operation_reason
        blocked = operation_reason(self.store, 'connect')
        if blocked:
            self.publish('livestream', blocked)
            return
        item = self.settings.read()
        if item.get('error'):
            self.publish('invalid_config', item['error'])
            return
        source, backup = item['config']['source'], item['config']['backup']
        request = self.store.get_meta('source_connect_request', {})
        request_key = request.get('id', request.get('until'))
        if request.get('until', 0) > time.time() and request_key != self.request_seen:
            self.request_seen = request_key
            self.retry_at = self.scan_at = 0
        mode = backup['source_transport']
        hint = item.get('source_hint', source)
        state = self.store.get_meta(KEY, {})
        old = state.get('device', {})
        status = self.store.get_meta('status', {})
        working = bool(status.get('source_work') or status.get('capture_work') or status.get('preview_work'))
        device = None
        if mode in ('auto', 'usb'):
            hints = [h for h in (hint, old, source) if h.get('transport') != 'wifi']
            device = discover_source(source['type'], hints, self.stop)
            if device:
                device.update(transport='usb')
        # A new preferred route waits at the current source-file boundary.
        if mode != 'usb' and wireless_supported() and (not device or (working and old.get('transport') == 'wifi')):
            linked = source_link(source['type'], 'probe', source.get('serial', ''), self.stop)
            if linked and (not device or working):
                device = linked
        if device:
            self.connected(device)
            return
        if state.get('state') == 'pending' and old.get('http_ready') is False and fresh(state):
            self.publish('waiting_confirmation', '已通过蓝牙识别相机，等待飞书确认')
            return
        state = observe(self.store, None)
        if working and fresh(state):
            return
        stamp = time.monotonic()
        if mode in ('auto', 'usb') and state.get('state') in ('searching', 'offline') and stamp >= self.scan_at:
            self.scan_at = stamp + 60
            device = discover_source(source['type'], [], self.stop, scan=True)
            if device:
                device.update(transport='usb')
                self.connected(device)
                return
        requested_until = self.store.get_meta('source_connect_request', {}).get('until', 0)
        requested = requested_until > time.time()
        needed = bool(backup['enabled'] or requested)
        if mode == 'usb' or not needed:
            self.publish('waiting_source', '等待相机连接' if mode == 'usb' else '同步关闭；可从「相机 → 连接相机」建立连接')
            return
        if not wireless_supported():
            self.publish('unsupported', '无线自动连接仅支持 Mac 原生版；此环境可继续使用 USB')
            return
        if stamp < self.retry_at or self.stop.is_set():
            return
        self.retry_at = stamp + backup['retry_seconds']
        self.publish('connecting', '正在连接相机蓝牙与 Wi-Fi；已有缓存继续上传')
        binding = self.store.get_meta('source_selection', {})
        serial = binding.get('serial_number', '') if binding.get('type') == source['type'] else ''
        attempt = ConnectionAttempt(self, mode, bool(backup['enabled']), requested_until)
        try:
            result = source_link(source['type'], 'connect' if serial else 'discover', serial, attempt)
            if attempt.is_set():
                self.publish('waiting_source', '自动连接已停止；任务与完整缓存保留。')
                return
            if result:
                self.connected(result)
                if not result.get('http_ready') and self.store.get_meta(KEY, {}).get('state') == 'approved':
                    self.publish('waiting_retry', '已识别相机，媒体连接尚未就绪；稍后自动重试。')
            else:
                self.publish('waiting_retry', '暂未发现可连接的相机；请保持相机开机并开启无线连接，程序会自动重试。')
        except Exception as exc:
            from .model import BackupError, Cancelled
            if isinstance(exc, Cancelled) and attempt.is_set():
                self.publish('waiting_source', '自动连接已停止；任务与完整缓存保留。')
                return
            if not isinstance(exc, (BackupError, OSError, ValueError)):
                raise
            # Adapters return safe errors, never SDK/OS diagnostics containing credentials.
            self.publish('waiting_retry', str(exc) if isinstance(exc, BackupError) else '无线连接暂不可用，等待重试')
        finally:
            if self.store.get_meta('source_link', {}).get('phase') == 'waiting_retry':
                self.retry_at = time.monotonic() + backup['retry_seconds']
