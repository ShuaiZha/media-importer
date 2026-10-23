"""Business completion facts, committed with file changes; no message rendering."""
import json
from pathlib import PurePosixPath

from .task_facts import archive_complete


def cleanup_summary(files, delete_source=False):
    """Observed outcomes, kept separate from the user's deletion preference."""
    previews = [f for f in files if f['state'] == 'excluded']
    files = [f for f in files if f['state'] != 'excluded']
    archived = [f for f in files if f['state'] == 'uploaded']
    ready_groups = {f.get('group_id') for f in files if f.get('group_id') and f.get('imported')}
    ready_groups -= {f.get('group_id') for f in files if not f.get('imported')}
    return {
        'preview_deleted': sum(f['cleanup'] == 'deleted' for f in previews),
        'preview_absent': sum(f['cleanup'] == 'absent' for f in previews),
        'preview_pending': sum(delete_source and f.get('cleanup_selected', True)
                               and (f.get('group_id') in ready_groups or bool(f.get('source_cleanup_error')))
                               and f['cleanup'] not in ('deleted', 'absent') for f in previews),
        'source_deleted': sum(f['cleanup'] == 'deleted' for f in files),
        'source_absent': sum(f['cleanup'] == 'absent' for f in files),
        'source_pending': sum(delete_source and f.get('selected', True) and f.get('imported')
                              and f['cleanup'] not in ('deleted', 'absent') for f in files),
        'source_not_deleted': sum(f['cleanup'] not in ('deleted', 'absent') for f in files),
        'cache_cleared': sum(f.get('cache_state') == 'missing' and not f.get('cache_cleanup_error') for f in archived),
        'cache_pending': sum(f.get('cache_state') in ('verified', 'unverified') or bool(f.get('cache_cleanup_error')) for f in archived),
        'cache_unknown': sum(f.get('cache_state') in (None, 'unavailable') and not f.get('cache_cleanup_error') for f in archived),
    }



def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS task_results (
        run_id TEXT PRIMARY KEY, destination TEXT NOT NULL, source_type TEXT NOT NULL,
        serial TEXT NOT NULL, selection TEXT NOT NULL, payload TEXT NOT NULL)""")
    # Bootstrap old unfinished work once. Readers never backfill or migrate.
    refresh(db, [r[0] for r in db.execute('SELECT id FROM transfer_runs')])


def build(run, rows):
    from .media import selected_file_ids
    rows = [dict(r) for r in rows]
    included = [r for r in rows if r['state'] != 'excluded']
    selected = (set() if run['selection'].startswith('selected:') else
                selected_file_ids(rows, run['selection'], include_excluded=True))
    observed = [{**r, 'imported': r['state'] in ('cached','staged','publishing','uploaded'),
                 'cleanup_selected': r['id'] in selected,
                 'cache_state': 'missing' if r.get('cache_cleared_at') else 'unverified' if r.get('cache_identity') else None} for r in rows]
    result = {'file_count': len(included),
              'byte_count': sum(json.loads(r['asset'])['size'] for r in included),
              'cleanup': cleanup_summary(observed, True),
              'ignored_preview_files': len(rows) - len(included)}
    return {'id': run['id'], 'historical': bool(run['historical']),
            'imported_at': run.get('imported_at'), 'created_at': run['created_at'],
            'complete': archive_complete(rows), 'file_ids': [r['id'] for r in rows],
            'result': result,
            'directories': sorted({str(PurePosixPath(r['remote_path']).parent) for r in included if r.get('remote_path')})}


def refresh(db, identifiers):
    for identifier in set(identifiers):
        saved = db.execute('SELECT * FROM transfer_runs WHERE id=?', (identifier,)).fetchone()
        if not saved:
            continue
        run = dict(saved)
        rows = db.execute('SELECT f.* FROM files f JOIN transfer_run_files r ON r.file_id=f.id WHERE r.run_id=?', (identifier,)).fetchall()
        payload = json.dumps(build(run, rows), ensure_ascii=False, sort_keys=True)
        db.execute("""INSERT INTO task_results VALUES (?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
            payload=excluded.payload WHERE task_results.payload<>excluded.payload""",
            (identifier, run['destination'], run['source_type'], run['serial'], run['selection'], payload))


def file_changed(db, file_id):
    refresh(db, [r[0] for r in db.execute('SELECT run_id FROM transfer_run_files WHERE file_id=?', (file_id,))])


def project(report, config, selection):
    """Resolve current cleanup policy over the recorded outcomes, without source IO."""
    from .source_connection import deletion_enabled
    from .paths import archive_location
    report = json.loads(json.dumps(report))
    if not deletion_enabled(config, selection):
        report['result']['cleanup'].update(source_pending=0, preview_pending=0)
    report['result']['destination_name'] = config.destination['name']
    report['locations'] = [archive_location(config.destination, directory) for directory in report.pop('directories', [])]
    return report


def read(db, config, *, selection='all'):
    binding = db.execute("SELECT value FROM meta WHERE key='source_selection'").fetchone()
    device = json.loads(binding[0]) if binding else {}
    return {row['run_id']: project(json.loads(row['payload']), config, device) for row in db.execute(
        """SELECT * FROM task_results WHERE destination=? AND source_type=? AND serial=?
        AND (selection=? OR json_extract(payload,'$.historical')=1)""",
        (config.destination_id, config.source['type'], config.source['serial'], selection))}
