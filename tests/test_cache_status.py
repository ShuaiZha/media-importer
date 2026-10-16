import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from media_importer.config import Config, preferences
from media_importer.model import CacheUnavailable, now
from media_importer.service import Service
from media_importer.settings import Settings
from media_importer.status import snapshot
from media_importer.storage import cache_evidence, file_identity, inspect_cached_file
from media_importer.store import Store
from media_importer.bot_cards import archive_content, status_content
from test_bot import ENV
from test_service import configuration, footage, FakeSource, FakeDestination


class CacheStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.config_path = self.root / 'settings.json'
        self.config_path.write_text(json.dumps(preferences(configuration(enabled=False).values)))
        self.settings = Settings(self.config_path)
        self.config = Config.load(self.config_path)
        self.store = Store(self.root / 'data')
        self.source = FakeSource([footage()])
        self.source.read_range = lambda asset, start, count: FakeSource.payload(asset)[start:start+count]
        self.remote = FakeDestination()
        self.service = Service(self.config, self.store, self.source, self.remote, threading.Event())
        self.batch = self.store.batch(self.config.destination_id, [footage()])
        self.row = self.store.files()[0]
        self.path = self.store.cached_path(self.row)
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(), source={'online': True, 'checked_at': now()}, batch_id=self.batch)

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def cache(self, legacy=False, remapped=False):
        self.path.write_bytes(FakeSource.payload(footage()))
        evidence = file_identity(self.path) if legacy else cache_evidence(self.path, self.row['asset_id'], None)
        if remapped:
            evidence['inode'] += 100
        self.store.update(self.row['id'], state='cached', cache_identity=json.dumps(evidence))
        return evidence

    def view(self):
        return snapshot(self.store.directory, self.settings)

    def test_durable_evidence_survives_inode_change_but_rejects_equal_size_content_change(self):
        expected = self.cache()
        previous = file_identity(self.path)
        replacement = self.path.with_suffix('.new')
        replacement.write_bytes(self.path.read_bytes())
        os.utime(replacement, ns=(previous['mtime_ns'], previous['mtime_ns']))
        os.replace(replacement, self.path)
        self.assertTrue(inspect_cached_file(self.path, expected, self.row['asset_id'], None)['valid'])
        self.path.write_bytes(b'z' * footage().size)
        os.utime(self.path, ns=(previous['mtime_ns'], previous['mtime_ns']))
        self.assertFalse(self.service.cache_valid(self.store.get(self.row['id'])))
        self.assertEqual(0, self.view()['batch']['imported_files'])
        self.assertTrue(self.view()['attention']['required'])

    def test_legacy_recovery_restores_count_without_transfer_delete_or_settings_change(self):
        self.cache(legacy=True, remapped=True)
        original = self.config_path.read_bytes()
        before = self.store.get(self.row['id'])
        view = self.view()
        self.assertEqual(1, view['batch']['unverified_files'])
        self.assertEqual(footage().size, view['disk']['unverified_cache_bytes'])
        self.assertEqual(0, view['batch']['imported_files'])
        self.assertIn('待核验', json.dumps(status_content(view), ensure_ascii=False))
        result = self.service.verify_caches()
        self.assertEqual([], result['pending'])
        self.assertEqual([footage().path], result['verified'])
        self.assertEqual(1, self.view()['batch']['imported_files'])
        after = self.store.get(self.row['id'])
        self.assertEqual({k:v for k,v in before.items() if k != 'cache_identity'},
                         {k:v for k,v in after.items() if k != 'cache_identity'})
        self.assertEqual(original, self.config_path.read_bytes())
        self.assertEqual([], self.source.downloads + self.source.deletes + self.remote.uploads)

    def test_legacy_offline_or_different_source_keeps_cache_and_count_unconfirmed(self):
        self.cache(legacy=True, remapped=True)
        payload = self.path.read_bytes()
        self.source.online = False
        self.assertEqual(1, len(self.service.verify_caches()['pending']))
        self.source.online = True
        self.source.assets = {}
        self.assertEqual(1, len(self.service.verify_caches()['pending']))
        self.assertEqual(payload, self.path.read_bytes())
        self.assertEqual(0, self.view()['batch']['imported_files'])

    def test_wrong_legacy_content_is_never_adopted_or_overwritten(self):
        self.cache(legacy=True, remapped=True)
        self.source.read_range = lambda asset, start, count: b'z' * count
        self.assertEqual(1, len(self.service.verify_caches()['pending']))
        original = self.path.read_bytes()
        active = Service(configuration(), self.store, self.source, self.remote, threading.Event())
        with self.assertRaises(CacheUnavailable):
            active.import_asset(self.store.get(self.row['id']))
        self.assertEqual(original, self.path.read_bytes())
        self.assertFalse(self.source.downloads)

    def test_commit_boundary_recovers_v2_partial_without_downloading(self):
        self.cache()
        partial = self.path.with_suffix('.download')
        self.path.rename(partial)
        self.store.update(self.row['id'], state='pending')
        self.service.recover_interrupted()
        self.assertEqual('cached', self.store.get(self.row['id'])['state'])
        self.assertTrue(self.path.exists())
        self.assertFalse(partial.exists())

    def test_stopped_is_safe_to_disconnect_but_not_falsely_all_imported(self):
        state = self.view()
        self.assertTrue(state['unplug']['can_disconnect'])
        self.assertFalse(state['unplug']['ready'])
        self.assertIn('还有 1 个', state['unplug']['message'])
        self.assertIn('尚未重新扫描', json.dumps(status_content(state), ensure_ascii=False))

    def test_unplug_requires_all_known_files_and_current_inventory(self):
        self.cache()
        self.settings.set_enabled(True)
        self.store.status(inventory={'scanned': True, 'verified': True, 'batch_id': self.batch,
                                     'destination': self.config.destination_id, 'serial': footage().serial})
        self.assertTrue(self.view()['unplug']['ready'])
        self.store.batch(self.config.destination_id, [footage(2)])
        self.assertFalse(self.view()['unplug']['ready'])
        self.store.status(inventory={'scanned': False, 'verified': False})
        self.assertFalse(self.view()['unplug']['ready'])

    def test_recent_archive_uses_real_suffixed_path_and_survives_next_pending_batch(self):
        path = footage().device_directory + '/GH010001_2.MP4'
        self.store.update(self.row['id'], state='uploaded', remote_path=path)
        self.store.batch_state(self.batch, 'uploaded')
        newest = self.store.batch(self.config.destination_id, [footage(2)])
        self.store.status(batch_id=newest)
        state = self.view()
        recent = state['recent_result']
        self.assertEqual(self.batch, recent['batch_id'])
        self.assertTrue(recent['complete'])
        self.assertEqual(1, recent['archived_files'])
        self.assertTrue(recent['archives'][0]['location'].endswith(path))
        text = json.dumps(archive_content(state), ensure_ascii=False)
        self.assertIn('GH010001_2.MP4', text)
        self.assertNotIn(ENV['DESTINATION_PASSWORD'], text)
        self.assertEqual(0, state['batch']['completed_files'])

    def test_incomplete_record_never_invents_success_or_actual_destination_file(self):
        state = self.view()
        self.assertFalse(state['recent_result']['complete'])
        self.assertIsNone(state['recent_result']['finished_at'])
        self.assertFalse(state['archive']['locations'])
        text = json.dumps(archive_content(state), ensure_ascii=False)
        self.assertIn('尚无已归档文件', text)
        self.assertIn('smb://192.0.2.20:445/media/media/HERO9-', text)

    def test_unverified_leftover_never_reverses_archive_or_hides_pending_cleanup(self):
        self.cache(legacy=True, remapped=True)
        self.store.update(self.row['id'], state='uploaded', remote_path='Camera/sample.bin')
        state = self.view()
        self.assertEqual(1, state['batch']['completed_files'])
        self.assertEqual(1, state['batch']['imported_files'])
        self.assertEqual(1, state['recent_result']['cache_cleanup_pending'])
        self.assertIn('归档完成数量不受影响', state['attention']['message'])

    def test_expired_service_and_permission_failure_explain_required_action(self):
        self.store.status(worker_heartbeat_at='2020-01-01T00:00:00+00:00')
        self.assertFalse(self.view()['unplug']['can_disconnect'])
        self.assertTrue(self.view()['attention']['required'])
        self.settings.set_enabled(True)
        self.store.status(worker_heartbeat_at=now(), destination={'online':False,'checked_at':now(),
                          'issue': {'code':'permission', 'action':'检查共享权限'}})
        self.assertIn('检查共享权限', self.view()['attention']['message'])
