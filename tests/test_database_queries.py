"""Queries cannot bootstrap, migrate, create a replacement DB, or wake transfers."""
import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_importer.camera_web import Workspace, issue
from media_importer.config import preferences
from media_importer.database import initialize
from media_importer.model import BackupError
from media_importer.settings import Settings
from media_importer.store import Store
from test_bot import ENV
from test_service import configuration


class DatabaseQueryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, ENV, clear=True)
        self.environment.start()
        self.store = Store.create(self.root / 'data')

    def tearDown(self):
        self.store.close()
        self.environment.stop()
        self.temporary.cleanup()

    def test_opening_runtime_connections_never_initializes(self):
        with patch('media_importer.database.initialize', side_effect=AssertionError('migration on query')):
            for readonly in (False, True):
                db = Store(self.store.directory, readonly=readonly)
                self.assertEqual([], db.files())
                self.assertEqual(0, db.db.total_changes)
                db.close()

    def test_read_only_snapshot_works_while_another_connection_owns_write_lock(self):
        self.store.db.execute('BEGIN IMMEDIATE')
        reader = Store(self.store.directory, readonly=True)
        try:
            self.assertEqual([], reader.files())
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("DELETE FROM meta")
        finally:
            reader.close()
            self.store.db.rollback()

    def test_missing_database_is_not_created_by_an_ordinary_connection(self):
        missing = self.root / 'detached' / 'data'
        with self.assertRaises(BackupError):
            Store(missing)
        self.assertFalse(missing.exists())

    def test_old_schema_requires_explicit_bootstrap_and_retains_existing_records(self):
        self.store.set_meta('fixture', {'pending': True})
        with self.store.db:
            self.store.db.execute("DELETE FROM meta WHERE key='database_layout'")
        before = self.store.db.execute('SELECT key,value FROM meta ORDER BY key').fetchall()
        with self.assertRaises(BackupError):
            Store(self.store.directory, readonly=True)
        self.assertEqual(before, self.store.db.execute('SELECT key,value FROM meta ORDER BY key').fetchall())
        initialize(self.store.directory)
        self.assertEqual({'pending': True}, self.store.get_meta('fixture'))

    def test_camera_status_and_result_queries_execute_only_reads(self):
        config = configuration()
        path = self.root / 'settings.json'
        path.write_text(json.dumps(preferences(config.values)))
        settings = Settings(path, self.store.directory)
        device = {'type': config.source['type'], 'serial_number': config.source['serial'],
                  'url': config.source['url'], 'transport': 'usb'}
        self.store.set_meta('source_selection', device)
        self.store.set_meta('source_connection', {'id': 'session', 'state': 'approved',
                            'online': True, 'last_seen': time.time(), 'device': device})
        token = issue(self.store, 'owner')['token']
        changed = threading.Event()
        page = Workspace(self.store.directory, settings, SimpleNamespace(changed=changed))
        statements = []
        original = sqlite3.connect

        def tracked(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch('media_importer.database.sqlite3.connect', side_effect=tracked):
            result = asyncio.run(page.dispatch(token, {'action': 'status'}))
        self.assertTrue(result['device']['online'])
        self.assertFalse(changed.is_set())
        forbidden = ('CREATE ', 'ALTER ', 'INSERT ', 'UPDATE ', 'DELETE ', 'BEGIN IMMEDIATE')
        self.assertFalse([sql for sql in statements if sql.strip().upper().startswith(forbidden)])


if __name__ == '__main__':
    unittest.main()
