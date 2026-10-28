"""Last-copy protection and failure isolation, using disposable byte-sized media."""
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from media_importer import camera_operations
from media_importer.completion import read
from media_importer.model import Cancelled, DestinationError, SourceConnectionError, SourceError, SourceScan
from media_importer.task_facts import file_facts
import test_camera_operations as operation_fixtures
from test_gopro_identity import RebootCamera
import test_service as fixtures
from test_service import FakeSource, configuration, footage, replace


class CacheCopySafetyTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp
    tearDown = fixtures.ServiceTests.tearDown
    service = fixtures.ServiceTests.service

    def archived_with_cache(self):
        self.config = configuration(delete_source_after_import=True)
        self.source = FakeSource([footage()])
        service = self.service()
        with patch('media_importer.service.unlink_cache', side_effect=PermissionError('fixture read-only cache')):
            self.assertTrue(service.cycle()['complete'])
        row = self.store.files()[0]
        self.assertFalse(self.source.assets)
        self.assertTrue(self.store.cached_path(row).is_file())
        self.assertTrue(row['cache_cleanup_error'])
        return service, row

    def test_missing_archive_never_discards_last_cache_or_reports_completion(self):
        service, row = self.archived_with_cache()
        original = self.destination.objects.pop(row['remote_path'])
        self.assertFalse(service.cycle()['complete'])
        saved = self.store.get(row['id'])
        self.assertTrue(self.store.cached_path(saved).is_file())
        self.assertEqual('uploaded', saved['state'])
        self.assertEqual('archive_verification', saved['error_code'])
        self.assertFalse(file_facts(saved)['archived'])
        from media_importer.camera_library import backup_states
        states = backup_states(self.store, service.destination_id, [self.store.asset(saved)])
        self.assertEqual('archive_pending', states[saved['asset_id']]['state'])
        self.assertFalse(next(iter(read(self.store.db, service.config).values()))['complete'])
        self.assertEqual(0, service.settle())

        self.assertEqual(1, len(self.destination.uploads))
        # Only restoration of the exact recorded remote copy permits cleanup.
        self.destination.objects[row['remote_path']] = original
        self.assertTrue(service.cycle()['complete'])
        self.assertFalse(self.store.cached_path(row).exists())
        self.assertIsNone(self.store.get(row['id'])['error_code'])
        self.assertEqual(1, len(self.destination.uploads))

    def test_smb_offline_after_restart_preserves_complete_cache(self):
        _, row = self.archived_with_cache()
        self.destination.online = False
        self.assertFalse(self.service().cycle()['complete'])
        self.assertTrue(self.store.cached_path(row).is_file())
        self.destination.online = True
        self.assertTrue(self.service().cycle()['complete'])
        self.assertFalse(self.store.cached_path(row).exists())

    def test_equal_size_replacement_is_not_accepted_for_cache_cleanup(self):
        service, row = self.archived_with_cache()
        self.destination.identities[row['remote_path']] = {'inode': 999}
        self.assertFalse(service.cycle()['complete'])
        self.assertTrue(self.store.cached_path(row).is_file())
        self.assertEqual({'inode': 999}, self.destination.identities[row['remote_path']])
        self.assertEqual(1, len(self.destination.uploads))

    def test_bad_archive_does_not_block_upload_of_another_complete_file(self):
        service, row = self.archived_with_cache()
        self.destination.objects.pop(row['remote_path'])
        other = footage(2)
        self.source.assets[other.path] = other
        self.assertFalse(service.cycle()['complete'])
        other_row = next(r for r in self.store.files() if r['asset_id'] == other.identity)
        self.assertEqual('uploaded', other_row['state'])
        self.assertFalse(self.store.cached_path(other_row).exists())
        self.assertTrue(self.store.cached_path(row).is_file())
        self.assertEqual(2, len(self.destination.uploads))

    def test_cancellation_during_remote_check_keeps_cache(self):
        service, row = self.archived_with_cache()
        with patch.object(self.destination, 'stat', side_effect=Cancelled('fixture stop')):
            with self.assertRaises(Cancelled):
                service.clean_cache(row)
        self.assertTrue(self.store.cached_path(row).is_file())


class PartialScanSafetyTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp
    tearDown = fixtures.ServiceTests.tearDown
    service = fixtures.ServiceTests.service

    def damaged_camera(self):
        self.config = configuration(archive_previews=False, delete_source_after_import=True)
        self.source = RebootCamera(self.config)
        self.good = self.source.video
        self.body = self.source.files[self.good]
        self.bad = '100GOPRO/GH010002.MP4'
        self.metadata = {self.good: dict(self.source.metadata),
                         self.bad: {**self.source.metadata, 's': '4', 'gumi': '11' * 16}}
        self.source.files = {self.good: self.body, self.bad: b'bad!'}
        self.source.media_list = lambda: self.metadata
        self.source.media_info = lambda path: self.metadata[path]
        self.source.delete = Mock(side_effect=AssertionError('partial inventory must never delete'))
        return self.service()

    def test_healthy_files_archive_while_bad_file_blocks_source_cleanup_and_completion(self):
        service = self.damaged_camera()
        result = service.cycle()
        self.assertFalse(result['complete'])
        self.assertEqual('source_files', result['reason'])
        self.assertEqual(self.bad, result['source_errors'][0]['path'])
        self.assertEqual([self.good], self.source.downloads)
        self.assertEqual(1, len(self.destination.publishes))
        self.assertEqual({self.good, self.bad}, set(self.source.files))
        self.source.delete.assert_not_called()
        self.assertFalse(next(iter(read(self.store.db, service.config).values()))['complete'])
        self.assertEqual(0, service.settle())

    def test_all_bad_files_are_not_reported_as_empty_or_offline(self):
        service = self.damaged_camera()
        del self.source.files[self.good]
        del self.metadata[self.good]
        result = service.cycle()
        self.assertEqual('source_files', result['reason'])
        self.assertEqual(1, self.store.get_meta('status')['inventory']['source_files'])
        self.assertFalse(self.source.downloads)
        self.source.delete.assert_not_called()

    def test_strict_snapshot_still_rejects_partial_inventory_with_filename(self):
        self.damaged_camera()
        scan = self.source.scan()
        self.assertEqual([self.good], [a.path for a in scan.assets])
        with self.assertRaisesRegex(SourceError, 'GH010002.MP4'):
            self.source.snapshot()

    def test_connection_failure_is_not_downgraded_to_one_bad_file(self):
        self.damaged_camera()
        with patch.object(self.source, '_identity_metadata', side_effect=SourceConnectionError('fixture offline')):
            with self.assertRaises(SourceConnectionError):
                self.source.scan()

    def test_unreadable_parent_is_not_inferred_absent_during_sidecar_reconciliation(self):
        parent, preview = footage(), footage(primary=False)
        self.store.batch(self.config.destination_id, [parent, preview])
        row = next(r for r in self.store.files() if r['asset_id'] == parent.identity)
        self.store.update(row['id'], cleanup='deleted')
        orphan = replace(preview, parent_gumi='', created='')
        self.source.scan = lambda: SourceScan([orphan], [{'path': parent.path, 'reason': 'fixture bad metadata'}])
        observed = self.service().source_scan()
        self.assertEqual(orphan.identity, observed.assets[0].identity)
        self.assertNotEqual(preview.identity, observed.assets[0].identity)

    def test_repaired_file_retries_without_reuploading_completed_file(self):
        service = self.damaged_camera()
        self.config.backup['delete_source_after_import'] = False
        service.config.backup['delete_source_after_import'] = False
        self.assertFalse(service.cycle()['complete'])
        self.source.files[self.bad] = self.body.replace(bytes.fromhex(self.source.metadata['gumi']), bytes.fromhex('11' * 16))
        self.metadata[self.bad]['s'] = str(len(self.source.files[self.bad]))
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual([self.good, self.bad], self.source.downloads)
        self.assertEqual(2, len(self.destination.publishes))
        self.assertFalse(self.store.get_meta('status')['inventory']['errors'])

    def test_partial_scan_preserves_old_dedup_and_source_presence_facts(self):
        service = self.damaged_camera()
        self.config.backup['delete_source_after_import'] = False
        service.config.backup['delete_source_after_import'] = False
        # A previously archived file is still physically present but unreadable.
        from media_importer.lifecycle import indexed
        asset = self.source.scan().assets[0]
        self.store.db.execute('INSERT INTO source_index VALUES (?,?,?,?,?,?,?,?,?)',
            (service.destination_id, 'old-bad-identity', asset.source_type, asset.serial,
             'uploaded', 'original', 4, 'device/old.MP4', json.dumps({'inode': 90})))
        self.store.db.commit()
        indexed(self.store, service.config, [asset], complete=False)
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM source_index').fetchone()[0])
        service.prepare_imports()
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM source_index').fetchone()[0])


    def test_incomplete_scan_does_not_freeze_a_completion_notice(self):
        service = self.damaged_camera()
        service.config.values['notifications']['enabled'] = True
        service.cycle()
        from media_importer.outbox import freeze_completion
        run = dict(self.store.db.execute('SELECT * FROM transfer_runs').fetchone())
        with self.store.db:
            freeze_completion(self.store.db, service.config, run, self.store.files(), create=True)
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM notification_events WHERE kind='completed'").fetchone()[0])

    def test_valid_file_can_be_selected_and_archived_despite_unrelated_bad_file(self):
        self.damaged_camera()
        self.config.backup['enabled'] = False
        from media_importer.selected_transfer import SelectedService
        groups = self.source.selection_groups(self.source.scan().assets)
        selection = {'id': 'fixture-selection', 'selection_mode': 'files', 'archive_previews': False,
                     'groups': [{**g, 'members': [a.to_dict() for a in g['members']]} for g in groups]}
        selected = SelectedService(self.config, self.store, self.source, self.destination, self.stop, selection=selection)
        self.assertTrue(selected.cycle()['complete'])
        self.assertEqual([self.good], self.source.downloads)
        self.assertEqual({self.good, self.bad}, set(self.source.files))
        self.source.delete.assert_not_called()


class CleanupPreviewConcurrencyTests(unittest.TestCase):
    def test_remote_preview_uses_file_pins_without_holding_global_cache_mutex(self):
        case = operation_fixtures.CameraOperationTests()
        case.setUp()
        entered, release = threading.Event(), threading.Event()
        observed = {}
        try:
            case.record()
            case.service.control = SimpleNamespace(boundary=lambda: None, allowed=lambda policy=None: True)
            row = case.service.rows()[0]
            original = case.remote.stat
            def slow_stat(path):
                entered.set()
                if not release.wait(3): raise AssertionError('fixture timeout')
                return original(path)
            def check_concurrent_access():
                observed['started'] = entered.wait(3)
                acquired = case.service.cache_guard.acquire(blocking=False)
                observed['global_mutex_available'] = acquired
                if acquired: case.service.cache_guard.release()
                observed['file_protected'] = case.service.cache_guard.protected(row['id'])
                observed['cleanup_deferred'] = case.service.clean_cache(row) is False
                release.set()
            case.remote.stat = slow_stat
            thread = threading.Thread(target=check_concurrent_access)
            thread.start()
            result, _ = camera_operations.inspect(case.service, 'archived')
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertTrue(all(observed.values()), observed)
            self.assertEqual(2, result['eligible_files'])
            self.assertFalse(case.service.cache_guard.protected(row['id']))
        finally:
            release.set()
            case.tearDown()

    def test_failed_remote_preview_releases_all_file_pins(self):
        case = operation_fixtures.CameraOperationTests()
        case.setUp()
        try:
            case.record()
            case.service.control = SimpleNamespace(boundary=lambda: None, allowed=lambda policy=None: True)
            with patch.object(case.remote, 'stat', side_effect=DestinationError('fixture offline')):
                result, _ = camera_operations.inspect(case.service, 'archived')
            self.assertEqual(0, result['eligible_files'])
            self.assertFalse(case.service.cache_guard.pins)
        finally:
            case.tearDown()
