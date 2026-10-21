import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_importer.backup import export, seal, verify, prepare_restore
from media_importer.config import preferences
from media_importer.model import BackupError
from media_importer.settings import Settings
from media_importer.storage import cache_evidence, inspect_cached_file, MARKER
from media_importer.store import Store, worker_lock
from test_bot import ENV
from test_service import configuration, footage


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, ENV, clear=True); self.env.start()
        self.settings = self.root / 'settings.json'
        self.config = configuration(enabled=True, delete_source_after_import=True)
        self.settings.write_text(json.dumps(preferences(self.config.values)))
        self.credentials = self.root / '.env'; self.credentials.write_text("DESTINATION_PASSWORD='secret'\n")
        self.store = Store(self.root / 'data')
        self.store.set_meta('cache_identity', 'test-volume')
        (self.store.cache / MARKER).write_text('test-volume\n')
        self.batch = self.store.batch(self.config.destination_id, [footage(1)])
        self.row = self.store.files()[0]
        cached = self.store.cached_path(self.row); cached.write_bytes(b'x' * 32)
        self.store.update(self.row['id'], state='cached', remote_path='Camera-serial/original_1.MP4',
                          cache_identity=json.dumps(cache_evidence(cached, self.row['asset_id'], 'test-volume')))
        self.bundle = self.root / 'bundle'; self.bundle.mkdir()

    def tearDown(self):
        self.store.close(); self.env.stop(); self.tmp.cleanup()

    def make_bundle(self):
        export(self.store.directory, self.settings, self.credentials, self.bundle)
        (self.bundle / 'project').mkdir()
        (self.bundle / 'project/README.md').write_text('test source')
        seal(self.bundle)

    def test_round_trip_keeps_tasks_paths_cache_proof_and_forces_safe_switches(self):
        before = self.store.files()
        self.make_bundle()
        manifest = verify(self.bundle)
        self.assertEqual(2, manifest['cache_files'])
        restored = self.root / 'restored'; restored.mkdir()
        shutil.copytree(self.bundle / 'cache', restored / 'cache')
        shutil.copy2(self.bundle / 'state.sqlite3', restored / 'state.sqlite3')
        target_settings = self.root / 'restored-settings.json'
        shutil.copy2(self.bundle / 'settings.json', target_settings)
        result = prepare_restore(restored, target_settings, self.bundle)
        self.assertFalse(result['sync_enabled']); self.assertFalse(result['delete_source'])
        db = Store(restored)
        try:
            self.assertEqual(before, db.files())
            row = db.files()[0]
            proof = inspect_cached_file(db.cached_path(row), json.loads(row['cache_identity']), row['asset_id'], db.get_meta('cache_identity'))
            self.assertTrue(proof['valid'])
        finally: db.close()
        self.assertEqual(before, self.store.files())
        self.assertTrue(Settings(self.settings).read()['config']['backup']['enabled'])

    def test_damaged_bundle_and_restored_cache_are_rejected(self):
        self.make_bundle()
        file = self.bundle / 'cache' / (self.row['id'] + '.bin')
        file.write_bytes(b'z' * 32)
        with self.assertRaises(BackupError): verify(self.bundle)

    def test_incomplete_or_metadata_modified_bundle_rejected(self):
        export(self.store.directory, self.settings, self.credentials, self.bundle)
        with self.assertRaises(FileNotFoundError): verify(self.bundle)
        (self.bundle / 'project').mkdir(); seal(self.bundle)
        (self.bundle / 'connection.env').write_text('changed')
        with self.assertRaises(BackupError): verify(self.bundle)

    def test_missing_complete_cache_fails_instead_of_claiming_a_complete_backup(self):
        self.store.cached_path(self.row).unlink()
        with self.assertRaises(BackupError):
            export(self.store.directory, self.settings, self.credentials, self.bundle)
        self.assertFalse((self.bundle / 'manifest.json').exists())

    def test_remote_credentials_are_in_portable_manifest_and_resolve_after_moving(self):
        from media_importer.credentials import stage, copy_credentials
        from media_importer.deployment import destination_password
        from media_importer.config import Config
        identifier = 'c' * 32
        stage(self.settings.parent, identifier, 'backup-fixture-secret')
        values = json.loads(self.settings.read_text())
        values['archive'] = {**{k: self.config.destination[k] for k in ('type', 'name', 'host', 'port', 'share', 'directory', 'username', 'domain')},
                             'id': self.config.destination_id, 'change_id': 'd' * 32, 'credential_id': identifier}
        self.settings.write_text(json.dumps(values))
        self.make_bundle()
        manifest = verify(self.bundle)
        self.assertIn('credentials/' + identifier + '.json', [entry['path'] for entry in manifest['entries']])
        target = self.root / 'new-computer'; target.mkdir()
        shutil.copy2(self.bundle / 'settings.json', target / 'settings.json')
        copy_credentials(self.bundle, target)
        loaded = Config.load(target / 'settings.json')
        self.assertEqual('backup-fixture-secret', destination_password(loaded.destination))
        self.assertTrue(loaded.destination['credential_file'].startswith(str(target)))

    def test_live_worker_lock_blocks_export(self):
        with worker_lock(self.store.directory) as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(BackupError):
                export(self.store.directory, self.settings, self.credentials, self.bundle)

    def test_apple_double_sidecars_are_not_portable_media(self):
        sidecar = self.store.cache / ('._' + self.row['id'] + '.bin')
        sidecar.write_bytes(b'\x00\x05\x16\x07' + b'filesystem metadata')
        self.make_bundle()
        manifest = verify(self.bundle)
        self.assertFalse(any(Path(e['path']).name == sidecar.name for e in manifest['entries']))
        self.assertTrue(sidecar.exists())
