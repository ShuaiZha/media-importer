"""Durable reporting rounds over existing file tasks; never schedule transfers."""
import json
import uuid

from .media import selected_file_ids
from .model import now


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS transfer_runs (
            id TEXT PRIMARY KEY, destination TEXT NOT NULL, source_type TEXT NOT NULL,
            serial TEXT NOT NULL, selection TEXT NOT NULL, created_at TEXT NOT NULL,
            finished_at TEXT, imported_at TEXT, historical INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS transfer_run_files (
            run_id TEXT NOT NULL, file_id TEXT NOT NULL,
            PRIMARY KEY (run_id, file_id)
        );
    """)
    with db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM meta WHERE key='reporting_rounds_v1'").fetchone():
            return
        groups = {}
        for row in db.execute('SELECT * FROM files ORDER BY rowid'):
            asset = json.loads(row['asset'])
            groups.setdefault((row['destination'], asset['source_type'], asset['serial']), []).append(dict(row))
        for (destination, source_type, serial), rows in groups.items():
            pending = {r['id'] for r in rows if r['state'] != 'uploaded'}
            # Include already finished members of unfinished old batches, but
            # do not invent a recent round from unrelated completed history.
            members = set(pending)
            for entry in db.execute('SELECT DISTINCT batch_id FROM batch_files WHERE file_id IN (SELECT id FROM files WHERE destination=? AND state<>?)',
                                    (destination, 'uploaded')):
                members.update(r[0] for r in db.execute('SELECT file_id FROM batch_files WHERE batch_id=?', (entry[0],)))
            for historical in (False, True):
                selected = [r for r in rows if (r['id'] not in members) == historical]
                if not selected:
                    continue
                identifier = uuid.uuid4().hex
                db.execute('INSERT INTO transfer_runs VALUES (?,?,?,?,?,?,?,?,?)',
                           (identifier, destination, source_type, serial, 'all', min(r['created_at'] for r in selected),
                            None, None, int(historical)))
                db.executemany('INSERT INTO transfer_run_files VALUES (?,?)', ((identifier, r['id']) for r in selected))
        db.execute("INSERT INTO meta VALUES ('reporting_rounds_v1','true')")


def reconcile(db, destination, source_type, serial, selection='all', preferred_id=None):
    """Resume the same round until its runnable files are all archived."""
    rows = [dict(r) for r in db.execute('SELECT * FROM files WHERE destination=? ORDER BY rowid', (destination,))
            if (lambda a: a['source_type'] == source_type and a['serial'] == serial)(json.loads(r['asset']))]
    selected = selected_file_ids(rows, selection)
    pending = [r['id'] for r in rows if r['state'] != 'uploaded' and
               (r['id'] in selected or r['state'] in ('cached', 'staged', 'publishing'))]
    current = db.execute('SELECT * FROM transfer_runs WHERE destination=? AND source_type=? AND serial=? AND selection=? AND historical=0 ORDER BY rowid DESC LIMIT 1',
                         (destination, source_type, serial, selection)).fetchone()
    if not pending:
        return current['id'] if current else None
    if not current or current['finished_at']:
        identifier = preferred_id or uuid.uuid4().hex
        if db.execute('SELECT 1 FROM transfer_runs WHERE id=?', (identifier,)).fetchone():
            identifier = uuid.uuid4().hex
        db.execute('INSERT INTO transfer_runs VALUES (?,?,?,?,?,?,NULL,NULL,0)',
                   (identifier, destination, source_type, serial, selection, now()))
    else:
        identifier = current['id']
    new = set(pending) - {r[0] for r in db.execute('SELECT file_id FROM transfer_run_files WHERE run_id=?', (identifier,))}
    if new:
        db.executemany('INSERT INTO transfer_run_files VALUES (?,?)', ((identifier, file_id) for file_id in new))
        db.execute('UPDATE transfer_runs SET imported_at=NULL WHERE id=?', (identifier,))
    return identifier


def archived(db, file_id):
    db.execute("""UPDATE transfer_runs SET finished_at=COALESCE(finished_at,?)
        WHERE historical=0 AND id IN (SELECT run_id FROM transfer_run_files WHERE file_id=?) AND NOT EXISTS (
            SELECT 1 FROM transfer_run_files rf JOIN files f ON f.id=rf.file_id
            WHERE rf.run_id=transfer_runs.id AND f.state<>'uploaded')""", (now(), file_id))


def imported(db, batch_id):
    db.execute("""UPDATE transfer_runs SET imported_at=COALESCE(imported_at,?)
        WHERE id IN (SELECT rf.run_id FROM transfer_run_files rf JOIN batch_files bf ON bf.file_id=rf.file_id WHERE bf.batch_id=?)
        AND NOT EXISTS (SELECT 1 FROM transfer_run_files rf JOIN files f ON f.id=rf.file_id
            WHERE rf.run_id=transfer_runs.id AND f.state NOT IN ('cached','staged','publishing','uploaded'))""", (now(), batch_id))


def read(db, destination, source_type, serial):
    result = []
    for row in db.execute('SELECT * FROM transfer_runs WHERE destination=? AND source_type=? AND serial=? ORDER BY rowid DESC',
                          (destination, source_type, serial)):
        result.append({**dict(row), 'file_ids': {r[0] for r in db.execute('SELECT file_id FROM transfer_run_files WHERE run_id=?', (row['id'],))}})
    return result
