import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .model import Asset, BackupError, now


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        from .deployment import assert_runtime
        assert_runtime(self.directory)
        if os.environ.get('MEDIA_IMPORTER_RUNTIME') == 'native' and not self.directory.is_dir():
            raise BackupError('原生数据目录不可访问；等待原磁盘，不创建替代目录')
        self.directory.mkdir(parents=True, exist_ok=True)
        from .deployment import cache_path
        self.cache = cache_path(self.directory)
        from .storage import requires_mount
        if not requires_mount() and os.environ.get('MEDIA_IMPORTER_RUNTIME') != 'native':
            self.cache.mkdir(exist_ok=True)
        self.db = sqlite3.connect(str(self.directory / "state.sqlite3"), timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 5):
            self.db.close()
            raise BackupError("数据库格式不受支持；请先离线迁移到版本 5")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, destination TEXT NOT NULL, asset_id TEXT NOT NULL,
                asset TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                remote_path TEXT, temp_path TEXT,
                cleanup TEXT NOT NULL DEFAULT 'pending', error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                remote_identity TEXT, cache_identity TEXT, group_id TEXT, source_cleanup_error TEXT, error_stage TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, cache_cleanup_error TEXT
            );
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, destination TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, state TEXT NOT NULL, imported_at TEXT
            );
            CREATE TABLE IF NOT EXISTS batch_files (
                batch_id TEXT NOT NULL, file_id TEXT NOT NULL,
                PRIMARY KEY(batch_id, file_id)
            );
            CREATE TABLE IF NOT EXISTS source_groups (
                id TEXT PRIMARY KEY, destination TEXT NOT NULL, members TEXT NOT NULL,
                safe INTEGER NOT NULL, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS temporary_cleanup (
                id TEXT PRIMARY KEY, file_id TEXT NOT NULL, destination TEXT NOT NULL,
                kind TEXT NOT NULL, path TEXT NOT NULL, identity TEXT,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT, created_at TEXT NOT NULL,
                UNIQUE(kind, destination, path)
            );
            CREATE INDEX IF NOT EXISTS files_destination_state ON files(destination, state);
            CREATE TABLE IF NOT EXISTS cache_receipts (
                file_id TEXT PRIMARY KEY, imported_at TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute("PRAGMA user_version=5")
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(files)')}
            for name, definition in {'role': "TEXT NOT NULL DEFAULT 'original'", 'source_deleted_at': 'TEXT',
                                     'source_absent_at': 'TEXT', 'cache_cleared_at': 'TEXT',
                                     'error_code': 'TEXT', 'retry_at': 'REAL NOT NULL DEFAULT 0'}.items():
                if name not in columns:
                    self.db.execute(f'ALTER TABLE files ADD COLUMN {name} {definition}')
        from .runs import initialize
        initialize(self.db)
        from .lifecycle import initialize as initialize_lifecycle
        with self.db:
            initialize_lifecycle(self.db)

    def close(self):
        self.db.close()

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("""INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value
                WHERE meta.value<>excluded.value""", (key, json.dumps(value, ensure_ascii=False)))

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def status(self, **updates):
        # The device monitor and worker own separate SQLite connections.
        # Serialize the read/merge/write so connection updates cannot erase progress.
        current = self.get_meta('status', {})
        if all(key in current and current[key] == value for key, value in updates.items()):
            return current
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            status = self.get_meta("status", {})
            status.update(updates)
            status["updated_at"] = now()
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('status', ?)", (json.dumps(status, ensure_ascii=False),))
        return status

    @staticmethod
    def file_id(destination, asset):
        return uuid.uuid5(uuid.NAMESPACE_URL, destination + ":" + asset.identity).hex

    def stage_status(self, stage, **updates):
        # Each transfer owns its connection. Merge within the same write transaction.
        current = self.get_meta('status', {}).get('stages', {}).get(stage, {})
        if all(key in current and current[key] == value for key, value in updates.items()):
            return
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            status = self.get_meta("status", {})
            progress = status.setdefault("stages", {}).setdefault(stage, {})
            progress.update(updates)
            progress["updated_at"] = now()
            status["updated_at"] = now()
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('status', ?)",
                            (json.dumps(status, ensure_ascii=False),))

    def cache_bytes(self):
        return cache_bytes(self.cache)

    def invalidate_inventory(self, reason):
        previous = self.get_meta('status', {}).get('inventory', {})
        if previous.get('verified') is False and previous.get('invalidated_reason') == reason:
            return
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            status = self.get_meta("status", {})
            status.setdefault("inventory", {}).update(verified=False, invalidated_reason=reason)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('status', ?)",
                            (json.dumps(status, ensure_ascii=False),))

    def batch(self, destination, assets, media_scope='all', *, preview_ids=(), archive_previews=True):
        fingerprint = destination + ":" + ",".join(sorted(a.identity for a in assets))
        batch_id = uuid.uuid5(uuid.NAMESPACE_URL, fingerprint).hex
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO batches (id,destination,created_at,updated_at,state) VALUES (?, ?, ?, ?, 'pending')", (batch_id, destination, now(), now()))
            self.db.execute("UPDATE batches SET updated_at=? WHERE id=?", (now(), batch_id))
            for asset in assets:
                file_id = self.file_id(destination, asset)
                preview = asset.identity in preview_ids
                self.db.execute("INSERT OR IGNORE INTO files (id, destination, asset_id, asset, created_at, updated_at, role, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (file_id, destination, asset.identity, json.dumps(asset.to_dict()), now(), now(),
                                 'preview' if preview else 'original', 'excluded' if preview and not archive_previews else 'pending'))
                self.db.execute("INSERT OR IGNORE INTO batch_files VALUES (?, ?)", (batch_id, file_id))
            if assets:
                from .runs import reconcile
                reconcile(self.db, destination, assets[0].source_type, assets[0].serial, media_scope, batch_id, archive_previews)
        self.set_meta("last_batch", batch_id)
        return batch_id

    def files(self, destination=None, batch_id=None, *, work=False):
        if batch_id:
            rows = self.db.execute("SELECT f.* FROM files f JOIN batch_files b ON f.id=b.file_id WHERE b.batch_id=? ORDER BY f.rowid", (batch_id,)).fetchall()
        elif destination:
            pending = " AND (state NOT IN ('uploaded','excluded') OR (state='uploaded' AND cache_cleared_at IS NULL))" if work else ''
            rows = self.db.execute("SELECT * FROM files WHERE destination=?" + pending + " ORDER BY rowid", (destination,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM files ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def get(self, file_id):
        row = self.db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def asset(row):
        return Asset.from_dict(json.loads(row["asset"]))

    def update(self, file_id, **fields):
        allowed = {"state", "remote_path", "temp_path", "cleanup", "error",
                   "remote_identity", "cache_identity", "group_id", "source_cleanup_error", "error_stage", "attempts",
                   "cache_cleanup_error", "cache_cleared_at", "error_code", "retry_at"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid state update")
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.get(file_id)
            if previous is None:
                raise ValueError('Unknown file')
            if fields.get('state') in ('cached', 'uploaded') and 'error' in fields and fields['error'] is None:
                fields.update(error_code=None, retry_at=0)
            # Absence is a current observation; it cannot erase a confirmed
            # deletion fact. Old "absent" rows cannot be retroactively inferred.
            if fields.get('cleanup') == 'absent':
                if not previous['source_absent_at']:
                    fields['source_absent_at'] = now()
                if previous['cleanup'] in ('deleting', 'deleted') or previous['source_deleted_at']:
                    fields['cleanup'] = 'deleted'
            if fields.get('cleanup') == 'deleted' and not previous['source_deleted_at'] and previous['cleanup'] != 'deleted':
                fields['source_deleted_at'] = now()
            if all(previous.get(k) == v for k, v in fields.items()):
                return
            fields["updated_at"] = now()
            self.db.execute("UPDATE files SET " + ", ".join(k + "=?" for k in fields) + " WHERE id=?", (*fields.values(), file_id))
            if fields.get("state") == "cached":
                self.db.execute("INSERT OR IGNORE INTO cache_receipts VALUES (?,?)", (file_id, now()))
            if fields.get('state') == 'uploaded':
                from .runs import archived
                archived(self.db, file_id)

    def batch_state(self, batch_id, state):
        with self.db:
            self.db.execute("UPDATE batches SET state=?, updated_at=? WHERE id=? AND state<>?", (state, now(), batch_id, state))

    def import_completed(self, batch_id):
        if batch_id:
            with self.db:
                self.db.execute("UPDATE batches SET imported_at=? WHERE id=? AND imported_at IS NULL", (now(), batch_id))
                from .runs import imported
                imported(self.db, batch_id)

    def reporting_run(self, config):
        from .runs import reconcile
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            return reconcile(self.db, config.destination_id, config.source['type'], config.source['serial'], config.backup['media_scope'],
                             archive_previews=config.backup['archive_previews'])

    def preview_policy(self, config):
        row = self.db.execute('''SELECT archive_previews FROM transfer_runs WHERE destination=? AND source_type=?
            AND serial=? AND selection=? AND historical=0 AND finished_at IS NULL ORDER BY rowid DESC LIMIT 1''',
            (config.destination_id, config.source['type'], config.source['serial'], config.backup['media_scope'])).fetchone()
        return bool(row[0]) if row else config.backup['archive_previews']

    def cached_path(self, row):
        return self.cache / (row["id"] + ".bin")

    def reserved(self, destination, remote_path, excluding):
        return remote_path.casefold() in self.reserved_paths(destination, excluding)

    def reserved_paths(self, destination, excluding):
        return {row[0].casefold() for row in self.db.execute(
            "SELECT remote_path FROM files WHERE destination=? AND id<>? AND remote_path IS NOT NULL",
            (destination, excluding))}

    def save_groups(self, destination, groups):
        with self.db:
            for group in groups:
                identifier = uuid.uuid5(uuid.NAMESPACE_URL, destination + ":group:" + group["id"]).hex
                members = [self.file_id(destination, asset) for asset in group["members"]]
                existing = self.db.execute("SELECT members FROM source_groups WHERE id=?", (identifier,)).fetchone()
                if existing:
                    members = list(dict.fromkeys(members + json.loads(existing[0])))
                self.db.execute("INSERT INTO source_groups VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET members=excluded.members,safe=excluded.safe,reason=excluded.reason",
                                (identifier, destination, json.dumps(members), int(group["safe"]), group.get("reason")))
                for file_id in members:
                    self.db.execute("UPDATE files SET group_id=? WHERE id=?", (identifier, file_id))

    def groups(self, destination):
        return [dict(r) for r in self.db.execute("SELECT * FROM source_groups WHERE destination=? ORDER BY rowid", (destination,))]

    def queue_temporary(self, row, kind, path, identity=None):
        with self.db:
            self.db.execute("""INSERT INTO temporary_cleanup
                (id,file_id,destination,kind,path,identity,state,created_at) VALUES (?,?,?,?,?,?,'pending',?)
                ON CONFLICT(kind,destination,path) DO UPDATE SET
                    identity=COALESCE(excluded.identity,temporary_cleanup.identity),
                    state=CASE WHEN temporary_cleanup.state='done' THEN 'pending' ELSE temporary_cleanup.state END,
                    error=CASE WHEN temporary_cleanup.state='done' THEN NULL ELSE temporary_cleanup.error END""",
                (uuid.uuid4().hex, row["id"], row["destination"], kind, path, json.dumps(identity) if identity else None, now()))

    def temporary_items(self, destination=None):
        return [dict(r) for r in self.db.execute("SELECT * FROM temporary_cleanup WHERE state!='done'" +
                (" AND destination=?" if destination else ""), (destination,) if destination else ())]

    def temporary_result(self, identifier, error=None, blocked=False):
        with self.db:
            self.db.execute("UPDATE temporary_cleanup SET state=?, error=? WHERE id=?",
                            ("blocked" if blocked else "pending" if error else "done", error, identifier))


def cache_bytes(directory):
    total = 0
    for path in Path(directory).glob("*"):
        if path.name.startswith(".") or path.suffix not in (".bin", ".download"):
            continue
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass  # A successfully uploaded cache may be removed concurrently.
    return total


@contextmanager
def worker_lock(directory, blocking=False):
    from .locking import file_lock
    path = Path(directory)
    if os.environ.get('MEDIA_IMPORTER_RUNTIME') == 'native' and not path.is_dir():
        raise BackupError('原生数据目录不可访问；未创建替代目录')
    path.mkdir(parents=True, exist_ok=True)
    with file_lock(path / 'worker.lock', blocking) as acquired:
        yield acquired


def fsync_directory(directory):
    if os.name == 'nt':
        return  # Windows does not support opening directories through os.open.
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
