"""One logical archive per data directory, independent of connection addresses.

Existing task keys are retained verbatim. A changed connection must prove that it
still reaches the recorded archive before any remote mutation is allowed.
"""
import json

from .adapters import destination_identity
from .config import Config
from .model import DestinationError, now

KEY = 'destination_binding'


def read(db):
    row = db.execute('SELECT value FROM meta WHERE key=?', (KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def identity(db, config):
    binding = read(db)
    return binding['id'] if binding else config.destination_id


def bind(store, config):
    """Additive migration; no file, group, round or notification IDs change."""
    binding = read(store.db)
    requested = config.values.get('archive', {}).get('id')
    if requested and (not binding or requested != binding['id']):
        # The JSON write is the durable switch intent. Commit the binding only
        # after proving the old work is settled; a restart can safely repeat it.
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            assert_settled(store)
            binding = {'id': requested, 'connection': destination_identity(config.destination), 'bound_at': now()}
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (KEY, json.dumps(binding)))
    if not binding:
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            binding = read(store.db)
            if binding:
                return bind(store, config)
            connection = destination_identity(config.destination)
            previous = store.get_meta('status', {}).get('active_destination')
            known = [r[0] for r in store.db.execute('SELECT DISTINCT destination FROM files')]
            if previous not in known:
                previous = connection if connection in known else known[0] if len(known) == 1 else None
            if known and previous is None:
                raise DestinationError('历史记录包含多个目的地，无法自动选择；请恢复原连接后重试', code='destination_identity')
            binding = {'id': previous or connection, 'connection': previous or connection, 'bound_at': now()}
            store.db.execute('INSERT INTO meta VALUES (?,?)', (KEY, json.dumps(binding)))
    values = {**config.values, 'destination': {**config.destination, 'archive_id': binding['id']}}
    return Config.from_values(values)


def verify_connection(store, config, remote, *, accept=True, require_proof=False):
    binding = read(store.db)
    connection = destination_identity(config.destination)
    if not binding or binding['connection'] == connection:
        return
    # Metadata only. A filename or size match is never enough to adopt another server.
    # A matching archive anchor is enough, unless a remote temporary file is still unfinished.
    rows = store.db.execute("""SELECT remote_path,remote_identity,asset FROM files
        WHERE destination=? AND state='uploaded'""", (binding['id'],)).fetchall()
    anchor = store.get_meta('destination_anchor')
    if anchor and anchor.get('destination') != binding['id']:
        anchor = None
    pending_cleanup = store.db.execute("""SELECT 1 FROM temporary_cleanup WHERE destination=?
        AND kind='remote' AND state<>'done' LIMIT 1""", (binding['id'],)).fetchone()
    if anchor:
        item = remote.stat(anchor['path'])
        actual = item.get('identity') if item else None
        if item and actual is None and hasattr(remote, 'file_identity'):
            actual = remote.file_identity(anchor['path'])
        if not (item and not item['is_directory'] and item['size'] == anchor['size'] and actual == anchor['identity']):
            raise DestinationError('新连接与已绑定存储身份不符，请检查共享目录；任务与缓存保留', code='destination_identity')
        if not pending_cleanup:
            if accept:
                _accept(store, binding, connection)
            return
    remote_work = store.db.execute("""SELECT 1 FROM files WHERE destination=?
        AND (remote_path IS NOT NULL OR temp_path IS NOT NULL OR remote_identity IS NOT NULL) LIMIT 1""", (binding['id'],)).fetchone()
    if not rows and not anchor and (remote_work or pending_cleanup or require_proof):
        raise DestinationError('连接已变化，但没有可核验的归档；请恢复原连接。切换新位置请先完成收尾，再使用飞书归档位置设置',
                               code='destination_identity')
    for row in rows:
        item = remote.stat(row['remote_path']) if row['remote_path'] else None
        expected = json.loads(row['remote_identity'] or 'null')
        actual = item.get('identity') if item else None
        if item and actual is None and hasattr(remote, 'file_identity'):
            actual = remote.file_identity(row['remote_path'])
        if not (item and expected and not item['is_directory'] and
                item['size'] == json.loads(row['asset'])['size'] and actual == expected):
            raise DestinationError('新连接与原归档身份不符；上传和远端清理已暂停，请恢复原连接或检查共享目录',
                                   code='destination_identity')
    if accept:
        _accept(store, binding, connection)


def assert_settled(store):
    """Changing the archive never abandons tasks or moves complete cache files."""
    from .storage import inspect_cache
    cache = inspect_cache(store.cache, store.get_meta('cache_identity'))
    if not cache['available']:
        raise DestinationError(cache['reason'])
    if store.db.execute('SELECT 1 FROM files LIMIT 1').fetchone() or store.temporary_items():
        raise DestinationError('仍有任务或清理事项，请完成收尾后再切换归档位置；现有任务与缓存保留')
    if any(p.is_file() or p.is_symlink() for pattern in ('*.bin', '*.download', '.receipts/*.json')
           for p in store.cache.glob(pattern)):
        raise DestinationError('缓存目录仍有素材或恢复凭据，请完成归档或恢复后再切换位置')


def _accept(store, binding, connection):
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        latest = read(store.db)
        if latest != binding:
            # Another worker may have completed the same verification.
            if latest['connection'] == connection:
                return
            raise DestinationError('目的地连接核验发生变化，请重试', code='destination_identity')
        binding.update(connection=connection, verified_at=now())
        store.db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(binding), KEY))
