"""A configuration upgrade keeps effective values, identities, and complete media."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_importer.config import Config, preferences
from media_importer.config_upgrade import convert, prepare
from media_importer.credentials import credential_path, read, stage
from media_importer.model import ConfigError
from media_importer.settings import Settings
from test_bot import ENV
from test_service import configuration


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.path = self.root / 'settings.json'
        self.env = {**ENV, 'PREVIEW_PUBLIC_URL': 'https://preview.example.ts.net:8443', 'SOURCE_TRANSPORT': 'wifi'}
        self.patch = patch.dict(os.environ, self.env, clear=True); self.patch.start(); self.addCleanup(self.patch.stop)
        self.old = preferences(configuration(enabled=False).values)
        self.old.update(version=4); self.old.pop('preview'); self.old.pop('source_options'); self.old.pop('archive')
        self.old['backup'].pop('source_transport')
        self.path.write_text(json.dumps(self.old))

    def test_upgrade_is_atomic_idempotent_and_preserves_all_effective_values(self):
        before = self.path.read_bytes()
        document, credential = convert(self.old, self.root)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse((self.root / 'credentials').exists())
        self.assertTrue(prepare(self.path))
        migrated = self.path.read_bytes()
        self.assertEqual(before, self.path.with_name('settings.json.v4-backup').read_bytes())
        self.assertEqual(credential[1], read(credential_path(self.root, credential[0])))
        self.assertNotIn(credential[1], migrated.decode())
        effective = Config.load(self.path)
        self.assertEqual('wifi', effective.source['transport'])
        self.assertEqual(self.env['DESTINATION_HOST'], effective.destination['host'])
        self.assertEqual(self.env['PREVIEW_PUBLIC_URL'], effective.values['preview']['public_url'])
        with patch.dict(os.environ, {'DESTINATION_HOST':'changed.invalid','SOURCE_URL':'invalid',
                                      'PREVIEW_PUBLIC_URL':'invalid','DESTINATION_PASSWORD':'changed'}):
            self.assertFalse(prepare(self.path))
            self.assertEqual(effective, Config.load(self.path))
            self.assertEqual(migrated, self.path.read_bytes())
            self.assertEqual(credential[1], read(credential_path(self.root, credential[0])))

    def test_saved_overrides_and_credentials_win_during_migration(self):
        old = copy.deepcopy(self.old)
        old['source_options'] = {'url': '', 'serial':'override', 'timeout_seconds':7, 'request_attempts':2}
        old['backup']['source_transport'] = 'usb'
        old['archive'] = {'host':'archive.example.test','port':445,'share':'final','directory':'flat',
                          'username':'new-user','domain':'domain','name':'final',
                          'id':'archive:existing-identity','change_id':'a'*32,'credential_id':'b'*32}
        stage(self.root, 'b'*32, 'stored-secret')
        self.path.write_text(json.dumps(old))
        prepare(self.path)
        item = Config.load(self.path)
        self.assertEqual('archive:existing-identity', item.destination_id)
        self.assertEqual('a'*32, item.values['archive']['change_id'])
        self.assertEqual('stored-secret', read(item.destination['credential_file']))
        self.assertEqual('override', item.source['serial']); self.assertEqual('', item.source['url'])
        self.assertEqual('usb', item.source['transport'])
        self.assertEqual('archive.example.test', item.destination['host'])

    def test_invalid_input_does_not_write_any_configuration_or_credential(self):
        for mutate in (lambda x: x.update(unexpected=True),
                       lambda x: x['backup'].update(enabled='true')):
            old = copy.deepcopy(self.old); mutate(old)
            self.path.write_text(json.dumps(old)); before = self.path.read_bytes()
            with self.assertRaises(ConfigError): prepare(self.path)
            self.assertEqual(before, self.path.read_bytes())
            self.assertFalse((self.root / 'credentials').exists())
            self.assertFalse(self.path.with_name('settings.json.v4-backup').exists())

    def test_candidate_validation_is_read_only_and_runtime_requires_upgrade(self):
        before = self.path.read_bytes()
        self.assertEqual(5, Config.load(self.path).values['version'])
        self.assertEqual(before, self.path.read_bytes())
        self.assertIsNotNone(Settings(self.path).read()['error'])
        self.assertEqual([self.path], list(self.root.iterdir()))

    def test_restart_after_credential_written_before_json_commit_is_safe(self):
        _, credential = convert(self.old, self.root)
        stage(self.root, *credential)
        self.assertTrue(prepare(self.path))
        self.assertEqual(credential[1], read(credential_path(self.root, credential[0])))
        self.assertFalse(prepare(self.path))

    def test_required_business_values_cannot_fall_back_to_env(self):
        prepare(self.path)
        raw = json.loads(self.path.read_text()); raw.pop('source_options')
        with self.assertRaises(ConfigError): Config.from_dict(raw, self.root)
        raw = json.loads(self.path.read_text()); raw.pop('archive')
        with self.assertRaises(ConfigError): Config.from_dict(raw, self.root)

    def test_preview_policy_does_not_recreate_transfer_clients(self):
        prepare(self.path); settings = Settings(self.path)
        token = settings.token()[1]
        raw = json.loads(self.path.read_text()); raw['preview']['public_url']=''
        self.path.write_text(json.dumps(raw))
        self.assertEqual(token, settings.token()[1])

    def test_migration_retains_bound_archive_with_unfinished_files_and_cache(self):
        from media_importer.store import Store
        from media_importer.destination_binding import bind
        from test_service import footage
        directory = self.root / 'data'
        store = Store.create(directory)
        try:
            logical = 'archive:already-bound'
            store.set_meta('destination_binding', {'id':logical, 'connection':'original-connection', 'bound_at':'existing'})
            store.batch(logical, [footage()]); row = store.files()[0]
            cached = store.cached_path(row); cached.write_bytes(b'complete-small-video')
            before = store.files()
            self.assertTrue(prepare(self.path, directory))
            config = Config.load(self.path)
            self.assertEqual(logical, config.destination_id)
            self.assertEqual(logical, bind(store, config).destination_id)
            self.assertEqual(before, store.files())
            self.assertEqual(b'complete-small-video', cached.read_bytes())
        finally:
            store.close()

    def test_first_canonical_binding_adopts_matching_unfinished_work(self):
        from media_importer.store import Store
        from media_importer.destination_binding import bind
        from test_service import footage
        directory = self.root / 'data'; store = Store.create(directory)
        try:
            document, _ = convert(self.old, self.root)
            store.batch(document['archive']['id'], [footage()]); before=store.files()
            prepare(self.path, directory)
            config=bind(store, Config.load(self.path))
            self.assertEqual(document['archive']['id'], config.destination_id)
            self.assertEqual(before,store.files())
        finally:
            store.close()

    def test_corrupt_database_does_not_guess_a_new_archive_identity(self):
        import sqlite3
        directory = self.root / 'data'; directory.mkdir()
        (directory/'state.sqlite3').write_bytes(b'broken database')
        before=self.path.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):prepare(self.path,directory)
        self.assertEqual(before,self.path.read_bytes())

    def test_two_workers_migrate_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:prepare(self.path,self.root/'data'),range(2)))
        self.assertEqual([False,True],sorted(results))
        self.assertEqual(5,json.loads(self.path.read_text())['version'])

    def test_missing_credential_never_falls_back_to_environment_password(self):
        from media_importer.deployment import destination_password
        prepare(self.path)
        config = Config.load(self.path)
        Path(config.destination['credential_file']).unlink()
        with self.assertRaises(ConfigError):destination_password(config.destination)
        with self.assertRaises(ConfigError):destination_password({})
