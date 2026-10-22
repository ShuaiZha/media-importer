import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from media_importer.adapters.gopro import GoProSource, make_asset
from media_importer.bot import Bot
from media_importer.bot_cards import detail_content, status_content, settings_result, progress_card
from media_importer.config import Config, ConfigError, preferences
from media_importer.media import select_groups, selected_file_ids
from media_importer.model import now
from media_importer.notifications import NotificationWorker
from media_importer.runtime import Control, Reconfigure
from media_importer.service import Service
from media_importer.settings import Settings
from media_importer.status import snapshot
from media_importer.store import Store
from test_bot import ENV
from test_service import configuration, footage, FakeSource, FakeDestination


def photo(extension='JPG', primary=True):
    return make_asset(footage().serial, '100GOPRO/GOPR0002.' + extension, 16,
                      '1791919163', 'Tue, 13 Oct 2026 19:19:28 GMT',
                      'photo-2' if primary else '', primary, '' if primary else 'photo-2')


def thumbnail():
    return make_asset(footage().serial, '100GOPRO/GH010001.THM', 8, '1791919163',
                      'Tue, 13 Oct 2026 19:19:28 GMT', '', False, 'gumi-1')


class MediaScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, ENV, clear=True)
        self.environment.start()
        self.path = self.root / 'settings.json'
        self.path.write_text(json.dumps(preferences(configuration().values)))
        self.settings = Settings(self.path)
        self.store = Store(self.root / 'data')
        self.assets = [footage(), footage(primary=False), thumbnail(), photo(), photo('GPR', False), photo('THM', False)]
        self.source = FakeSource(self.assets)
        self.destination = FakeDestination()

    def tearDown(self):
        self.store.close()
        self.environment.stop()
        self.tmp.cleanup()

    def service(self, scope='all', **settings):
        self.settings.update_preferences({'backup': {'media_scope': scope, **settings}})
        return Service(Config.load(self.path), self.store, self.source, self.destination, threading.Event())

    def view(self):
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(), configuration={
            'applied_revision': self.settings.read()['revision'], 'pending': False})
        return snapshot(self.store.directory, self.settings)

    def test_old_settings_default_to_all_and_invalid_scope_does_not_save(self):
        old = json.loads(self.path.read_text())
        old['backup'].pop('media_scope')
        self.path.write_text(json.dumps(old))
        self.assertEqual('all', Config.load(self.path).backup['media_scope'])
        before = self.path.read_bytes()
        for invalid in ('audio', ['video'], True, None):
            with self.assertRaises(ConfigError):
                self.settings.update_preferences({'backup': {'media_scope': invalid}})
            self.assertEqual(before, self.path.read_bytes())

    def test_filter_includes_attached_files_and_case_insensitive_raw(self):
        groups = GoProSource.groups(self.assets)
        for scope, expected in (('video', self.assets[:3]), ('photo', self.assets[3:]), ('all', self.assets)):
            selected = [a for g in select_groups(groups, scope) for a in g['members']]
            self.assertEqual({a.identity for a in expected}, {a.identity for a in selected})
        raw = photo('gpr', True)
        unknown = photo('dat', True)
        self.assertEqual(1, len(select_groups(GoProSource.groups([raw, unknown]), 'photo')))
        self.assertEqual([], select_groups([{'members': [photo(), footage()]}], 'video'))

    def test_video_cycle_keeps_unselected_photos_and_archives_flat(self):
        service = self.service('video')
        result = service.cycle()
        self.assertTrue(result['complete'])
        self.assertEqual({a.path for a in self.assets[:3]}, set(self.source.downloads))
        self.assertEqual({a.path for a in self.assets}, set(self.source.assets))
        rows = self.store.files()
        self.assertEqual(3, len(rows))
        self.assertTrue(all(r['state'] == 'uploaded' for r in rows))
        self.assertTrue(all(len(Path(r['remote_path']).parts) == 2 for r in rows))
        self.assertFalse(any(self.store.cached_path(r).exists() for r in rows))
        view = self.view()
        self.assertTrue(view['unplug']['ready'])
        self.assertEqual(3, view['selection']['excluded_source_files'])
        self.assertEqual(0, view['queues']['import']['pending_files'])
        text = json.dumps(detail_content(view), ensure_ascii=False)
        self.assertTrue(view['unplug']['ready'])
        self.assertIn('仅视频', text)
        self.assertIn('仅视频', text)
        self.assertIn('范围外', text)

    def test_switching_back_to_all_preserves_finished_files_and_imports_remainder(self):
        self.assertTrue(self.service('photo').cycle()['complete'])
        before = {r['id']: r['remote_path'] for r in self.store.files()}
        self.assertTrue(self.service('all').cycle()['complete'])
        self.assertEqual(6, len(self.source.downloads))
        self.assertEqual(6, len(set(self.source.downloads)))
        for identifier, path in before.items():
            self.assertEqual(path, self.store.get(identifier)['remote_path'])
        self.assertEqual(6, len(self.destination.uploads))

    def test_existing_mixed_batch_is_not_deleted_or_marked_fully_complete(self):
        full = self.service().prepare_imports()
        batch_id = full['batch_id']
        original = {r['id']: r for r in self.store.files()}
        self.assertTrue(self.service('video').cycle()['complete'])
        self.assertEqual(6, len(self.store.files(batch_id=batch_id)))
        self.assertEqual('pending', self.store.db.execute('SELECT state FROM batches WHERE id=?', (batch_id,)).fetchone()[0])
        for row in self.store.files():
            if self.store.asset(row).path in {a.path for a in self.assets[3:]}:
                self.assertEqual(original[row['id']], row)
        view = self.view()
        self.assertEqual(3, view['selection']['deferred_files'])
        self.assertEqual(0, view['queues']['import']['pending_files'])
        self.assertTrue(view['unplug']['ready'])

    def test_complete_photo_cache_still_uploads_but_only_selected_video_is_deleted(self):
        all_media = self.service()
        all_media.prepare_imports()
        for row in self.store.files():
            if self.store.asset(row).path in {a.path for a in self.assets[3:]}:
                all_media.import_asset(row)
        result = self.service('video', delete_source_after_import=True).cycle()
        self.assertTrue(result['complete'])
        self.assertEqual([footage().path], self.source.deletes)
        self.assertEqual({a.path for a in self.assets[3:]}, set(self.source.assets))
        self.assertEqual(6, len(self.destination.uploads))
        self.assertTrue(all(r['state'] == 'uploaded' for r in self.store.files()))
        self.assertEqual(0, self.view()['work']['pending_cleanup'])

    def test_no_selected_files_is_empty_scan_not_previous_mixed_batch(self):
        self.source = FakeSource([footage()])
        self.service().prepare_imports()
        result = self.service('photo').cycle()
        self.assertTrue(result['complete'])
        self.assertEqual(0, result['files'])
        view = self.view()
        self.assertEqual(0, view['batch']['total_files'])
        self.assertTrue(view['batch']['scanned'])
        self.assertTrue(view['unplug']['ready'])
        self.assertFalse(self.source.downloads)
        self.assertEqual('pending', self.store.files()[0]['state'])

    def test_other_category_cache_progress_is_visible_when_selected_scope_is_empty(self):
        self.source = FakeSource([footage()])
        service = self.service()
        service.prepare_imports()
        row = self.store.files()[0]
        service.import_asset(row)
        self.service('photo').prepare_imports()
        self.store.stage_status('upload', phase='uploading', current_file_id=row['id'],
                                current_file=footage().path, current_bytes=8, current_total_bytes=32,
                                speed_bytes_per_second=2, progress_updated_at=now())
        view = self.view()
        self.assertEqual(1, view['batch']['total_files'])
        self.assertEqual(1, view['selection']['cached_outside_scope_files'])
        self.assertEqual(1, view['queues']['upload']['pending_files'])
        for card in (detail_content(view),):
            text = json.dumps(card, ensure_ascii=False)
            self.assertIn('GH010001.MP4', text)
            self.assertIn('其他类别的 1 个既有缓存', text)
            self.assertNotIn('0 / 0', text)
            self.assertNotIn('本轮更新结束', text)

    def test_unselected_photo_cleanup_is_not_reported_as_pending_in_completion(self):
        self.service().prepare_imports()
        self.settings.update_preferences({'backup': {'media_scope': 'video', 'delete_source_after_import': True}})
        row = next(r for r in self.store.files() if self.store.asset(r).path == photo().path)
        row['batch_created'] = now()
        text = NotificationWorker._completion([row], self.view())
        self.assertIn('未执行删除 1', text)
        self.assertNotIn('原片清理待处理', text)

    def test_rescan_ignores_new_unselected_files_but_detects_selected_changes(self):
        service = self.service('video')
        inventory = service.prepare_imports()
        for row in self.store.files():
            service.import_asset(row)
        new = photo('PNG', True)
        self.source.assets[new.path] = new
        self.assertTrue(service.verify_imports(inventory))
        added = footage(3)
        self.source.assets[added.path] = added
        self.assertFalse(service.verify_imports(inventory))

    def test_scope_change_waits_for_boundary_and_invalidates_old_unplug_claim(self):
        self.service('all').cycle()
        self.assertTrue(self.view()['unplug']['ready'])
        control = Control(self.settings, threading.Event())
        control.adopt()
        self.settings.update_preferences({'backup': {'media_scope': 'photo'}})
        control.refresh()
        self.assertFalse(control.transfer_stop.is_set())
        with self.assertRaises(Reconfigure):
            control.boundary()
        self.assertFalse(self.view()['unplug']['ready'])
        self.assertEqual('photo', control.adopt().backup['media_scope'])
        self.assertTrue(control.enabled())
        self.assertFalse(control.allowed('delete_source_after_import'))


    def test_unselected_failed_pending_file_does_not_notify(self):
        self.service().prepare_imports()
        self.settings.update_preferences({'backup': {'media_scope': 'video'}})
        row = next(r for r in self.store.files() if self.store.asset(r).path == photo().path)
        self.store.update(row['id'], error='previous photo read error', error_stage='import')
        view = self.view()
        self.assertFalse(view['issues'])
        self.assertFalse(NotificationWorker._problems(view))


if __name__ == '__main__':
    unittest.main()
