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
        if 'archive_previews' not in {r[1] for r in db.execute('PRAGMA table_info(transfer_runs)')}:
            # Existing rounds keep their original, all-files policy.
            db.execute('ALTER TABLE transfer_runs ADD COLUMN archive_previews INTEGER NOT NULL DEFAULT 1')
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
                db.execute('INSERT INTO transfer_runs (id,destination,source_type,serial,selection,created_at,finished_at,imported_at,historical) VALUES (?,?,?,?,?,?,?,?,?)',
                           (identifier, destination, source_type, serial, 'all', min(r['created_at'] for r in selected),
                            None, None, int(historical)))
                db.executemany('INSERT INTO transfer_run_files VALUES (?,?)', ((identifier, r['id']) for r in selected))
        db.execute("INSERT INTO meta VALUES ('reporting_rounds_v1','true')")


def reconcile(db, destination, source_type, serial, selection='all', preferred_id=None, archive_previews=True):
    """Resume the same round until its runnable files are all archived."""
    rows = [dict(r) for r in db.execute('SELECT * FROM files WHERE destination=? ORDER BY rowid', (destination,))
            if (lambda a: a['source_type'] == source_type and a['serial'] == serial)(json.loads(r['asset']))]
    selected = selected_file_ids(rows, selection)
    pending = [r['id'] for r in rows if r['state'] not in ('uploaded', 'excluded') and
               (r['id'] in selected or r['state'] in ('cached', 'staged', 'publishing'))]
    current = db.execute('SELECT * FROM transfer_runs WHERE destination=? AND source_type=? AND serial=? AND selection=? AND historical=0 ORDER BY rowid DESC LIMIT 1',
                         (destination, source_type, serial, selection)).fetchone()
    if not pending:
        if current:
            _include_previews(db, current['id'])
        return current['id'] if current else None
    if not current or current['finished_at']:
        identifier = preferred_id or uuid.uuid4().hex
        if db.execute('SELECT 1 FROM transfer_runs WHERE id=?', (identifier,)).fetchone():
            identifier = uuid.uuid4().hex
        db.execute('INSERT INTO transfer_runs VALUES (?,?,?,?,?,?,NULL,NULL,0,?)',
                   (identifier, destination, source_type, serial, selection, now(), int(archive_previews)))
    else:
        identifier = current['id']
    new = set(pending) - {r[0] for r in db.execute('SELECT file_id FROM transfer_run_files WHERE run_id=?', (identifier,))}
    if new:
        db.executemany('INSERT INTO transfer_run_files VALUES (?,?)', ((identifier, file_id) for file_id in new))
        db.execute('UPDATE transfer_runs SET imported_at=NULL WHERE id=?', (identifier,))
    _include_previews(db, identifier)
    return identifier


def _include_previews(db, identifier):
    # Retain exclusion/cleanup evidence alongside the original tasks without
    # treating an ignored preview as an imported or archived file.
    db.execute('''INSERT OR IGNORE INTO transfer_run_files
        SELECT ?, f.id FROM files f WHERE f.state='excluded' AND (
          f.group_id IN (SELECT p.group_id FROM files p JOIN transfer_run_files r ON r.file_id=p.id
                         WHERE r.run_id=?) OR
          f.group_id IS NULL AND f.id IN (SELECT b.file_id FROM batch_files b WHERE b.batch_id IN
                   (SELECT p.batch_id FROM batch_files p JOIN transfer_run_files r ON r.file_id=p.file_id WHERE r.run_id=?)))''',
        (identifier, identifier, identifier))


def archived(db, file_id):
    db.execute("""UPDATE transfer_runs SET finished_at=COALESCE(finished_at,?)
        WHERE historical=0 AND id IN (SELECT run_id FROM transfer_run_files WHERE file_id=?) AND NOT EXISTS (
            SELECT 1 FROM transfer_run_files rf JOIN files f ON f.id=rf.file_id
            WHERE rf.run_id=transfer_runs.id AND f.state NOT IN ('uploaded','excluded'))""", (now(), file_id))


def imported(db, batch_id):
    db.execute("""UPDATE transfer_runs SET imported_at=COALESCE(imported_at,?)
        WHERE id IN (SELECT rf.run_id FROM transfer_run_files rf JOIN batch_files bf ON bf.file_id=rf.file_id WHERE bf.batch_id=?)
        AND NOT EXISTS (SELECT 1 FROM transfer_run_files rf JOIN files f ON f.id=rf.file_id
            WHERE rf.run_id=transfer_runs.id AND f.state NOT IN ('cached','staged','publishing','uploaded','excluded'))""", (now(), batch_id))


def read(db, destination, source_type, serial):
    result = []
    for row in db.execute('SELECT * FROM transfer_runs WHERE destination=? AND source_type=? AND serial=? ORDER BY rowid DESC',
                          (destination, source_type, serial)):
        result.append({**dict(row), 'file_ids': {r[0] for r in db.execute('SELECT file_id FROM transfer_run_files WHERE run_id=?', (row['id'],))}})
    return result
