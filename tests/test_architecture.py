"""Small fixtures for archive identity, immutable facts, bounded IO and recovery."""
import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from media_importer.config import Config, preferences
from media_importer.destination_binding import bind, identity, read, verify_connection
from media_importer.model import DestinationError, now
from media_importer.service import Service
from media_importer.settings import Settings
from media_importer.status import healthy, snapshot
from media_importer.storage import cache_evidence, inspect_cached_file, observe_cached_file
from media_importer.store import Store
from media_importer.workers import Workers
from test_bot import ENV
from test_service import FakeSource, FakeDestination, configuration, footage


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.config = configuration(delete_source_after_import=True, archive_previews=False)
        self.path = self.root / 'settings.json'
        self.path.write_text(json.dumps(preferences(self.config.values)))
        self.settings = Settings(self.path)
        self.store = Store(self.root / 'data')
        self.source = FakeSource([footage(), footage(primary=False)])
        self.remote = FakeDestination()
        self.stop = threading.Event()

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def service(self, config=None, remote=None):
        return Service(config or self.config, self.store, self.source, remote or self.remote, self.stop)

    def changed(self):
        values = copy.deepcopy(self.config.values)
        values['destination'].update(host='storage.example.invalid', username='new_login')
        return Config.from_values(values)

    def archived(self):
        self.assertTrue(self.service().cycle()['complete'])
        return next(r for r in self.store.files() if r['state'] == 'uploaded')

    def test_delete_facts_survive_rescan_and_restart(self):
        self.archived()
        before = {r['id']: r for r in self.store.files()}
        self.assertTrue(all(r['cleanup'] == 'deleted' and r['source_deleted_at'] for r in before.values()))
        self.service().cycle()
        self.store.close()
        self.store = Store(self.root / 'data')
        self.service().cycle()
        for row in self.store.files():
            self.assertEqual('deleted', row['cleanup'])
            self.assertEqual(before[row['id']]['source_deleted_at'], row['source_deleted_at'])
        view = snapshot(self.store.directory, self.settings)
        self.assertEqual(1, view['cleanup']['source_deleted'])
        self.assertEqual(1, view['cleanup']['preview_deleted'])

    def test_preexisting_absence_is_not_claimed_as_program_deletion(self):
        config = configuration(archive_previews=True)
        self.service(config).cycle()
        main = footage()
        del self.source.assets[main.path]
        self.service().cycle()
        row = next(r for r in self.store.files() if self.store.asset(r).path == main.path)
        self.assertEqual('absent', row['cleanup'])
        self.assertIsNone(row['source_deleted_at'])
        self.assertIsNotNone(row['source_absent_at'])

    def test_existing_absent_migration_does_not_invent_deleted_fact(self):
        row = self.archived()
        with self.store.db:
            self.store.db.execute("UPDATE files SET cleanup='absent',source_deleted_at=NULL WHERE id=?", (row['id'],))
        self.store.close()
        self.store = Store(self.root / 'data')
        self.assertIsNone(self.store.get(row['id'])['source_deleted_at'])
        self.assertEqual('absent', self.store.get(row['id'])['cleanup'])

    def test_lost_delete_rescan_recovers_group_facts_after_restart(self):
        from media_importer.model import SourceError
        service = self.service()
        service.prepare_imports()
        main = next(r for r in self.store.files() if r['state'] != 'excluded')
        service.import_asset(main)
        original_snapshot = self.source.snapshot
        def lost_rescan():
            if not self.source.assets:
                raise SourceError('Lost rescan response')
            return original_snapshot()
        self.source.snapshot = lost_rescan
        service.cleanup_groups(set())
        self.assertTrue(all(r['cleanup'] == 'deleting' for r in self.store.files()))
        self.assertFalse(self.source.assets)
        self.source.snapshot = original_snapshot
        self.store.close()
        self.store = Store(self.root / 'data')
        restarted = self.service()
        restarted.cycle()
        self.assertTrue(all(r['cleanup'] == 'deleted' for r in self.store.files()))
        self.assertEqual(1, len(self.source.deletes))

    def test_legacy_binding_keeps_file_round_group_and_notification_keys(self):
        row = self.archived()
        binding = read(self.store.db)
        self.assertEqual(self.config.destination_id, binding['id'])
        before = {name: list(map(tuple, self.store.db.execute('SELECT * FROM ' + name)))
                  for name in ('files', 'transfer_runs', 'source_groups')}
        with self.store.db:
            self.store.db.execute("DELETE FROM meta WHERE key='destination_binding'")
        bind(self.store, self.config)
        for name, expected in before.items():
            self.assertEqual(expected, list(map(tuple, self.store.db.execute('SELECT * FROM ' + name))))
        self.assertEqual(row['destination'], identity(self.store.db, self.changed()))

    def test_connection_change_verifies_archive_without_new_tasks_or_transfers(self):
        row = self.archived()
        changed = self.service(self.changed())
        before = self.store.files()
        changed.observe_destination()
        self.assertEqual(row['destination'], changed.destination_id)
        self.assertEqual(before, self.store.files())
        self.assertEqual(1, len(self.remote.uploads))
        self.assertEqual(row['id'], changed.rows()[0]['id'])
        with patch.object(self.remote, 'stat', wraps=self.remote.stat) as stat:
            changed.verify_destination()
            stat.assert_not_called()  # Connection already proved; don't rescan remotely.

    def test_same_name_and_size_on_other_server_cannot_claim_archive(self):
        row = self.archived()
        other = FakeDestination()
        other.objects = dict(self.remote.objects)
        other.identities = {key: {'inode': 999} for key in other.objects}
        changed = self.service(self.changed(), other)
        with self.assertRaises(DestinationError) as error:
            changed.verify_destination()
        self.assertEqual('destination_identity', error.exception.code)
        self.assertEqual(row['remote_identity'], self.store.get(row['id'])['remote_identity'])
        self.assertEqual([], other.uploads)

    def test_bad_connection_blocks_owned_remote_cleanup_too(self):
        row = self.archived()
        other = FakeDestination()
        other.objects['.partial'] = b'x'
        other.identities['.partial'] = {'inode': 7}
        self.store.queue_temporary(row, 'remote', '.partial', {'inode': 7})
        changed = self.service(self.changed(), other)
        changed.clean_temporaries()
        self.assertIn('.partial', other.objects)
        self.assertTrue(self.store.temporary_items())

    def test_connection_change_retains_complete_cache_and_upload_queue(self):
        self.archived()
        self.source.assets = {footage(2).path: footage(2)}
        self.remote.online = False
        self.service().cycle()
        row = next(r for r in self.store.files() if r['state'] == 'cached')
        cache = self.store.cached_path(row).read_bytes()
        self.remote.online = True
        changed = self.service(self.changed())
        changed.observe_destination()
        self.assertEqual(cache, self.store.cached_path(row).read_bytes())
        changed.upload_cached(row)
        self.assertEqual('uploaded', self.store.get(row['id'])['state'])
        self.assertFalse(self.store.cached_path(row).exists())

    def test_empty_destination_connection_can_be_corrected_without_losing_tasks(self):
        original = self.service()
        self.remote.online = False
        original.cycle()
        saved = self.store.files()
        self.remote.online = True
        changed = self.service(self.changed())
        changed.observe_destination()
        self.assertEqual(saved, self.store.files())
        self.assertEqual(original.destination_id, changed.destination_id)

    def test_connection_without_final_evidence_keeps_unconfirmed_publish(self):
        config = configuration(archive_previews=False)
        original = self.service(config)
        self.remote.lose_publish_ack = True
        original.cycle()
        row = next(r for r in self.store.files() if r['state'] == 'publishing')
        before = dict(self.remote.objects)
        with self.assertRaises(DestinationError):
            self.service(self.changed()).upload_cached(row)
        self.assertEqual(before, self.remote.objects)
        self.assertTrue(self.store.cached_path(row).is_file())

    def test_current_binding_scope_is_used_after_address_edit(self):
        row = self.archived()
        settings = Mock()
        settings.read.return_value = {'config': self.changed().values, 'revision': 'test', 'error': None}
        view = snapshot(self.store.directory, settings)
        self.assertIn(row['id'], {f['id'] for f in view['files']})
        self.assertEqual(1, view['totals']['uploaded_files'])

    def test_cleared_cache_not_cleaned_again_next_cycle_or_restart(self):
        row = self.archived()
        cleared = self.store.get(row['id'])['cache_cleared_at']
        self.assertTrue(cleared)
        with patch.object(Service, 'clean_cache', wraps=self.service().clean_cache) as clean:
            self.service().cycle()
            clean.assert_not_called()
        self.store.close()
        self.store = Store(self.root / 'data')
        self.assertEqual(cleared, self.store.get(row['id'])['cache_cleared_at'])
        self.assertEqual([], self.store.files(self.config.destination_id, work=True))

    def test_noop_status_updates_do_not_write_sqlite(self):
        self.store.status(phase='idle')
        self.store.stage_status('upload', phase='idle', current_bytes=0)
        self.store.set_meta('probe', {'ready': True})
        before = self.store.db.total_changes
        for _ in range(10):
            self.store.status(phase='idle')
            self.store.stage_status('upload', phase='idle', current_bytes=0)
            self.store.set_meta('probe', {'ready': True})
        self.assertEqual(before, self.store.db.total_changes)

    def test_display_reuses_samples_but_operations_and_changes_verify_fresh(self):
        path = self.store.cache / 'sample.bin'
        path.write_bytes(b'a' * 1024)
        evidence = cache_evidence(path, 'sample', None)
        with patch('media_importer.storage.cache_evidence', wraps=cache_evidence) as sample:
            self.assertTrue(observe_cached_file(path, evidence, 'sample', None)['valid'])
            self.assertTrue(observe_cached_file(path, evidence, 'sample', None)['valid'])
            self.assertEqual(1, sample.call_count)
            self.assertTrue(inspect_cached_file(path, evidence, 'sample', None)['valid'])
            self.assertEqual(2, sample.call_count)
            path.write_bytes(b'b' * 1024)
            # Some container filesystems coalesce sub-millisecond timestamps.
            # This case exercises an observable metadata change, not the TTL.
            changed = evidence['mtime_ns'] + 2_000_000_000
            os.utime(path, ns=(changed, changed))
            self.assertFalse(observe_cached_file(path, evidence, 'sample', None)['valid'])
            self.assertEqual(3, sample.call_count)

    def test_display_ttl_and_volume_change_invalidate_samples(self):
        path = self.store.cache / 'sample.bin'
        path.write_bytes(b'a' * 128)
        evidence = cache_evidence(path, 'sample', 'volume1')
        with patch('media_importer.storage.time.monotonic', return_value=100), patch('media_importer.storage.cache_evidence', wraps=cache_evidence) as sample:
            observe_cached_file(path, evidence, 'sample', 'volume1')
            observe_cached_file(path, evidence, 'sample', 'volume1')
            self.assertEqual(1, sample.call_count)
            with patch('media_importer.storage.time.monotonic', return_value=131):
                observe_cached_file(path, evidence, 'sample', 'volume1')
            self.assertEqual(2, sample.call_count)
            self.assertFalse(observe_cached_file(path, evidence, 'sample', 'volume2')['valid'])

    def test_health_probe_does_not_inspect_media_and_detects_failed_worker(self):
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now())
        with patch('media_importer.status.snapshot', side_effect=AssertionError('media read')):
            self.assertTrue(healthy(self.store.directory))
        self.store.status(workers={'watcher': {'critical': True, 'state': 'failed'}})
        self.assertFalse(healthy(self.store.directory))


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stop = Mock()
        self.stop.is_set.return_value = False
        self.stop.wait = Mock()
        self.fatal = Mock()
        self.workers = Workers(self.stop, self.fatal)

    def test_transient_startup_failure_is_recreated(self):
        attempts = []
        def task():
            attempts.append(1)
            if len(attempts) < 2:
                raise OSError('private value must not be logged')
            self.stop.is_set.return_value = True
        with self.assertLogs(level='ERROR') as logs:
            self.workers.run('test', task)
        self.assertEqual(2, len(attempts))
        self.fatal.assert_not_called()
        self.assertNotIn('private value', str(logs.output))

    def test_persistent_critical_failure_exits_instead_of_zombie_service(self):
        target = Mock(side_effect=RuntimeError('failure'))
        self.workers.run('test', target)
        self.assertEqual(3, target.call_count)
        self.fatal.assert_called_once()
        self.assertEqual('failed', self.workers.snapshot()['test']['state'])
        self.assertEqual([1, 2], [c.args[0] for c in self.stop.wait.call_args_list])

    def test_unexpected_normal_return_is_also_recovered(self):
        target = Mock()
        self.workers.run('test', target)
        self.assertEqual(3, target.call_count)
        self.fatal.assert_called_once()

    def test_optional_failure_keeps_transfer_running(self):
        attempts = []
        def task():
            attempts.append(1)
            if len(attempts) == 5:
                self.stop.is_set.return_value = True
            raise OSError('offline')
        self.workers.run('notifications', task, critical=False)
        self.assertEqual(5, len(attempts))
        self.fatal.assert_not_called()

    def test_healthy_period_resets_consecutive_failure_budget(self):
        self.workers.run('test', Mock(side_effect=RuntimeError()))
        self.assertTrue(self.fatal.called)
        self.fatal.reset_mock()
        count = []
        def task():
            count.append(1)
            if len(count) == 4:
                self.stop.is_set.return_value = True
            raise RuntimeError()
        with patch('media_importer.workers.time.monotonic', side_effect=[0, 1, 2, 100, 101, 102, 103]):
            self.workers.run('test', task)
        self.fatal.assert_not_called()
