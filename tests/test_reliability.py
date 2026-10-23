"""Small fixtures for per-file isolation, cache recovery and stalled transfers."""
import json
import errno
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from media_importer.liveness import Activity, CombinedStop
from media_importer.model import BackupError, CacheFileError, Cancelled, TransferStalled, now
from media_importer.recovery import checkpoint, inspect_receipts, receipt_path, recover, snapshot_metadata
from media_importer.service import Service
from media_importer.status import live_state
from media_importer.storage import MARKER
from media_importer.store import Store
from test_bot import ENV
from test_service import FakeSource, FakeDestination, configuration, footage, replace


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.store = Store.create(self.root / 'data')
        self.volume = '12345678123456781234567812345678'
        self.store.set_meta('cache_identity', self.volume)
        (self.store.cache / MARKER).write_text(self.volume)
        self.config = configuration(archive_previews=False)
        self.source = FakeSource([footage(1), footage(2)])
        self.remote = FakeDestination()
        self.stop = threading.Event()

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def service(self, store=None):
        return Service(self.config, store or self.store, self.source, self.remote, self.stop)

    def cached(self):
        service = self.service()
        service.prepare_imports()
        for row in service.rows():
            service.import_asset(row)
        return service.rows()

    def test_one_bad_cache_does_not_block_other_files_or_count_as_complete(self):
        rows = self.cached()
        for row in rows:
            self.store.update(row['id'], cleanup='deleted')
        self.source.assets.clear()
        self.store.cached_path(rows[0]).write_bytes(b'z' * 32)
        result = self.service().cycle()
        bad, good = (self.store.get(r['id']) for r in rows)
        self.assertFalse(result['complete'])
        self.assertEqual('cached', bad['state'])
        self.assertEqual('cache_verification', bad['error_code'])
        self.assertGreater(bad['retry_at'], time.time())
        self.assertEqual('uploaded', good['state'])
        self.assertEqual(1, len(self.remote.uploads))
        self.assertTrue(self.store.cached_path(bad).exists())
        self.service().cycle()
        self.assertEqual(1, self.store.get(bad['id'])['attempts'])
        self.store.close()
        self.store = Store.create(self.root / 'data')
        self.assertEqual(bad['retry_at'], self.store.get(bad['id'])['retry_at'])

    def test_cache_volume_failure_still_stops_both_lanes(self):
        self.cached()
        (self.store.cache / MARKER).write_text('wrong-volume')
        result = self.service().cycle()
        self.assertFalse(result['complete'])
        self.assertEqual([], self.remote.uploads)
        self.assertTrue(all(r['state'] == 'cached' for r in self.store.files()))

    def test_one_unreadable_file_is_isolated_while_volume_remains_usable(self):
        rows = self.cached()
        original = self.remote.upload
        def upload(local, *args, **kwargs):
            if local.name == rows[0]['id'] + '.bin':
                raise PermissionError(errno.EACCES, 'fixture file permission')
            return original(local, *args, **kwargs)
        self.remote.upload = upload
        self.service().cycle()
        self.assertEqual('cache_verification', self.store.get(rows[0]['id'])['error_code'])
        self.assertEqual('uploaded', self.store.get(rows[1]['id'])['state'])

    def test_stopped_or_restarted_service_does_not_keep_stale_liveness_failure(self):
        self.store.status(transfer_health={'upload': {'state': 'stalled'}})
        self.service().recover_interrupted()
        self.assertEqual({}, self.store.get_meta('status')['transfer_health'])
        self.store.status(transfer_health={'upload': {'state': 'stalled'}})
        self.config = configuration(enabled=False)
        self.service().cycle()
        self.assertEqual({}, self.store.get_meta('status')['transfer_health'])

    def test_valid_receipt_exists_before_source_delete(self):
        self.config = configuration(archive_previews=False, delete_source_after_import=True)
        self.cached()
        checked = []
        def on_delete(asset):
            row = next(r for r in self.store.files() if r['asset_id'] == asset.identity)
            value = json.loads(receipt_path(self.store.cache, row['id']).read_text())
            self.assertEqual(asset.identity, value['asset']['identity_key'])
            checked.append(asset.path)
        self.source.on_delete = on_delete
        self.service().cleanup_groups(set())
        self.assertEqual(2, len(checked))
        self.assertTrue(all(r['source_deleted_at'] for r in self.store.files()))

    def test_receipt_failure_preserves_source_and_complete_cache(self):
        self.config = configuration(archive_previews=False, delete_source_after_import=True)
        rows = self.cached()
        with patch('media_importer.recovery.checkpoint', side_effect=CacheFileError('receipt unavailable')):
            self.service().cleanup_groups(set())
        self.assertFalse(self.source.deletes)
        self.assertTrue(all(self.store.cached_path(r).exists() for r in rows))

    def test_rebuild_without_original_database_retains_cache_and_safe_settings(self):
        rows = self.cached()
        self.store.close()
        (self.root / 'data/state.sqlite3').unlink()
        output = self.root / 'recovered'
        result = recover(self.root / 'data/cache', output)
        self.assertEqual(2, len(result['recovered']))
        values = json.loads((output / 'config/settings.json').read_text())
        self.assertFalse(values['backup']['enabled'])
        self.assertFalse(values['backup']['delete_source_after_import'])
        # Emulate mounting the original cache at the recovered data path, as
        # documented for Compose. Each lane opens its own Store connection.
        (output / 'data/cache').rmdir()
        (output / 'data/cache').symlink_to(self.root / 'data/cache', target_is_directory=True)
        self.store = Store.create(output / 'data')
        self.assertEqual({r['id'] for r in rows}, {r['id'] for r in self.store.files()})
        self.source.assets.clear()
        self.service().cycle()
        self.assertTrue(all(r['state'] == 'uploaded' for r in self.store.files()))
        self.assertEqual(2, len(self.remote.uploads))

    def test_lost_publish_ack_receipt_recovers_without_duplicate_upload(self):
        self.source.assets = {footage().path: footage()}
        row = self.cached()[0]
        self.remote.lose_publish_ack = True
        with self.assertRaises(BackupError):
            self.service().upload_cached(row)
        saved = json.loads(receipt_path(self.store.cache, row['id']).read_text())
        self.assertEqual('publishing', saved['state'])
        output = self.root / 'recovered'
        recover(self.store.cache, output)
        restored = Store.create(output / 'data')
        restored.cache = self.store.cache
        try:
            self.service(restored).upload_cached(restored.get(row['id']))
            self.assertEqual('uploaded', restored.get(row['id'])['state'])
            self.assertEqual(1, len(self.remote.uploads))
            self.assertFalse(receipt_path(self.store.cache, row['id']).exists())
        finally:
            restored.close()

    def test_corrupt_cache_is_preserved_and_excluded_from_recovery(self):
        rows = self.cached()
        self.store.cached_path(rows[0]).write_bytes(b'z' * 32)
        result = recover(self.store.cache, self.root / 'recovered')
        self.assertEqual(1, len(result['recovered']))
        self.assertEqual(1, len(result['rejected']))
        self.assertTrue(self.store.cached_path(rows[0]).exists())

    def test_invalid_remote_path_receipt_is_not_trusted(self):
        rows = self.cached()
        path = receipt_path(self.store.cache, rows[0]['id'])
        value = json.loads(path.read_text())
        value['remote_path'] = '../other-file'
        path.write_text(json.dumps(value))
        _, accepted, rejected = inspect_receipts(self.store.cache)
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))
        self.assertFalse(self.remote.uploads)

    def test_missing_or_symlink_receipts_do_not_adopt_media(self):
        rows = self.cached()
        path = receipt_path(self.store.cache, rows[0]['id'])
        content = path.read_text()
        path.unlink()
        _, accepted, rejected = inspect_receipts(self.store.cache)
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))
        outside = self.root / 'outside.json'; outside.write_text(content)
        path.symlink_to(outside)
        self.assertEqual(1, len(inspect_receipts(self.store.cache)[1]))

    def test_external_disk_metadata_is_not_counted_as_media_or_receipts(self):
        rows = self.cached()
        for row in rows:
            for path in (self.store.cached_path(row), receipt_path(self.store.cache, row['id'])):
                path.with_name('._' + path.name).write_bytes(b'\x00\x05\x16\x07fixture metadata')
        _, accepted, rejected = inspect_receipts(self.store.cache)
        self.assertEqual(2, len(accepted))
        self.assertEqual([], rejected)
        self.service().cycle()
        self.assertFalse(list((self.store.cache / '.receipts').glob('*.json')))

    def test_export_preserves_names_and_handles_case_insensitive_collisions(self):
        other = replace(footage(2), path='101GOPRO/GH010001.MP4')
        self.source.assets = {footage().path: footage(), other.path: other}
        rows = self.cached()
        result = recover(self.store.cache, self.root / 'exported', export=True)
        names = sorted(Path(r['path']).name for r in result['recovered'])
        self.assertEqual(['GH010001.MP4', 'GH010001_1.MP4'], names)
        self.assertTrue(all(self.store.cached_path(r).exists() for r in rows))
        with self.assertRaises(BackupError):
            recover(self.store.cache, self.root / 'exported', export=True)

    def test_restart_backfills_missing_receipts_without_changing_tasks(self):
        rows = self.cached()
        for row in rows:
            receipt_path(self.store.cache, row['id']).unlink()
        before = self.store.files()
        self.service().recover_interrupted()
        self.assertEqual(before, self.store.files())
        self.assertEqual(2, len(inspect_receipts(self.store.cache)[1]))

    def test_metadata_snapshots_are_consistent_bounded_and_do_not_copy_media(self):
        rows = self.cached()
        for _ in range(5):
            self.assertTrue(snapshot_metadata(self.store, interval=0, keep=3))
        files = list((self.store.directory / 'recovery').glob('*.sqlite3'))
        self.assertEqual(3, len(files))
        import sqlite3
        with sqlite3.connect('file:' + str(files[-1]) + '?mode=ro', uri=True) as db:
            self.assertEqual([('ok',)], db.execute('PRAGMA quick_check').fetchall())
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM files').fetchone()[0])
        self.assertEqual(rows, self.store.files())
        self.assertFalse(snapshot_metadata(self.store))
        self.assertFalse(list((self.store.directory / 'recovery').glob('*.bin')))

    def test_interrupted_snapshot_keeps_last_good_copy(self):
        self.cached()
        snapshot_metadata(self.store, interval=0)
        existing = set((self.store.directory / 'recovery').iterdir())
        stop = threading.Event(); stop.set()
        with self.assertRaises(Cancelled):
            snapshot_metadata(self.store, stop, interval=0)
        self.assertEqual(existing, set((self.store.directory / 'recovery').iterdir()))

    def test_cooperative_stall_cancels_attempt_and_keeps_complete_cache(self):
        rows = self.cached()
        service = self.service()
        service.activity_timeouts = {'import': 1, 'upload': .03}
        service.cancel_grace = 1
        service.hard_exit = Mock()
        def fork(stop=None):
            self.remote.stop = stop
            return self.remote
        self.remote.fork = fork
        def stuck(*args, **kwargs):
            self.remote.stop.wait(3)
            raise Cancelled('cancelled')
        self.remote.upload = stuck
        with self.assertRaises(TransferStalled):
            service.cycle()
        service.hard_exit.assert_not_called()
        self.assertTrue(all(self.store.cached_path(r).exists() for r in rows))
        self.assertTrue(any(v['state'] == 'stalled' for v in self.store.get_meta('status')['transfer_health'].values()))

    def test_uncooperative_stall_requests_process_restart(self):
        rows = self.cached()
        service = self.service()
        service.activity_timeouts = {'import': 1, 'upload': .03}
        service.cancel_grace = .05
        release, ended = threading.Event(), threading.Event()
        codes = []
        def stuck(*args, **kwargs):
            try:
                release.wait(4)
                raise Cancelled('cancelled')
            finally:
                ended.set()
        self.remote.upload = stuck
        service.hard_exit = lambda code: (codes.append(code), release.set())
        try:
            with self.assertRaises(TransferStalled):
                service.cycle()
            self.assertEqual([75], codes)
        finally:
            release.set(); ended.wait(2)
            for thread in threading.enumerate():
                if thread.name in ('import-worker', 'upload-worker'):
                    thread.join(2)
        self.assertTrue(all(self.store.cached_path(r).exists() for r in rows))


class ActivityTests(unittest.TestCase):
    def test_only_active_overdue_work_expires(self):
        clock = [0]
        activity = Activity({'import': 10, 'upload': 10}, clock=lambda: clock[0])
        activity.begin('import'); activity.begin('upload')
        activity.idle('import')
        clock[0] = 9; activity.touch('upload')
        clock[0] = 15; self.assertEqual([], activity.expired())
        clock[0] = 20; self.assertEqual(['upload'], activity.expired())

    def test_internal_wait_does_not_look_like_stalled_io(self):
        clock = [0]
        activity = Activity({'upload': 10}, clock=lambda: clock[0])
        activity.begin('upload')
        with activity.waiting('upload'):
            clock[0] = 100
            self.assertEqual([], activity.expired())
        self.assertEqual([], activity.expired())

    def test_health_includes_transfer_activity_but_not_expected_waits(self):
        meta = {'service': {'running': True}, 'status': {'worker_heartbeat_at': now(), 'transfer_health': {
            'upload': {'state': 'active', 'activity_at': '2000-01-01T00:00:00+00:00', 'timeout_seconds': 120}}}}
        self.assertFalse(live_state(meta))
        meta['status']['transfer_health']['upload']['state'] = 'waiting'
        self.assertTrue(live_state(meta))

    def test_combined_stop_responds_to_any_event(self):
        first, second = threading.Event(), threading.Event()
        combined = CombinedStop(first, second)
        self.assertFalse(combined.wait(.001))
        second.set()
        self.assertTrue(combined.is_set())
        self.assertTrue(combined.wait(1))
