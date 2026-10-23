"""Frozen selected imports use disposable bytes, never a real camera/storage."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from media_importer.model import Cancelled, ConfigError
from media_importer.selected_transfer import SelectedService
from media_importer.service import Service
from media_importer.store import Store
from test_service import configuration, footage, FakeSource, FakeDestination, replace


class SelectedTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.create(Path(self.tmp.name) / 'data')
        self.config = configuration(enabled=False, delete_source_after_import=True, archive_previews=False)
        self.assets = [footage(n, primary=p) for n in (1, 2, 3) for p in (True, False)]
        self.source = FakeSource(self.assets)
        self.remote = FakeDestination()
        self.stop = threading.Event()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def manifest(self, numbers=(1,), previews=False):
        selected = {a.identity for n in numbers for a in (footage(n), footage(n, primary=False))}
        groups = self.source.groups(self.source.assets.values())
        return {'id': 'selected-test', 'groups': [
            {**group, 'members': [a.to_dict() for a in group['members']]}
            for group in groups if any(a.identity in selected for a in group['members'])],
            'archive_previews': previews}

    def service(self, manifest=None, **kwargs):
        return SelectedService(self.config, self.store, self.source, self.remote, self.stop,
                               selection=manifest or self.manifest(), **kwargs)

    def automatic(self):
        return Service(configuration(delete_source_after_import=False, archive_previews=True),
                       self.store, self.source, self.remote, self.stop)

    def test_selection_runs_with_sync_off_and_never_deletes_source(self):
        result = self.service().cycle()
        self.assertTrue(result['complete'])
        self.assertEqual((1, 32, 1), (result['files'], result['bytes'], result['excluded_preview_files']))
        self.assertEqual([footage().path], self.source.downloads)
        self.assertEqual(1, len(self.remote.uploads))
        self.assertFalse(self.source.deletes)
        self.assertEqual({a.path for a in self.assets}, set(self.source.assets))
        self.assertFalse(self.config.backup['enabled'])
        self.assertTrue(self.config.backup['delete_source_after_import'])
        self.assertTrue(all(not self.store.cached_path(row).exists() for row in self.store.files()))

    def test_unselected_tasks_caches_partial_cleanup_and_old_run_are_unchanged(self):
        prior = self.automatic()
        prior.prepare_imports()
        for row in prior.rows():
            if self.store.asset(row).path == footage(2).path:
                prior.import_asset(row)
            if self.store.asset(row).path == footage(3).path:
                partial = self.store.cached_path(row).with_suffix('.download')
                partial.write_bytes(b'unselected-partial')
                self.store.queue_temporary(row, 'local', partial.name)
        before = {row['id']: row for row in self.store.files() if row['asset_id'] not in
                  {footage().identity, footage(primary=False).identity}}
        cached = {p.name: p.read_bytes() for p in self.store.cache.iterdir() if p.is_file()}
        old_run = dict(self.store.db.execute('SELECT * FROM transfer_runs').fetchone())
        old_members = {r[0] for r in self.store.db.execute(
            'SELECT file_id FROM transfer_run_files WHERE run_id=?', (old_run['id'],))}
        service = self.service()
        result = service.cycle()
        self.assertTrue(result['complete'])
        self.assertEqual(before, {key: self.store.get(key) for key in before})
        for name, content in cached.items():
            self.assertEqual(content, (self.store.cache / name).read_bytes())
        self.assertEqual(old_members, {r[0] for r in self.store.db.execute(
            'SELECT file_id FROM transfer_run_files WHERE run_id=?', (old_run['id'],))})
        self.assertIsNone(self.store.db.execute(
            'SELECT finished_at FROM transfer_runs WHERE id=?', (old_run['id'],)).fetchone()[0])
        current = self.store.db.execute('SELECT selection FROM transfer_runs WHERE id=?', (service.run_id,)).fetchone()
        self.assertEqual('selected:selected-test', current[0])
        selected_members = {r[0] for r in self.store.db.execute(
            'SELECT file_id FROM transfer_run_files WHERE run_id=?', (service.run_id,))}
        # A prior pending preview stays untouched when this selection excludes it.
        self.assertEqual({Store.file_id(service.destination_id, footage())}, selected_members)
        self.assertFalse(self.store.get_meta('last_batch') == service.batch_id)

    def test_archive_preview_policy_is_frozen_per_selection(self):
        result = self.service(self.manifest(previews=True)).cycle()
        self.assertEqual(2, result['files'])
        self.assertEqual(0, result['excluded_preview_files'])
        self.assertEqual({footage().path, footage(primary=False).path}, set(self.source.downloads))
        self.assertFalse(self.source.deletes)

    def test_compact_dedupe_does_not_reupload_or_prune_unselected_entries(self):
        service = self.service()
        archive = self.automatic()
        archive.cycle()
        for row in self.store.files():
            asset = self.store.asset(row)
            with self.store.db:
                self.store.db.execute('INSERT INTO source_index VALUES (?,?,?,?,?,?,?,?,?)',
                    (service.destination_id, asset.identity, asset.source_type, asset.serial, row['state'],
                     row['role'], asset.size, row['remote_path'], row['remote_identity']))
                self.store.db.execute('DELETE FROM files WHERE id=?', (row['id'],))
        index_before = [dict(r) for r in self.store.db.execute('SELECT * FROM source_index ORDER BY asset_id')]
        before = len(self.remote.uploads), len(self.source.downloads)
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(before, (len(self.remote.uploads), len(self.source.downloads)))
        self.assertEqual(index_before, [dict(r) for r in self.store.db.execute('SELECT * FROM source_index ORDER BY asset_id')])

    def test_missing_archive_requires_attention_without_redownload_or_record_replacement(self):
        service = self.service()
        self.assertTrue(service.cycle()['complete'])
        service.settle()
        index = [dict(row) for row in self.store.db.execute('SELECT * FROM source_index')]
        self.remote.objects.clear()
        result = self.service(service.selection).cycle()
        self.assertFalse(result['complete'])
        self.assertEqual('archive_verification', result['reason'])
        self.assertEqual(1, result['archive_verification_pending'])
        self.assertEqual(0, result['uploaded_files'])
        self.assertEqual(1, len(self.remote.uploads))
        self.assertEqual(1, len(self.source.downloads))
        self.assertEqual(index, [dict(row) for row in self.store.db.execute('SELECT * FROM source_index')])
        self.assertFalse(self.source.deletes)

    def test_same_size_replaced_archive_is_not_adopted_or_overwritten(self):
        service = self.service()
        self.assertTrue(service.cycle()['complete'])
        row = next(row for row in service.rows() if row['state'] == 'uploaded')
        original_row = dict(row)
        self.remote.identities[row['remote_path']] = {'inode': 'somebody-elses-file'}
        result = self.service(service.selection).cycle()
        self.assertFalse(result['complete'])
        self.assertEqual([], result['archive_verified_ids'])
        self.assertEqual(0, result['uploaded_files'])
        self.assertEqual(original_row, self.store.get(row['id']))
        self.assertEqual(1, len(self.remote.uploads))

    def test_old_archive_check_offline_does_not_block_other_selected_imports(self):
        service = self.service()
        self.assertTrue(service.cycle()['complete'])
        service.settle()
        manifest = self.manifest((1, 2))
        self.remote.online = False
        request = self.service(manifest)
        result = request.cycle()
        self.assertFalse(result['complete'])
        self.assertEqual(1, result['archive_verification_pending'])
        self.assertEqual(1, result['imported_files'])  # Only the new, valid complete cache.
        row = next(row for row in request.rows() if row['asset_id'] == footage(2).identity)
        self.assertEqual('cached', row['state'])
        self.assertTrue(request.cache_valid(row))
        self.remote.online = True
        self.assertTrue(self.service(manifest).cycle()['complete'])
        self.assertEqual(2, len(self.source.downloads))
        self.assertEqual(2, len(self.remote.uploads))

    def test_failed_old_archive_proof_does_not_block_other_selected_uploads(self):
        service = self.service()
        self.assertTrue(service.cycle()['complete'])
        self.remote.objects.clear()
        result = self.service(self.manifest((1, 2))).cycle()
        self.assertFalse(result['complete'])
        self.assertEqual(1, result['uploaded_files'])
        self.assertEqual(1, result['archive_verification_pending'])
        self.assertEqual(2, len(self.remote.uploads))

    def test_archive_checks_publish_per_file_for_live_status(self):
        result = self.service(self.manifest((1, 2))).cycle()
        metadata = self.store.get_meta('selected_archive_checks')
        self.assertEqual('selected-test', metadata['id'])
        self.assertEqual(result['archive_verified_ids'], metadata['verified_file_ids'])
        self.assertEqual({}, metadata['errors'])

    def test_smb_offline_imports_then_restarts_upload_without_redownload(self):
        self.remote.online = False
        service = self.service()
        first = service.cycle()
        self.assertFalse(first['complete'])
        self.assertEqual(1, first['imported_files'])
        cache = self.store.cached_path(next(row for row in service.rows() if row['state'] == 'cached'))
        content = cache.read_bytes()
        self.remote.online = True
        second = self.service(service.selection).cycle()
        self.assertTrue(second['complete'])
        self.assertEqual(1, len(self.source.downloads))
        self.assertEqual(content, next(iter(self.remote.objects.values())))
        self.assertFalse(cache.exists())
        self.assertEqual(1, self.store.db.execute(
            "SELECT count(*) FROM transfer_runs WHERE selection LIKE 'selected:%'").fetchone()[0])

    def test_camera_disconnected_complete_cache_still_uploads(self):
        self.remote.online = False
        manifest = self.manifest()
        self.assertFalse(self.service(manifest).cycle()['complete'])
        self.source.online = False
        self.remote.online = True
        result = self.service(manifest).cycle()
        self.assertTrue(result['complete'])
        self.assertEqual(1, len(self.remote.uploads))
        self.assertEqual(1, len(self.source.downloads))
        self.assertFalse(self.source.deletes)

    def test_new_unselected_group_does_not_extend_selection_or_prevent_completion(self):
        manifest = self.manifest()
        new = footage(99)
        self.source.assets[new.path] = new
        result = self.service(manifest).cycle()
        self.assertTrue(result['complete'])
        self.assertEqual([footage().path], self.source.downloads)
        self.assertNotIn(new.identity, {r['asset_id'] for r in self.store.files()})

    def test_startup_full_inventory_invalidation_does_not_rescan_frozen_selection(self):
        self.store.invalidate_inventory('服务重启后需重新核对相机清单')
        full_inventory = self.store.get_meta('status')['inventory']
        def bounded_scan(count):
            if count > 10:
                self.fail('选定导入被全量清单的旧失效标记反复触发扫描')
        self.source.on_snapshot = bounded_scan
        result = self.service(self.manifest((1, 2))).cycle()
        self.assertTrue(result['complete'])
        self.assertLessEqual(self.source.snapshot_calls, 4)
        self.assertEqual(full_inventory, self.store.get_meta('status')['inventory'])
        self.assertEqual(2, len(self.source.downloads))
        self.assertEqual(2, len(self.remote.uploads))
        self.assertFalse(self.source.deletes)

    def test_changed_selected_group_does_not_block_other_selected_group(self):
        manifest = self.manifest((1, 2))
        changed = replace(footage(), gumi='replacement')
        self.source.assets[changed.path] = changed
        result = self.service(manifest).cycle()
        self.assertFalse(result['complete'])
        self.assertEqual([footage(2).path], self.source.downloads)
        self.assertNotIn(changed.identity, {r['asset_id'] for r in self.store.files()})
        self.assertFalse(self.source.deletes)

    def test_new_sidecar_in_selected_group_is_not_silently_added(self):
        del self.source.assets[footage(primary=False).path]
        manifest = self.manifest()
        sidecar = footage(primary=False)
        self.source.assets[sidecar.path] = sidecar
        result = self.service(manifest).cycle()
        self.assertFalse(result['complete'])
        self.assertFalse(self.source.downloads)
        self.assertNotIn(sidecar.identity, {row['asset_id'] for row in self.store.files()})

    def test_unfinished_download_retries_from_zero_and_preserves_complete_unselected_cache(self):
        prior = self.automatic()
        prior.prepare_imports()
        unselected = next(row for row in prior.rows() if row['asset_id'] == footage(2).identity)
        prior.import_asset(unselected)
        original = self.store.cached_path(unselected).read_bytes()
        self.source.fail_download = True
        service = self.service()
        self.assertFalse(service.cycle()['complete'])
        selected = next(row for row in service.rows() if row['asset_id'] == footage().identity)
        self.assertFalse(self.store.cached_path(selected).with_suffix('.download').exists())
        self.source.fail_download = False
        self.store.update(selected['id'], retry_at=0)
        self.assertTrue(self.service(service.selection).cycle()['complete'])
        self.assertEqual(original, self.store.cached_path(unselected).read_bytes())
        self.assertEqual(2, self.source.downloads.count(footage().path))

    def test_selected_lanes_are_parallel(self):
        importing_second = threading.Event()
        uploading_first = threading.Event()
        download = self.source.download
        upload = self.remote.upload
        def source_download(asset, path, progress):
            if asset.identity == footage(2).identity:
                self.assertTrue(uploading_first.wait(3))
                importing_second.set()
            return download(asset, path, progress)
        def remote_upload(*args, **kwargs):
            if not uploading_first.is_set():
                uploading_first.set()
                self.assertTrue(importing_second.wait(3))
            return upload(*args, **kwargs)
        self.source.download, self.remote.upload = source_download, remote_upload
        self.assertTrue(self.service(self.manifest((1, 2))).cycle()['complete'])
        self.assertTrue(importing_second.is_set())

    def test_manifest_rejects_other_camera_and_duplicate_members(self):
        manifest = self.manifest()
        manifest['groups'][0]['members'][0]['serial'] = 'OTHER'
        with self.assertRaises(ConfigError):
            self.service(manifest)
        manifest = self.manifest()
        manifest['groups'] += manifest['groups']
        with self.assertRaises(ConfigError):
            self.service(manifest)

    def test_replayed_request_keeps_finished_paths_and_no_duplicate_transfer(self):
        service = self.service()
        self.assertTrue(service.cycle()['complete'])
        first = {row['id']: row['remote_path'] for row in service.rows()}
        self.assertTrue(self.service(service.selection).cycle()['complete'])
        self.assertEqual(1, len(self.remote.uploads))
        self.assertEqual(first, {row['id']: row['remote_path'] for row in service.rows()})

    def test_scoped_settle_preserves_unselected_tasks_and_keeps_selected_dedupe(self):
        prior = self.automatic()
        prior.prepare_imports()
        unselected = {row['id']: row for row in self.store.files() if row['asset_id'] not in
                      {footage().identity, footage(primary=False).identity}}
        service = self.service()
        result = service.cycle()
        self.assertTrue(result['complete'])
        self.assertEqual(1, service.settle())  # Existing pending preview was outside this request.
        self.assertEqual(unselected, {key: self.store.get(key) for key in unselected})
        original = self.store.db.execute(
            'SELECT * FROM source_index WHERE asset_id=?', (footage().identity,)).fetchone()
        self.assertEqual('uploaded', original['state'])
        self.assertTrue(original['remote_identity'])
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM transfer_runs WHERE id=?', (service.run_id,)).fetchone())
        self.assertFalse(self.source.deletes)
        before = len(self.source.downloads), len(self.remote.uploads)
        self.assertTrue(self.service(service.selection).cycle()['complete'])
        self.assertEqual(before, (len(self.source.downloads), len(self.remote.uploads)))

    def test_scoped_settle_does_not_wait_for_global_automatic_deletion(self):
        service = self.service()
        self.assertTrue(service.cycle()['complete'])
        self.assertTrue(self.config.backup['delete_source_after_import'])
        self.assertEqual(2, service.settle())
        self.assertEqual([], service.rows())
        self.assertEqual(2, self.store.db.execute('SELECT count(*) FROM source_index').fetchone()[0])
        self.assertEqual(6, len(self.source.assets))
        self.assertFalse(self.source.deletes)

    def test_scoped_settle_preserves_cache_when_not_all_selected_files_archived(self):
        self.remote.online = False
        service = self.service()
        self.assertFalse(service.cycle()['complete'])
        before = {row['id']: row for row in service.rows()}
        self.assertEqual(0, service.settle())
        self.assertEqual(before, {row['id']: row for row in service.rows()})
        self.assertTrue(any(self.store.cached_path(row).exists() for row in service.rows()))

    def test_result_does_not_count_missing_complete_cache_as_imported(self):
        self.remote.online = False
        service = self.service()
        self.assertEqual(1, service.cycle()['imported_files'])
        row = next(row for row in service.rows() if row['state'] == 'cached')
        self.store.cached_path(row).unlink()
        result = service.result()
        self.assertEqual(0, result['imported_files'])
        self.assertEqual(0, result['imported_bytes'])
        self.assertFalse(result['import_verified'])

    def test_reading_selected_files_keeps_camera_awake_with_global_sync_off(self):
        service = Service(self.config, self.store, self.source, self.remote, self.stop)
        self.source.keep_alive_interval_seconds = .05
        called = threading.Event()
        original = self.source.keep_alive
        def keep_alive():
            original()
            called.set()
        self.source.keep_alive = keep_alive
        self.store.status(selected_import={'id': 'selected-test', 'state': 'running'}, source_work=True)
        with service.monitoring():
            self.assertTrue(called.wait(2))
            self.store.status(source_work=False)
            count = self.source.keep_alive_calls
            time.sleep(.15)
            self.assertEqual(count, self.source.keep_alive_calls)

    def test_drain_stops_after_current_file_without_starting_remaining_members(self):
        class DrainControl:
            transfer_stop = threading.Event()
            finished = False
            draining_requested = False
            def refresh(self):
                pass
            def allowed(self, policy=None):
                return not self.transfer_stop.is_set() and policy is None
            def boundary(self):
                if self.transfer_stop.is_set():
                    raise Cancelled('drained')
            def draining(self):
                return self.draining_requested
            def finish_stop(self):
                self.finished = True
                self.transfer_stop.set()
        control = DrainControl()
        download = self.source.download
        def first_file(asset, path, progress):
            result = download(asset, path, progress)
            control.draining_requested = True
            return result
        self.source.download = first_file
        service = self.service(self.manifest((1, 2)), control=control)
        with self.assertRaises(Cancelled):
            service.cycle()
        self.assertTrue(control.finished)
        self.assertEqual([footage().path], self.source.downloads)
        self.assertFalse(self.source.deletes)
        self.assertTrue(any(self.store.cached_path(row).exists() for row in service.rows()))

    def test_selected_copy_respects_shared_stop_and_never_deletes(self):
        download = self.source.download
        def cancelled(asset, path, progress):
            self.stop.set()
            raise Cancelled('test stop')
        self.source.download = cancelled
        service = self.service()
        with self.assertRaises(Cancelled):
            service.cycle()
        self.assertFalse(self.source.deletes)
        self.assertFalse(self.remote.uploads)
        self.assertFalse(list(self.store.cache.glob('*.download')))
        self.source.download = download


if __name__ == '__main__':
    unittest.main()
