"""Periodic recovery snapshots, independent of media transfer and bot controls."""
import time


def serve(directory, stop):
    from .store import Store
    store = Store(directory)
    next_snapshot = 0
    try:
        while not stop.is_set():
            if time.monotonic() >= next_snapshot and not stop.is_set():
                from .recovery import snapshot_metadata
                try:
                    snapshot_metadata(store, stop)
                except Exception:
                    try:
                        previous = store.get_meta('metadata_backup', {})
                        store.set_meta('metadata_backup', {**previous, 'error': '元数据快照暂未完成，请检查数据盘；已有快照保留'})
                    except Exception:
                        pass
                next_snapshot = time.monotonic() + 60
            stop.wait(5)
    finally:
        store.close()
