"""Once-per-round retirement, using tiny synthetic media and isolated storage."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from media_importer.config import Config
from media_importer.destination_binding import verify_connection
from media_importer.model import CacheUnavailable, DestinationError
from media_importer.notifications import NotificationWorker, _database, _digest
from media_importer.recovery import snapshot_metadata
from media_importer.store import Store
from test_notifications import CREDENTIALS
import test_reliability
from test_service import configuration, footage, replace


class LifecycleTests(unittest.TestCase):
    setUp = test_reliability.ReliabilityTests.setUp
    tearDown = test_reliability.ReliabilityTests.tearDown
    service = test_reliability.ReliabilityTests.service
    cached = test_reliability.ReliabilityTests.cached

    def index(self):
        return [dict(r) for r in self.store.db.execute('SELECT * FROM source_index')]

    def test_whole_round_retires_only_after_cleanup_without_touching_archive(self):
        self.config = configuration(delete_source_after_import=True, archive_previews=False)
        self.source.assets[footage(1, False).path] = footage(1, False)
        service = self.service()
        service.settle()  # Bootstrap, before any work.
        self.assertTrue(service.cycle()['complete'])
        archive = copy.deepcopy(self.remote.objects)
        rows = self.store.files()
        self.store.update(rows[0]['id'], cache_cleanup_error='fixture pending')
        self.assertEqual(0, service.settle())
        self.assertEqual(3, len(self.store.files()))
        self.store.update(rows[0]['id'], cache_cleanup_error=None)
        self.assertEqual(3, service.settle())
        self.assertEqual(3, len(self.store.files()))
        self.assertEqual([], self.index())
        self.assertEqual(0, service.settle())
        self.assertEqual(archive, self.remote.objects)
        self.assertFalse(list(self.store.cache.glob('*.bin')))
        self.assertIsNone(self.store.get_meta('last_result'))

    def test_retained_original_stays_deduplicated_after_restart_and_camera_absence(self):
        self.service().cycle()
        self.assertEqual(2, self.service().settle())
        self.assertEqual(2, len(self.store.files()))
        self.assertEqual([], self.index())
        self.store.close()
        self.store = Store(self.root / 'data')
        self.service().cycle()
        self.assertEqual(2, len(self.store.files()))
        self.assertEqual(2, len(self.remote.uploads))
        self.assertEqual(2, len(self.source.downloads))
        self.source.online = False
        self.service().safe_cycle()
        self.assertEqual(2, len(self.store.files()))
        self.source.online = True
        self.source.assets.clear()
        self.service().cycle()
        self.assertEqual(2, len(self.store.files()))
        self.assertEqual(2, len(self.source.downloads))

    def test_delete_enabled_later_checks_archive_then_cleans_without_reupload(self):
        self.service().cycle()
        self.service().settle()
        self.config = configuration(delete_source_after_import=True)
        self.service().cycle()
        self.assertEqual(2, len(self.source.deletes))
        self.assertEqual(2, len(self.remote.uploads))
        self.assertEqual(0, self.service().settle())
        self.assertEqual([], self.index())
        self.assertTrue(all(r['cleanup'] == 'deleted' for r in self.store.files()))

    def test_changed_archive_identity_prevents_deleting_retained_original(self):
        self.service().cycle()
        self.service().settle()
        for key in self.remote.identities:
            self.remote.identities[key] = {'inode': 999}
        self.config = configuration(delete_source_after_import=True)
        self.service().cycle()
        self.assertEqual([], self.source.deletes)
        self.assertEqual(0, self.service().settle())
        self.assertTrue(all(r['source_cleanup_error'] for r in self.store.files()))

    def test_live_delete_setting_reactivates_index_without_restarting_clients(self):
        from media_importer.config import preferences
        from media_importer.runtime import Control
        from media_importer.settings import Settings
        path = self.root / 'settings.json'
        path.write_text(json.dumps(preferences(self.config.values)))
        settings = Settings(path)
        control = Control(settings, self.stop)
        control.adopt()
        service = self.service()
        service.control = control
        service.cycle()
        self.assertEqual(2, service.settle())
        settings.update_preferences({'backup': {'delete_source_after_import': True}})
        control.adopt()
        self.assertFalse(service.config.backup['delete_source_after_import'])
        service.cycle()
        self.assertEqual(2, len(self.source.deletes))
        self.assertEqual(0, service.settle())
        self.assertEqual(2, len(self.remote.uploads))
        self.assertEqual([], self.index())

    def test_same_name_new_source_is_archived_with_suffix(self):
        self.service().cycle()
        self.service().settle()
        asset = replace(footage(1), gumi='new-camera-material')
        self.source.assets[asset.path] = asset
        self.service().cycle()
        self.assertEqual(3, len(self.remote.uploads))
        self.assertTrue(any(path.endswith('_1.MP4') for path in self.remote.objects))

    def test_unfinished_round_and_complete_cache_survive_retirement_and_restart(self):
        self.remote.fail_upload = True
        self.service().cycle()
        before = self.store.files()
        content = {r['id']: self.store.cached_path(r).read_bytes() for r in before}
        self.assertEqual(0, self.service().settle())
        self.store.close()
        self.store = Store(self.root / 'data')
        self.assertEqual(before, self.store.files())
        for row in self.store.files():
            self.assertEqual(content[row['id']], self.store.cached_path(row).read_bytes())

    def test_leftover_receipt_or_pending_temporary_prevents_retirement(self):
        self.service().cycle()
        row = self.store.files()[0]
        self.store.queue_temporary(row, 'remote', '.owned-part', {'inode': 123})
        self.assertEqual(0, self.service().settle())
        self.assertEqual(2, len(self.store.files()))

    def test_cache_offline_preserves_all_details(self):
        self.service().cycle()
        with patch('media_importer.lifecycle.require_cache', side_effect=CacheUnavailable('offline')):
            with self.assertRaises(CacheUnavailable):
                self.service().settle()
        self.assertEqual(2, len(self.store.files()))

    def test_notification_failure_does_not_keep_file_details_and_retries_after_stop(self):
        values = copy.deepcopy(self.config.values)
        values['notifications']['enabled'] = True
        self.config = Config.from_values(values)
        service = self.service()
        service.settle()
        service.cycle()
        settings = Mock()
        settings.notification_credentials.return_value = CREDENTIALS
        settings.read.side_effect = lambda: {'config': values, 'error': None}
        sender = Mock(side_effect=DestinationError('fixture delivery failure'))
        worker = NotificationWorker(self.store.directory, settings, threading.Event(), sender=sender)
        self.assertEqual(2, service.settle())
        self.assertEqual(2, len(self.store.files()))
        saved = self.store.db.execute("SELECT * FROM notification_events WHERE kind='completed'").fetchone()
        self.assertEqual(1, saved['detached'])
        self.assertNotIn('100GOPRO', saved['payload'])
        frozen = json.loads(saved['payload'])['result']
        self.assertEqual(2, frozen['file_count'])
        self.assertEqual(2, frozen['cleanup']['cache_cleared'])
        scope = _digest([service.config.destination_id, self.config.source['type'], self.config.source['serial']])
        values['backup']['enabled'] = False
        with _database(self.store.directory) as db:
            worker._send_pending(db, scope, values['notifications'], time.time(), {})
        self.assertEqual(1, self.store.db.execute('SELECT attempts FROM notification_events').fetchone()[0])
        worker.sender = Mock(return_value='om_completion')
        with _database(self.store.directory) as db:
            worker._send_pending(db, scope, values['notifications'], time.time() + 60, {})
            worker._send_pending(db, scope, values['notifications'], time.time() + 120, {})
        worker.sender.assert_called_once()
        self.assertIn('**2 个文件**', json.dumps(worker.sender.call_args.args[1], ensure_ascii=False))
        self.assertEqual(0, self.store.db.execute('SELECT count(*) FROM notification_events').fetchone()[0])

    def test_old_completed_round_does_not_replay_completion_on_upgrade(self):
        values = copy.deepcopy(self.config.values)
        values['notifications']['enabled'] = True
        self.config = Config.from_values(values)
        self.service().cycle()
        self.assertEqual(2, self.service().settle())
        self.assertEqual(0, self.store.db.execute('SELECT count(*) FROM notification_events').fetchone()[0])

    def test_progress_send_finishing_after_retirement_attaches_to_completion(self):
        values = copy.deepcopy(self.config.values)
        values['notifications']['enabled'] = True
        self.config = Config.from_values(values)
        service = self.service()
        service.settle()
        service.cycle()
        batch = self.store.get_meta('status')['batch_id']
        settings = Mock()
        settings.notification_credentials.return_value = CREDENTIALS
        def send(*args, **kwargs):
            service.settle()
            return 'om_late_progress'
        worker = NotificationWorker(self.store.directory, settings, threading.Event(), sender=send)
        view = {'batch': {'id': batch, 'total_files': 2, 'completed_files': 1}, 'files': []}
        scope = _digest([service.config.destination_id, self.config.source['type'], self.config.source['serial']])
        with _database(self.store.directory) as db:
            worker._send_progress(db, scope, view, values['notifications'], time.time())
        payload = json.loads(self.store.db.execute("SELECT payload FROM notification_events WHERE detached=1").fetchone()[0])
        self.assertEqual('om_late_progress', payload['progress_messages'][0]['id'])
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'notification_progress_card:%'").fetchone()[0])

    def test_completed_send_finishing_after_retirement_reuses_sent_message(self):
        values = copy.deepcopy(self.config.values)
        values['notifications']['enabled'] = True
        self.config = Config.from_values(values)
        service = self.service()
        service.settle()
        service.cycle()
        batch = self.store.get_meta('status')['batch_id']
        settings = Mock()
        settings.notification_credentials.return_value = CREDENTIALS
        settings.read.side_effect = lambda: {'config': values, 'error': None}
        def send(*args, **kwargs):
            service.settle()
            return 'om_same_completion'
        worker = NotificationWorker(self.store.directory, settings, threading.Event(), sender=send, updater=Mock())
        scope = _digest([service.config.destination_id, self.config.source['type'], self.config.source['serial']])
        with _database(self.store.directory) as db:
            with db:
                worker._enqueue(db, 'completed:' + scope + ':' + batch, 'completed', scope, 'Done', 'Done', time.time(), batch_id=batch)
            worker._send_pending(db, scope, values['notifications'], time.time(), {'files': []})
            saved = db.execute('SELECT * FROM notification_events WHERE detached=1').fetchone()
            self.assertEqual('pending', saved['state'])
            self.assertEqual('om_same_completion', json.loads(saved['payload'])['update_message_id'])
            worker._send_pending(db, scope, values['notifications'], time.time() + 60, {'files': []})
            self.assertEqual(0, db.execute('SELECT count(*) FROM notification_events').fetchone()[0])
        worker.updater.assert_called_once()

    def test_stale_completion_observer_cannot_recreate_closed_round(self):
        self.service().cycle()
        batch = self.store.get_meta('status')['batch_id']
        self.service().settle()
        worker = NotificationWorker(self.store.directory, Mock(), threading.Event())
        with self.store.db:
            worker._enqueue(self.store.db, 'stale', 'completed', 'fixture', 'Done', 'Done', time.time(), batch_id=batch)
        self.assertEqual(0, self.store.db.execute('SELECT count(*) FROM notification_events').fetchone()[0])

    def test_retirement_is_atomic_if_notification_freeze_fails(self):
        self.service().cycle()
        before = self.store.files()
        with patch('media_importer.notifications.freeze_completion', side_effect=ValueError('fixture')):
            with self.assertRaises(ValueError):
                self.service().settle()
        self.assertEqual(before, self.store.files())
        self.assertEqual([], self.index())

    def test_one_archive_identity_anchor_survives_without_history(self):
        self.service().cycle()
        self.service().settle()
        anchor = self.store.get_meta('destination_anchor')
        values = copy.deepcopy(self.config.values)
        values['destination']['host'] = 'changed.example.invalid'
        service = self.service()
        changed = Config.from_values(values)
        from media_importer.destination_binding import bind
        changed = bind(self.store, changed)
        verify_connection(self.store, changed, self.remote)
        self.remote.identities[anchor['path']] = {'inode': 999}
        values['destination']['host'] = 'changed-again.example.invalid'
        with self.assertRaises(DestinationError):
            verify_connection(self.store, bind(self.store, Config.from_values(values)), self.remote)

    def test_retirement_replaces_old_automatic_snapshots_only(self):
        self.service().cycle()
        snapshot_metadata(self.store, interval=0)
        old = self.store.get_meta('metadata_backup')['path']
        self.service().settle()
        self.assertTrue(snapshot_metadata(self.store))
        from pathlib import Path
        self.assertFalse(Path(old).exists())
        self.assertEqual(1, len(list((self.store.directory / 'recovery').glob('state-*.sqlite3'))))
