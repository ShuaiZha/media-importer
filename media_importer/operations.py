"""Durable owner-requested checks; verification is exclusive with media work."""
import json

from .model import now, Cancelled


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS maintenance_requests (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT, target TEXT
    )""")


def enqueue(db, identifier, kind, target=None):
    if kind not in ("check", "verify_cache"):
        raise ValueError("不支持的检查操作")
    initialize(db)
    db.execute("INSERT OR IGNORE INTO maintenance_requests VALUES (?,?,'pending',?,?,NULL,?)",
               (identifier, kind, now(), now(), target))


def perform(store, control, stop):
    row = store.db.execute("SELECT * FROM maintenance_requests WHERE state='pending' ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        return False
    verify = row['kind'] == 'verify_cache'
    locked = False
    if verify:
        control.refresh()
        if control.enabled():
            result = {'state': 'error', 'message': '请先停止同步，再发起缓存核验；未改动文件。'}
        elif not control.work_guard.acquire(timeout=.5):
            return False
        else:
            locked = True
            result = None
    else:
        result = None
    try:
        with store.db:
            store.db.execute("UPDATE maintenance_requests SET state='running',updated_at=? WHERE id=?", (now(), row['id']))
        if result is None:
            control.refresh()
            if row['target'] and row['target'] != control.token:
                raise ValueError('检查对象的配置已经变化')
            if not verify:
                from .preflight import run
                result = run(control.settings, store, stop)
            else:
                from .config import Config
                from .adapters import create_source, create_destination
                from .service import Service
                config = Config.from_values(control.settings.read()['config'])
                source = create_source(config, stop)
                remote = create_destination(config, stop)
                try:
                    store.status(source_work=True, maintenance_running=True)
                    result = Service(config, store, source, remote, stop).verify_caches(
                        cancelled=lambda: control.allowed())
                    result['state'] = 'waiting' if result['pending'] else 'ok'
                finally:
                    store.status(source_work=False, maintenance_running=False)
                    remote.close()
    except Cancelled:
        result = {'state': 'waiting', 'message': '核验已中止，文件与原片保留；停止同步后可重新核验。'}
    except Exception:
        # Adapter exceptions may include connection values. Never echo them into chat.
        result = {'state': 'error', 'message': '检查未完成，请查看设备与存储状态；文件与原片保留。'}
    finally:
        if locked:
            control.work_guard.release()
    with store.db:
        store.db.execute("UPDATE maintenance_requests SET state='done',result=?,updated_at=? WHERE id=?",
                         (json.dumps(result, ensure_ascii=False), now(), row['id']))
    return True


def serve(directory, control, stop):
    from .store import Store
    store = Store(directory)
    try:
        with store.db:
            initialize(store.db)
            store.db.execute("UPDATE maintenance_requests SET state='done',result=?,updated_at=? WHERE state='running'",
                             (json.dumps({'state': 'waiting', 'message': '上次检查被重启中断，请重新发起；文件保留。'}, ensure_ascii=False), now()))
        while not stop.is_set():
            try:
                perform(store, control, stop)
            except Exception:
                pass  # A temporarily unavailable state disk must not kill the worker.
            stop.wait(1)
    finally:
        store.close()
