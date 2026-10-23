"""Camera resource policy. Archiving complete cache never needs this resource."""
import threading
from contextlib import nullcontext
from dataclasses import dataclass


def preview_state(store):
    return store.get_meta('live_preview', {})


def preview_active(store):
    state = preview_state(store)
    return state.get('state') in {'queued', 'connecting', 'starting', 'ready', 'stopping'} or bool(state.get('cleanup_pending'))


def live_radio_owned(store):
    return preview_state(store).get('mode') == 'rtmp' and preview_active(store)


def operation_reason(store, operation):
    """One resource policy; connection health and user preferences stay separate."""
    state = preview_state(store)
    if operation in ('connect', 'capture'):
        if live_radio_owned(store):
            return '请先结束有声直播，再进行拍摄控制' if operation == 'capture' else '有声直播使用蓝牙会话；结束后恢复媒体连接'
        return None
    if operation not in ('import', 'delete', 'browse'):
        raise ValueError('Unknown camera operation')
    if state.get('cleanup_pending'):
        return '相机预览停止结果待核验；完整缓存可继续归档'
    if preview_active(store):
        return '实时预览进行中，本地导入与原片清理等待；完整缓存可继续归档'
    from .camera_library import active
    if operation != 'browse' and active(store):
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
