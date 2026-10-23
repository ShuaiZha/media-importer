"""Camera resource policy. Archiving complete cache never needs this resource."""
import threading
from contextlib import nullcontext
from dataclasses import dataclass


@dataclass(frozen=True)
class Operation:
    resource: str
    read_only: bool = False
    stop: bool = False
    during_recording: bool = False
    finish_preview: bool = False


# Every entry point uses the same admission contract. Device capabilities,
# trusted identity and copy proofs remain independently enforced by their owners.
OPERATIONS = {
    'status': Operation('snapshot', read_only=True),
    'connect': Operation('radio'),
    'import': Operation('media'), 'delete': Operation('media'), 'browse': Operation('media'),
    'inspect': Operation('media', read_only=True, during_recording=True),
    'preview': Operation('picture'), 'livestream': Operation('picture'),
    'capture': Operation('control', read_only=True),
    'state': Operation('control', read_only=True), 'catalog': Operation('control', read_only=True),
    'stop_recording': Operation('control', stop=True, during_recording=True),
    'hilight': Operation('control', during_recording=True),
    **{name: Operation('control') for name in (
        'load_preset', 'set_parameter', 'start_timelapse', 'start_recording', 'photo', 'mode_video', 'mode_photo')},
    **{name: Operation('control', finish_preview=True) for name in ('preview_record', 'preview_photo', 'preview_timelapse')},
}


def state(store):
    from .camera_state import project
    import json
    keys = ('source_connection', 'source_link', 'camera_control', 'live_preview', 'camera_library', 'status')
    rows = store.db.execute('SELECT key,value FROM meta WHERE key IN (?,?,?,?,?,?)', keys)
    return project({row[0]: json.loads(row[1]) for row in rows})


def preview_state(store):
    return store.get_meta('live_preview', {})


def preview_active(store):
    from .camera_state import PICTURE_ACTIVE
    value = preview_state(store)
    return value.get('state') in PICTURE_ACTIVE or bool(value.get('cleanup_pending'))


def live_radio_owned(store):
    return preview_state(store).get('mode') == 'rtmp' and preview_active(store)


def operation_reason(store, operation, *, command=None, facts=None):
    """One resource policy; connection health and user preferences stay separate."""
    name = command if operation == 'capture' and command else operation
    if name not in OPERATIONS:
        raise ValueError('Unknown camera operation')
    contract = OPERATIONS[name]
    if contract.resource == 'snapshot':
        return None
    facts = state(store) if facts is None else facts
    picture = facts['picture']
    if contract.resource in ('radio', 'control') and picture['active'] and picture['mode'] == 'rtmp':
        return '请先结束有声直播并完成连接恢复'
    if contract.resource == 'radio':
        return None  # Reconnection is also required to verify an uncertain stop.
    if picture['recovering'] and not (contract.read_only or contract.stop):
        return '相机画面停止结果待核验；完整缓存可继续归档'
    if facts['control_uncertain'] and not (contract.read_only or contract.stop):
        return '相机操作结果待核验，请先刷新相机状态'
    if facts['recording'] and not (contract.read_only or contract.stop or contract.during_recording):
        return '相机正在拍摄或拍摄结果待核验，请先停止拍摄并核对状态'
    if picture['active']:
        if contract.resource in ('media', 'picture'):
            return ('有声直播' if picture['mode'] == 'rtmp' else '实时预览') + '进行中，相机读取与原片清理等待；完整缓存可继续归档'
        if not (contract.read_only or contract.stop or contract.finish_preview):
            return '请先结束实时预览，再执行相机操作'
    if contract.resource == 'media' and operation != 'browse' and facts['browsing']:
        return '正在浏览或回放相机素材，本地导入与原片清理等待；完整缓存可继续归档'
    return None


def source_wait_reason(store):
    return operation_reason(store, 'import')


@dataclass(frozen=True)
class DispatchResult:
    handled: bool = False
    selection: bool = False


def dispatch(service, settings, *, import_lane=False):
    """Use the same priority order both while idle and at an import boundary.

    Only the outer loop may start a selected transfer. The import lane may
    prepare its selection, but cannot recursively start another pipeline.
    """
    from .live_preview import process_one as preview
    from .capture import process_one as capture
    from .camera_library import process_one as library
    from .camera_operations import process_one as operation
    from .selected_imports import process_one as selection
    from .model import SourceBlocked

    def waiting():
        return service.activity.waiting('import') if import_lane and service.activity else nullcontext()

    with waiting():
        # An explicit stop outranks a newly queued picture. It must never wait
        # behind a preview which is itself waiting for recording to stop.
        if service.store.db.execute("SELECT 1 FROM capture_requests WHERE state IN ('queued','running') AND command='stop_recording' LIMIT 1").fetchone():
            if capture(service, settings):
                return DispatchResult(True)
        handled = bool(preview(service, settings))
    if import_lane and handled:
        return DispatchResult(True)
    captured = bool(capture(service, settings))
    if import_lane and captured:
        interrupt = getattr(service.control, 'source_interrupt', None)
        if interrupt:
            interrupt.clear()
        return DispatchResult(True)
    with waiting():
        library(service)
    if import_lane:
        reason = operation_reason(service.store, 'import')
        if reason:
            raise SourceBlocked(reason)
    if import_lane and not service.process_camera_operations:
        return DispatchResult(handled or captured)
    operated = bool(operation(service, settings))
    if import_lane and operated:
        return DispatchResult(True)
    selected = bool(selection(service, settings, browsing_only=import_lane))
    return DispatchResult(handled or captured or operated or selected, selected)


class CameraCoordinator:
    """One owned preview runner, launched only at an importer camera boundary."""
    def __init__(self, stop):
        self.stop = stop
        self.lock = threading.Lock()
        self.thread = None

    def start_preview(self, service, settings):
        from .live_preview import _run_session, KEY, update
        from .store import Store
        from types import SimpleNamespace
        directory = service.store.directory
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            state = service.store.get_meta(KEY, {})
            if state.get('state') != 'queued':
                return False

            def run():
                db = Store(directory)
                proxy = SimpleNamespace(store=db, stop=self.stop, control=service.control,
                                        report=lambda *a, **kw: None)
                try:
                    _run_session(proxy, settings)
                except Exception:
                    update(db, state['id'], state='error', url=None, local_url=None,
                           cleanup_pending=True, reason='预览工作线程异常退出，停止结果待核验')
                finally:
                    db.close()
                    changed = getattr(service.control, 'changed', None)
                    if changed: changed.set()

            self.thread = threading.Thread(target=run, name='camera-preview', daemon=True)
            self.thread.start()
            return True

    def close(self):
        if self.thread:
            self.thread.join(timeout=30)
        if self.thread and self.thread.is_alive():
            raise RuntimeError('Preview did not stop')
