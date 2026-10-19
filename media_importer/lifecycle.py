"""Retire settled rounds; keep only live work and source-presence deduplication."""
import json
import time

from .model import now
from .storage import require_cache


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
    from .notifications import freeze_completion, initialize as notification_schema
    from .media import selected_file_ids
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        notification_schema(store.db)
        rows = store.files(destination)
        initialized = store.get_meta('once_clear_initialized', False)
        groups = store.groups(destination)
        runs = [dict(r) for r in store.db.execute('SELECT * FROM transfer_runs WHERE destination=?', (destination,))]
        members = {r['id']: {m[0] for m in store.db.execute('SELECT file_id FROM transfer_run_files WHERE run_id=?', (r['id'],))} for r in runs}
        relations = [json.loads(g['members']) for g in groups] + list(members.values())
        cleanup_selected = selected_file_ids(rows, config.backup['media_scope'], include_excluded=True)
        blocked = {r['file_id'] for r in store.temporary_items(destination)}
        def done(row):
            if row['state'] not in ('uploaded', 'excluded') or row['id'] in blocked:
                return False
            if row['state'] == 'uploaded' and not all(row[k] for k in ('remote_path', 'remote_identity', 'cache_cleared_at')):
                return False
            if row['cache_cleanup_error'] or row['cleanup'] == 'deleting':
                return False
            asset = store.asset(row)
            active_source = (asset.source_type, asset.serial) == (config.source['type'], config.source['serial'])
            if delete_source and active_source and row['id'] in cleanup_selected and (
                    row['cleanup'] not in ('deleted', 'absent') or row['source_cleanup_error']):
                return False
            cache = store.cached_path(row)
            # A disconnected cache is checked above; unknown complete files are never discarded.
            return not any(p.exists() or p.is_symlink() for p in (
                cache, cache.with_suffix('.download'), store.cache / '.receipts' / (row['id'] + '.json')))
        retired = [r for group in _components(rows, relations) if all(done(r) for r in group) for r in group]
        ids = {r['id'] for r in retired}
        if ids:
            for run in runs:
                keys = members[run['id']]
                if keys and keys <= ids:
                    freeze_completion(store.db, config, run, [r for r in retired if r['id'] in keys],
                                      create=bool(initialized and not run['historical']))
            for row in retired:
                asset = store.asset(row)
                if row['cleanup'] not in ('deleted', 'absent'):
                    store.db.execute('''INSERT OR REPLACE INTO source_index VALUES (?,?,?,?,?,?,?,?,?)''',
                        (destination, asset.identity, asset.source_type, asset.serial, row['state'], row['role'],
                         asset.size, row['remote_path'], row['remote_identity']))
                else:
                    store.db.execute('DELETE FROM source_index WHERE destination=? AND asset_id=?', (destination, asset.identity))
                if row['state'] == 'uploaded':
                    # One bounded proof preserves destination identity after history is gone.
                    store.db.execute("INSERT OR REPLACE INTO meta VALUES ('destination_anchor',?)",
                        (json.dumps({'destination': destination, 'path': row['remote_path'],
                                     'identity': json.loads(row['remote_identity']), 'size': asset.size}),))
            for table, column in (('cache_receipts', 'file_id'), ('batch_files', 'file_id'),
                                  ('transfer_run_files', 'file_id'), ('temporary_cleanup', 'file_id'), ('files', 'id')):
                store.db.executemany(f'DELETE FROM {table} WHERE {column}=?', ((key,) for key in ids))
            store.db.executemany('DELETE FROM source_groups WHERE id=?',
                ((g['id'],) for g in groups if set(json.loads(g['members'])) <= ids))
            store.db.execute('DELETE FROM batches WHERE id NOT IN (SELECT batch_id FROM batch_files)')
            store.db.execute('DELETE FROM transfer_runs WHERE id NOT IN (SELECT run_id FROM transfer_run_files)')
            closed = {r['id'] for r in runs if members[r['id']] and members[r['id']] <= ids}
            store.db.execute("DELETE FROM notification_events WHERE detached=0 AND batch_id IS NOT NULL AND batch_id NOT IN (SELECT id FROM transfer_runs)")
            for entry in store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'notification_progress_card:%' OR key LIKE 'event_message:completed:%'").fetchall():
                value = json.loads(entry['value'])
                if value.get('batch_id') in closed or any(entry['key'].endswith(':' + key) for key in closed):
                    store.db.execute('DELETE FROM meta WHERE key=?', (entry['key'],))
            notice = store.get_meta('notification_state', {})
            for key, episode in list(notice.get('problems', {}).items()):
                if episode.get('files') and set(episode['files']) <= ids:
                    notice['problems'].pop(key)
                    event_id = 'failed:' + notice.get('scope', '') + ':' + key + ':' + str(episode['since'])
                    store.db.execute('DELETE FROM notification_events WHERE id=?', (event_id,))
                    store.db.execute('DELETE FROM meta WHERE key=?', ('event_message:' + event_id,))
            if notice:
                store.db.execute("UPDATE meta SET value=? WHERE key='notification_state'", (json.dumps(notice),))
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
            store.db.execute("INSERT OR REPLACE INTO meta VALUES ('metadata_backup_reset',?)", (json.dumps(time.time_ns()),))
        store.db.execute("INSERT OR IGNORE INTO meta VALUES ('once_clear_initialized','true')")
        prune_transient(store.db)
    return len(retired)


def prune_transient(db):
    """No chat history; bounded IDs keep delayed callbacks from executing twice."""
    stamp = time.time()
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
