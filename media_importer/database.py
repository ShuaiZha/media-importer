"""Explicit schema bootstrap and non-creating runtime connections."""
import json
import os
import sqlite3
import uuid
from pathlib import Path

from .model import BackupError

VERSION = 5
LAYOUT = 1


def connect(directory, *, readonly=False, timeout=15):
    path = Path(directory) / 'state.sqlite3'
    try:
        db = sqlite3.connect(path.resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                             uri=True, timeout=timeout)
        db.row_factory = sqlite3.Row
        if readonly:
            db.execute('PRAGMA query_only=ON')
        else:
            db.execute('PRAGMA synchronous=FULL')
        if db.execute('PRAGMA user_version').fetchone()[0] != VERSION:
            raise BackupError('数据库格式不受支持，请先停止服务并检查升级结果')
        row = db.execute("SELECT value FROM meta WHERE key='database_layout'").fetchone()
        if not row or json.loads(row[0]) != LAYOUT:
            raise BackupError('数据库尚未初始化，请通过正常启动或安装入口完成初始化')
        return db
    except (sqlite3.Error, BackupError, ValueError) as exc:
        if 'db' in locals(): db.close()
        if isinstance(exc, BackupError): raise
        raise BackupError('任务数据库不可访问或尚未初始化；请恢复原数据目录后启动服务') from exc


def initialize(directory):
    from .deployment import assert_runtime
    from .locking import file_lock
    directory = Path(directory)
    assert_runtime(directory)
    if os.environ.get('MEDIA_IMPORTER_RUNTIME') == 'native' and not directory.is_dir():
        raise BackupError('原生数据目录不可访问；等待原磁盘，不创建替代目录')
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / 'schema.lock'):
        db = sqlite3.connect(str(directory / 'state.sqlite3'), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            if db.execute('PRAGMA user_version').fetchone()[0] not in (0, VERSION):
                raise BackupError('数据库格式不受支持；请先离线迁移到版本 5')
            _schema(db)
            with db:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('database_layout', ?)", (json.dumps(LAYOUT),))
        finally:
            db.close()
    from .deployment import cache_path
    from .storage import requires_mount
    if not requires_mount() and os.environ.get('MEDIA_IMPORTER_RUNTIME') != 'native':
        cache_path(directory).mkdir(exist_ok=True)


def _schema(db):
    from .camera_operations import initialize as initialize_camera_operations
    initialize_camera_operations(db)
    from .capture import initialize as initialize_capture
    initialize_capture(db)
    from .selected_imports import initialize as initialize_selected_imports
    initialize_selected_imports(db)
    db.executescript("""
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
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("PRAGMA user_version=5")
        columns = {r[1] for r in db.execute('PRAGMA table_info(files)')}
        for name, definition in {'role': "TEXT NOT NULL DEFAULT 'original'", 'source_deleted_at': 'TEXT',
                                 'source_absent_at': 'TEXT', 'cache_cleared_at': 'TEXT',
                                 'error_code': 'TEXT', 'retry_at': 'REAL NOT NULL DEFAULT 0'}.items():
            if name not in columns:
                db.execute(f'ALTER TABLE files ADD COLUMN {name} {definition}')
    from .runs import initialize
    initialize(db)
    from .lifecycle import initialize as initialize_lifecycle
    with db:
        initialize_lifecycle(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS bot_actions (
            token TEXT PRIMARY KEY, scope TEXT NOT NULL, payload TEXT NOT NULL,
            revision TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_jobs (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, payload TEXT NOT NULL,
            created REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
            response TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, error TEXT);
    """)
    from .outbox import initialize as initialize_outbox
    with db:
        initialize_outbox(db)
        db.execute("INSERT OR IGNORE INTO meta VALUES ('notification_instance_id', ?)", (json.dumps(uuid.uuid4().hex),))
    from .completion import initialize as initialize_results
    with db:
        initialize_results(db)
