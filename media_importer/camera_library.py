"""Bounded, ephemeral camera reads executed at the importer's IO boundary.

No second transfer engine, disk cache or archive records. A short renewed lease
pauses source import/cleanup while browsing; the independent uploader continues.
"""
import queue
import threading
import time
from concurrent.futures import Future
from pathlib import PurePosixPath

from .model import BackupError, ConfigError
from .source_connection import fresh


def active(store):
    value = store.get_meta('camera_library', {})
    connection = store.get_meta('source_connection', {})
    return value.get('until', 0) > time.time() and value.get('session_id') == connection.get('id')


def touch(store, session, *, release=False):
    store.set_meta('camera_library', {'session_id': session, 'until': 0 if release else time.time() + 25})


def byte_range(header, size):
    import re
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header or '')
    if not match or not any(match.groups()) or size <= 0:
        raise ConfigError('需要有效的单段 Range 请求')
    left, right = match.groups()
    if len(left) > 16 or len(right) > 16:
        raise ConfigError('回放范围无效')
    start = int(left) if left else max(0, size - int(right))
    end = min(int(right), size - 1) if left and right else size - 1
    if not 0 <= start <= end < size:
        raise ConfigError('回放范围无效')
    return start, min(end - start + 1, 1024 * 1024)


class Library:
    def __init__(self, wake=None):
        self.requests = queue.Queue(maxsize=12)
        self.wake = wake or (lambda: None)
        self.lock = threading.Lock()
        self.session = None
        self.assets = {}

    def submit(self, session, action, identifier=None, header=None):
        future = Future()
        try:
            self.requests.put_nowait((future, time.monotonic() + 30, session, action, identifier, header))
        except queue.Full:
            raise ConfigError('素材读取繁忙，请稍后重试')
        self.wake()
        return future

    def process_one(self, service):
        try:
            future, deadline, session, action, identifier, header = self.requests.get_nowait()
        except queue.Empty:
            return False
        try:
            if not future.set_running_or_notify_cancel(): return True
            if time.monotonic() > deadline: raise ConfigError('素材读取等待超时，请重试')
            connection = service.store.get_meta('source_connection', {})
            if connection.get('id') != session or not fresh(connection) or connection.get('state') != 'approved':
                raise ConfigError('相机连接已变化，请刷新页面')
            from .camera_coordinator import preview_active
            if preview_active(service.store):
                raise ConfigError('请先结束实时预览，再浏览相机素材')
            if not active(service.store):
                raise ConfigError('素材浏览会话已结束，请刷新素材')
            source = service.source.fork(service.stop, purpose='browse')
            if action == 'list':
                assets = service.source_snapshot(source)
                from .adapters import source_selection_groups
                result, mapping = [], {}
                for group in source_selection_groups(source, assets):
                    primary = next((a for a in group['members'] if a.metadata.get('primary')), None)
                    if not primary or PurePosixPath(primary.path).suffix.lower() not in ('.mp4', '.jpg', '.jpeg'):
                        continue
                    previews = [a for a in group['members'] if a.identity in group.get('preview_ids', [])
                                and PurePosixPath(a.path).suffix.lower() == '.lrv']
                    playback = previews[0] if len(previews) == 1 else primary
                    mapping[primary.identity] = (primary, playback)
                    result.append({'id': primary.identity, 'name': PurePosixPath(primary.path).name,
                                   'size': primary.size, 'created': primary.created,
                                   'kind': 'video' if primary.path.lower().endswith('.mp4') else 'photo',
                                   'playback_size': playback.size,
                                   'quality': '预览副本' if playback != primary else '原文件'})
                with self.lock:
                    self.session, self.assets = session, mapping
                future.set_result({'files': result, 'session_id': session})
            else:
                with self.lock:
                    pair = self.assets.get(identifier) if session == self.session else None
                if not pair: raise ConfigError('素材清单已失效，请重新刷新')
                primary, playback = pair
                if action == 'thumbnail':
                    future.set_result((source.thumbnail(primary), 'image/jpeg', {}))
                elif action == 'media' and primary.path.lower().endswith('.mp4'):
                    offset, length = byte_range(header, playback.size)
                    body = source.read_media(primary, playback, offset, length)
                    future.set_result((body, 'video/mp4', {'Content-Range': f'bytes {offset}-{offset + length - 1}/{playback.size}',
                                                         'Accept-Ranges': 'bytes'}))
                else:
                    raise ConfigError('不支持此回放格式')
        except Exception as exc:
            if not future.done():
                future.set_exception(exc if isinstance(exc, (BackupError, ConfigError)) else ConfigError('素材读取失败，请检查相机连接'))
        finally:
            self.requests.task_done()
            if not self.requests.empty(): self.wake()
        return True


def process_one(service):
    library = getattr(service.control, 'library', None)
    return library.process_one(service) if library else False
