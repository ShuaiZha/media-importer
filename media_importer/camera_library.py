"""Bounded, ephemeral camera reads executed at the importer's IO boundary.

No second transfer engine, disk cache or archive records. A short renewed lease
pauses source import/cleanup while browsing; the independent uploader continues.
"""
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import PurePosixPath

from .model import BackupError, ConfigError
from .source_connection import fresh


def backup_states(store, destination, assets):
    """Recorded outcomes only; remote and cache proofs belong to cleanup."""
    assets = list(assets)
    if not assets:
        return {}
    rows = {r['asset_id']: dict(r) for r in store.db.execute(
        'SELECT * FROM source_index WHERE destination=? AND source_type=? AND serial=?',
        (destination, assets[0].source_type, assets[0].serial))}
    rows.update({r['asset_id']: dict(r) for r in store.db.execute(
        'SELECT * FROM files WHERE destination=?', (destination,))})
    status = store.get_meta('status', {})
    stage = status.get('stages', {}).get('import') or {}
    from .status import age
    importing = (stage.get('current_file_id') if stage.get('phase') == 'downloading'
                 and age(stage.get('progress_updated_at')) < 30 else None)
    result = {}
    for asset in assets:
        row = rows.get(asset.identity, {})
        state = row.get('state')
        error = row.get('error') or row.get('source_cleanup_error') or row.get('cache_cleanup_error')
        key, label = 'pending', '未备份'
        if state == 'uploaded':
            key, label = 'archived', '已归档'
        elif error:
            key, label = 'error', '异常'
        elif state in ('cached', 'staged', 'publishing'):
            key, label = 'imported', '待归档'
        elif row.get('id') and row['id'] == importing:
            key, label = 'importing', '导入中'
        result[asset.identity] = {'state': key, 'label': label,
                                  'error': str(error) if error else None,
                                  'archive_path': row.get('remote_path') if state == 'uploaded' else None}
    return result


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
        self.choices = {}
        self.catalog = None
        self.observed_at = 0

    def selection(self, session, catalog, identifiers):
        with self.lock:
            if (session != self.session or catalog != self.catalog or not catalog
                    or time.monotonic() - self.observed_at > 1800):
                raise ConfigError('素材清单已失效，请刷新后重新选择')
            if (not isinstance(identifiers, list) or not identifiers or len(identifiers) > 1000
                    or any(not isinstance(value, str) for value in identifiers)
                    or len(set(identifiers)) != len(identifiers)
                    or not set(identifiers) <= self.choices.keys()):
                raise ConfigError('请选择当前清单中的文件')
            return [self.choices[value] for value in identifiers]

    def recorded_states(self, store, destination, session):
        with self.lock:
            assets = [pair[0] for pair in self.assets.values()] if session == self.session else []
        return backup_states(store, destination, assets)

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
            from .camera_coordinator import operation_reason
            reason = operation_reason(service.store, 'browse')
            if reason:
                raise ConfigError(reason)
            if not active(service.store):
                raise ConfigError('素材浏览会话已结束，请刷新素材')
            source = service.source.fork(service.stop, purpose='browse')
            if action == 'list':
                assets = service.source_snapshot(source)
                from .adapters import source_selection_groups
                result, mapping, choices = [], {}, {}
                for group in source_selection_groups(source, assets):
                    primary = next((a for a in group['members'] if a.metadata.get('primary')), None)
                    if not primary or PurePosixPath(primary.path).suffix.lower() not in ('.mp4', '.jpg', '.jpeg'):
                        continue
                    previews = [a for a in group['members'] if a.identity in group.get('preview_ids', [])
                                and PurePosixPath(a.path).suffix.lower() == '.lrv']
                    playback = previews[0] if len(previews) == 1 else primary
                    mapping[primary.identity] = (primary, playback)
                    choices[primary.identity] = group
                    result.append({'id': primary.identity, 'name': PurePosixPath(primary.path).name,
                                   'size': primary.size, 'created': primary.created,
                                   'kind': 'video' if primary.path.lower().endswith('.mp4') else 'photo',
                                   'playback_size': playback.size,
                                   'quality': '预览副本' if playback != primary else '原文件'})
                with self.lock:
                    self.session, self.assets = session, mapping
                    self.choices, self.catalog = choices, uuid.uuid4().hex
                    self.observed_at = time.monotonic()
                    catalog = self.catalog
                states = backup_states(service.store, service.destination_id, [pair[0] for pair in mapping.values()])
                for item in result:
                    item['backup'] = states[item['id']]
                future.set_result({'files': result, 'session_id': session, 'catalog': catalog})
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
