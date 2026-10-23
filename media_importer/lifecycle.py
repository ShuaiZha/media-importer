"""Retire settled rounds; keep only live work and source-presence deduplication."""
import json
import time

from .model import now
from .storage import require_cache
from .task_facts import file_facts


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS source_index (
        destination TEXT NOT NULL, asset_id TEXT NOT NULL, source_type TEXT NOT NULL,
        serial TEXT NOT NULL, state TEXT NOT NULL, role TEXT NOT NULL,
        size INTEGER NOT NULL, remote_path TEXT, remote_identity TEXT,
        PRIMARY KEY(destination,asset_id))''')
    # A short-lived version kept completed rows and a growing settled_runs list.
    # Convert that marker once, without changing file tasks or replaying notices.
    # Actual retirement still requires settle() to prove all cleanup is finished.
    if db.execute("SELECT 1 FROM meta WHERE key='settled_runs'").fetchone():
        db.execute('BEGIN IMMEDIATE')
        saved = db.execute("SELECT value FROM meta WHERE key='settled_runs'").fetchone()
        if saved:
            keys = [(key,) for key in json.loads(saved[0])]
            db.executemany('UPDATE transfer_runs SET historical=1 WHERE id=?',
                           keys)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='notification_events'").fetchone():
                db.executemany("""UPDATE notification_events SET state='cancelled'
                    WHERE batch_id=? AND detached=0 AND state='pending'
                    AND kind IN ('progress','imported','completed')""", keys)
            db.execute("DELETE FROM meta WHERE key='settled_runs'")


def indexed(store, config, assets):
    """Call only after a successful, identity-checked full source inventory."""
    present = {a.identity for a in assets}
    with store.db:
        entries = [dict(r) for r in store.db.execute('''SELECT * FROM source_index
            WHERE destination=? AND source_type=? AND serial=?''',
            (config.destination_id, config.source['type'], config.source['serial']))]
        store.db.executemany('DELETE FROM source_index WHERE destination=? AND asset_id=?',
                             ((config.destination_id, r['asset_id']) for r in entries if r['asset_id'] not in present))
    return {r['asset_id']: r for r in entries if r['asset_id'] in present}


def restore_member(store, destination, asset, entry):
    """A fresh source observation can reactivate cleanup, never reupload a known original."""
    identifier = store.file_id(destination, asset)
    store.db.execute('''INSERT OR IGNORE INTO files
        (id,destination,asset_id,asset,state,role,remote_path,remote_identity,
         cache_cleared_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (identifier, destination, asset.identity, json.dumps(asset.to_dict()), entry['state'], entry['role'],
         entry['remote_path'], entry['remote_identity'], now() if entry['state'] == 'uploaded' else None, now(), now()))


def _components(rows, relations):
    """Keep a round/group intact while any of its members still needs work."""
    parent = {r['id']: r['id'] for r in rows}
    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    for keys in relations:
        keys = [key for key in keys if key in parent]
        for key in keys[1:]:
            parent[root(key)] = root(keys[0])
    result = {}
    for row in rows:
        result.setdefault(root(row['id']), []).append(row)
    return list(result.values())


def retirement_ready(store, row, *, blocked=(), source_cleanup_required=False):
    """A shared retirement boundary, separate from archive completion."""
    facts = file_facts(row)
    if not (facts['archived'] or facts['excluded']) or row['id'] in blocked:
        return False
    if facts['archived'] and not all(row.get(k) for k in ('remote_path', 'remote_identity', 'cache_cleared_at')):
        return False
    if row.get('cache_cleanup_error') or facts['source_uncertain']:
        return False
    if source_cleanup_required and (not (facts['source_deleted'] or facts['source_absent']) or row.get('source_cleanup_error')):
        return False
    cache = store.cached_path(row)
    return not any(p.exists() or p.is_symlink() for p in (
        cache, cache.with_suffix('.download'), store.cache / '.receipts' / (row['id'] + '.json')))


def retire_rows(store, destination, rows):
    """Retire only these proved rows, within the caller's result transaction.

    The caller freezes its full-sync notice or selected-operation result first.
    Other task scopes, incomplete groups and detached notices remain intact.
    """
    if not store.db.in_transaction:
        raise ValueError('Retirement requires an active transaction')
    if not rows:
        return
    if any(row['destination'] != destination for row in rows):
        raise ValueError('Retirement scope belongs to another destination')
    ids = {r['id'] for r in rows}
    db = store.db
    batches, runs, groups = set(), set(), set()
    for row in rows:
        key = row['id']
        batches.update(r[0] for r in db.execute('SELECT batch_id FROM batch_files WHERE file_id=?', (key,)))
        runs.update(r[0] for r in db.execute('SELECT run_id FROM transfer_run_files WHERE file_id=?', (key,)))
        if row['group_id']:
            groups.add(row['group_id'])
        asset = store.asset(row)
        facts = file_facts(row)
        if not (facts['source_deleted'] or facts['source_absent']):
            db.execute('INSERT OR REPLACE INTO source_index VALUES (?,?,?,?,?,?,?,?,?)',
                       (destination, asset.identity, asset.source_type, asset.serial, row['state'], row['role'],
                        asset.size, row['remote_path'], row['remote_identity']))
        else:
            db.execute('DELETE FROM source_index WHERE destination=? AND asset_id=?', (destination, asset.identity))
        if facts['archived']:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('destination_anchor',?)",
                       (json.dumps({'destination': destination, 'path': row['remote_path'],
                                    'identity': json.loads(row['remote_identity']), 'size': asset.size}),))
    for table, column in (('cache_receipts', 'file_id'), ('batch_files', 'file_id'),
                          ('transfer_run_files', 'file_id'), ('temporary_cleanup', 'file_id'), ('files', 'id')):
        db.executemany(f'DELETE FROM {table} WHERE {column}=?', ((key,) for key in ids))
    for key in groups:
        saved = db.execute('SELECT members FROM source_groups WHERE id=?', (key,)).fetchone()
        if saved and not any(db.execute('SELECT 1 FROM files WHERE id=?', (member,)).fetchone()
                             for member in json.loads(saved[0])):
            db.execute('DELETE FROM source_groups WHERE id=?', (key,))
    for key in batches:
        if not db.execute('SELECT 1 FROM batch_files WHERE batch_id=?', (key,)).fetchone():
            db.execute('DELETE FROM batches WHERE id=?', (key,))
            if store.get_meta('last_batch') == key:
                db.execute("DELETE FROM meta WHERE key='last_batch'")
    closed = set()
    for key in runs:
        if not db.execute('SELECT 1 FROM transfer_run_files WHERE run_id=?', (key,)).fetchone():
            db.execute('DELETE FROM task_results WHERE run_id=?', (key,))
            db.execute('DELETE FROM transfer_runs WHERE id=?', (key,))
            closed.add(key)
    from .outbox import retire_references
    retire_references(db, closed, ids)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('metadata_backup_reset',?)", (json.dumps(time.time_ns()),))


def settle(service):
    """Called with the daemon work guard, after both transfer lanes have stopped.

    This function never deletes source media, cache media, or remote files.
    Retirement and its detached completion notification commit together.
    """
    store, config = service.store, service.config
    if service.control:
        service.control.refresh()
        if service.control.error or service.control.token != service.control.applied_token:
            return 0
        from .config import Config
        values = service.control.item['config']
        config = Config.from_values({**values, 'destination': {**values['destination'], 'archive_id': service.destination_id}})
    require_cache(store)
    destination = service.destination_id
    from .source_connection import deletion_enabled
    delete_source = deletion_enabled(config, store.get_meta('source_selection', {}))
    from .outbox import freeze_completion
    from .media import selected_file_ids
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        rows = store.files(destination)
        initialized = store.get_meta('once_clear_initialized', False)
        groups = store.groups(destination)
        runs = [dict(r) for r in store.db.execute('SELECT * FROM transfer_runs WHERE destination=?', (destination,))]
        members = {r['id']: {m[0] for m in store.db.execute('SELECT file_id FROM transfer_run_files WHERE run_id=?', (r['id'],))} for r in runs}
        relations = [json.loads(g['members']) for g in groups] + list(members.values())
        cleanup_selected = selected_file_ids(rows, config.backup['media_scope'], include_excluded=True)
        blocked = {r['file_id'] for r in store.temporary_items(destination)}
        def done(row):
            asset = store.asset(row)
            active_source = (asset.source_type, asset.serial) == (config.source['type'], config.source['serial'])
            return retirement_ready(store, row, blocked=blocked, source_cleanup_required=(
                delete_source and active_source and row['id'] in cleanup_selected))
        retired = [r for group in _components(rows, relations) if all(done(r) for r in group) for r in group]
        ids = {r['id'] for r in retired}
        if ids:
            for run in runs:
                keys = members[run['id']]
                if keys and keys <= ids:
                    freeze_completion(store.db, config, run, [r for r in retired if r['id'] in keys],
                                      create=bool(initialized and not run['historical']))
            retire_rows(store, destination, retired)
            state = store.get_meta('status', {})
            if not store.db.execute('SELECT 1 FROM batches WHERE id=?', (state.get('batch_id'),)).fetchone():
                state.update(batch_id=None, phase='idle', message='本轮已收尾，等待新素材', current_file=None, current_file_id=None,
                             current_bytes=0, current_total_bytes=0, speed_bytes_per_second=0, eta_seconds=None)
                state['inventory'] = {**state.get('inventory', {}), 'batch_id': None, 'new_files': 0}
                state['stages'] = {k: {**v, 'phase': 'idle', 'current_file': None, 'current_file_id': None,
                                     'current_bytes': 0, 'current_total_bytes': 0, 'issue': None,
                                     'speed_bytes_per_second': 0, 'eta_seconds': None}
                                   for k, v in state.get('stages', {}).items()}
                store.db.execute("INSERT OR REPLACE INTO meta VALUES ('status',?)", (json.dumps(state),))
                store.db.execute("DELETE FROM meta WHERE key='last_batch'")
            store.db.execute("DELETE FROM meta WHERE key='last_result'")
            if (store.get_meta('last_error') or {}).get('file') in {store.asset(r).path for r in retired}:
                store.db.execute("DELETE FROM meta WHERE key='last_error'")
        store.db.execute("INSERT OR IGNORE INTO meta VALUES ('once_clear_initialized','true')")
        prune_transient(store.db)
    return len(retired)


def prune_transient(db):
    """No chat history; bounded IDs keep delayed callbacks from executing twice."""
    stamp = time.time()
    from .camera_operations import prune
    prune(db)
    from .capture import prune as prune_capture
    prune_capture(db)
    from .selected_imports import prune as prune_selected
    prune_selected(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'bot_actions' in tables:
        db.execute('DELETE FROM bot_actions WHERE created<?', (stamp - 86400,))
    if 'bot_jobs' in tables:
        if 'maintenance_requests' in tables:
            db.execute("""DELETE FROM maintenance_requests WHERE state='reported' AND EXISTS (
                SELECT 1 FROM bot_jobs j WHERE j.id='maintenance-result:' || maintenance_requests.id
                AND j.state='done' AND j.created<?)""", (stamp - 300,))
        # Reply receipts are only needed while a command or its follow-up is live.
        for row in db.execute("SELECT id,scope FROM bot_jobs WHERE state IN ('done','error') AND created<?", (stamp - 86400,)).fetchall():
            db.execute('DELETE FROM meta WHERE key=?', ('bot_reply:' + row['scope'] + ':' + row['id'],))
        db.execute("DELETE FROM bot_jobs WHERE state IN ('done','error') AND created<?", (stamp - 86400,))
        db.execute("UPDATE bot_jobs SET payload='{}',response=NULL,error=NULL WHERE state IN ('done','error') AND created<? AND (payload<>'{}' OR response IS NOT NULL OR error IS NOT NULL)", (stamp - 300,))
    for row in db.execute("SELECT id FROM notification_events WHERE state<>'pending' AND batch_id IS NULL AND created_at<?", (stamp - 86400,)).fetchall():
        db.execute("DELETE FROM meta WHERE key IN (?,?)", ('event_message:' + row['id'], 'recovery_message:' + row['id']))
    db.execute("DELETE FROM notification_events WHERE state<>'pending' AND batch_id IS NULL AND created_at<?", (stamp - 86400,))
    db.execute("UPDATE notification_events SET payload='{}' WHERE state<>'pending' AND batch_id IS NULL AND created_at<? AND payload<>'{}'", (stamp - 300,))
