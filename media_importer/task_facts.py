"""Durable file outcomes shared by transfer results, snapshots and messages.

Observed cache/remote evidence is supplied by the caller; this module performs
no IO and never authorizes deletion or changes a task.
"""


def file_facts(row, *, cached=False, archive_verified=True):
    if not hasattr(row, 'get'):
        row = dict(row)
    excluded = row['state'] == 'excluded'
    archived = not excluded and row['state'] == 'uploaded' and archive_verified
    return {
        'excluded': excluded,
        'archived': archived,
        'archive_pending': not excluded and row['state'] == 'uploaded' and not archived,
        'imported': not excluded and (archived or cached),
        'cache_cleared': bool(row.get('cache_cleared_at')) and not row.get('cache_cleanup_error'),
        'source_deleted': row.get('cleanup') == 'deleted',
        'source_absent': row.get('cleanup') == 'absent',
        'source_uncertain': row.get('cleanup') == 'deleting',
    }


def archive_complete(rows, *, require_files=True):
    facts = [file_facts(row) for row in rows]
    return all(f['excluded'] or f['archived'] for f in facts) and (
        not require_files or any(f['archived'] for f in facts))
