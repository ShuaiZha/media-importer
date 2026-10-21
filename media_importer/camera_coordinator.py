"""Camera resource policy. Archiving complete cache never needs this resource."""
import threading


def preview_state(store):
    return store.get_meta('live_preview', {})


def preview_active(store):
    state = preview_state(store)
    return state.get('state') in {'queued', 'connecting', 'starting', 'ready', 'stopping'} or bool(state.get('cleanup_pending'))


def source_wait_reason(store):
    state = preview_state(store)
    if state.get('cleanup_pending'):
        return '相机预览停止结果待核验；完整缓存可继续归档'
    if preview_active(store):
        return '实时预览进行中，本地导入与原片清理等待；完整缓存可继续归档'
    return None


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
