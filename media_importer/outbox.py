"""Durable message bookkeeping, with no network or presentation dependencies."""
import hashlib
import json
import time


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def _meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def retire_references(db, runs, file_ids):
    """Drop only message tracking whose durable task scope has been retired."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='notification_events'").fetchone():
        return
    db.executemany('DELETE FROM notification_events WHERE batch_id=? AND detached=0', ((key,) for key in runs))
    for entry in db.execute("SELECT key,value FROM meta WHERE key LIKE 'notification_progress_card:%' OR key LIKE 'event_message:completed:%'").fetchall():
        value = json.loads(entry['value'])
        if value.get('batch_id') in runs or any(entry['key'].endswith(':' + key) for key in runs):
            db.execute('DELETE FROM meta WHERE key=?', (entry['key'],))
    notice_row = db.execute("SELECT value FROM meta WHERE key='notification_state'").fetchone()
    if notice_row:
        notice = json.loads(notice_row[0])
        for key, episode in list(notice.get('problems', {}).items()):
            if episode.get('files') and set(episode['files']) <= file_ids:
                notice['problems'].pop(key)
                event_id = 'failed:' + notice.get('scope', '') + ':' + key + ':' + str(episode['since'])
                db.execute('DELETE FROM notification_events WHERE id=?', (event_id,))
                db.execute('DELETE FROM meta WHERE key=?', ('event_message:' + event_id,))
        db.execute("UPDATE meta SET value=? WHERE key='notification_state'", (json.dumps(notice),))


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS notification_events (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, batch_id TEXT,
        payload TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
        sent_at REAL, detached INTEGER NOT NULL DEFAULT 0)""")
    if 'detached' not in {r[1] for r in db.execute('PRAGMA table_info(notification_events)')}:
        db.execute('ALTER TABLE notification_events ADD COLUMN detached INTEGER NOT NULL DEFAULT 0')


def freeze_completion(db, config, run, rows, *, create):
    """Detach a bounded result from file details in the retirement transaction."""
    if run['selection'].startswith('selected:'):
        # Explicit selected imports update their own operation card, including
        # completion. They do not inherit the full-sync notification policy.
        return
    scope = _digest([config.destination_id, run['source_type'], run['serial']])
    key = 'completed:' + scope + ':' + run['id']
    saved = db.execute('SELECT * FROM notification_events WHERE id=?', (key,)).fetchone()
    if saved and saved['detached']:
        return  # Preserve an already-frozen result, retries and progress-message references.
    enabled = config.values['notifications']['enabled']
    if not enabled or saved and saved['state'] in ('suppressed', 'cancelled') or not saved and not create:
        return
    if not create and saved and saved['state'] == 'sent':
        return
    from .completion import build, project, source_errors
    if source_errors(db, dict(run)):
        return
    binding = _meta(db, 'source_selection', {})
    summary = project(build(dict(run), rows), config, binding)
    result, locations = summary['result'], summary['locations']
    previous = _meta(db, 'event_message:' + key, {})
    progress = [json.loads(r[0]) for r in db.execute("SELECT value FROM meta WHERE key LIKE 'notification_progress_card:%'")]
    pending_progress = [p for p in progress if p.get('batch_id') == run['id'] and p.get('message_id') and p.get('recipient')]
    if saved and saved['state'] == 'sent' and (not previous.get('id') or
            previous.get('result_signature') == _digest([result, locations]) and not any(not p.get('finished') for p in pending_progress)):
        return
    payload = {'result': result, 'locations': locations,
               'progress_messages': [{'id': p['message_id'], 'recipient': p['recipient']} for p in pending_progress],
               'update_message_id': previous.get('id'), 'update_recipient': previous.get('recipient')}
    db.execute('''INSERT INTO notification_events (id,kind,scope,batch_id,payload,state,created_at,detached)
        VALUES (?,'completed',?,?,?,'pending',?,1) ON CONFLICT(id) DO UPDATE SET
        payload=excluded.payload,detached=1,state='pending' ''',
        (key, scope, run['id'], json.dumps(payload, ensure_ascii=False), time.time()))
