"""Small fixtures exercise preview exclusions, deletion boundaries and reporting."""
import json
import threading
import unittest
from unittest.mock import Mock, patch

from media_importer.adapters.gopro import GoProSource
from media_importer.bot import Bot
from media_importer.bot_cards import detail_content, progress_card, status_content
from media_importer.config import Config, ConfigError, DEFAULTS
from media_importer.notifications import NotificationWorker
from media_importer.completion import read as read_results
from media_importer.task_facts import archive_complete
from media_importer.store import Store
import test_media_scope as media_fixtures
from test_media_scope import thumbnail, photo
from test_service import footage, replace


class PreviewPolicyTests(unittest.TestCase):
    setUp = media_fixtures.MediaScopeTests.setUp
    tearDown = media_fixtures.MediaScopeTests.tearDown
    service = media_fixtures.MediaScopeTests.service
    view = media_fixtures.MediaScopeTests.view

    def prepare(self, **options):
        assets = [footage(), footage(primary=False), thumbnail()]
        self.source.assets = {a.path: a for a in assets}
        service = self.service(archive_previews=False, **options)
        return service, assets

    def test_default_and_old_json_use_false_and_reject_non_boolean(self):
        self.assertFalse(DEFAULTS['backup']['archive_previews'])
        raw = json.loads(self.path.read_text())
        raw['backup'].pop('archive_previews')
        self.path.write_text(json.dumps(raw))
        self.assertFalse(Config.load(self.path).backup['archive_previews'])
        before = self.path.read_bytes()
        for value in ('false', 0, None):
            with self.assertRaises(ConfigError):
                self.settings.update_preferences({'backup': {'archive_previews': value}})
            self.assertEqual(before, self.path.read_bytes())

    def test_only_confirmed_previews_are_expendable_raw_audio_and_orphans_are_kept(self):
        service, assets = self.prepare()
        wav = replace(thumbnail(), path='100GOPRO/GH010001.WAV')
        unknown = replace(thumbnail(), path='100GOPRO/GH010001.DAT')
        orphan = footage(9, primary=False)
        extras = [wav, unknown, orphan, photo(), photo('GPR', False)]
        self.source.assets.update({a.path: a for a in extras})
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual({assets[0].path, *(a.path for a in extras)}, set(self.source.downloads))
        excluded = [r for r in self.store.files() if r['state'] == 'excluded']
        self.assertEqual({assets[1].path, assets[2].path}, {self.store.asset(r).path for r in excluded})
        unsafe = GoProSource.groups([footage(), replace(footage(primary=False), parent_gumi='')])
        self.assertFalse(any(g['preview_ids'] for g in unsafe))

    def test_default_excludes_previews_from_both_queues_completion_and_repeated_scans(self):
        service, assets = self.prepare()
        result = service.cycle()
        self.assertEqual((True, 1, 32, 2), (result['complete'], result['files'], result['bytes'], result['excluded_preview_files']))
        self.assertEqual([assets[0].path], self.source.downloads)
        self.assertEqual(1, len(self.destination.uploads))
        self.assertEqual(3, len(self.source.assets))
        view = self.view()
        self.assertEqual((1, 1, 1, 32), (view['batch']['total_files'], view['batch']['imported_files'], view['batch']['completed_files'], view['batch']['total_bytes']))
        self.assertTrue(view['unplug']['ready'])
        self.assertFalse(view['issues'])
        self.assertEqual(0, view['work']['pending_files'])
        self.assertEqual(0, view['selection']['deferred_files'])
        self.assertEqual(2, view['batch']['excluded_preview_files'])
        for stage in ('import', 'upload'):
            self.assertEqual(0, view['queues'][stage]['pending_files'])
        for row in self.store.files():
            self.assertFalse(self.store.cached_path(row).exists())
            if row['state'] == 'excluded':
                file = next(f for f in view['files'] if f['id'] == row['id'])
                self.assertFalse(file['imported'])
                self.assertIsNone(row['remote_path'])
        for card in (progress_card(view, 300), status_content(view)):
            text = json.dumps(card, ensure_ascii=False)
            self.assertIn('预览文件：未归档 2 个', json.dumps(detail_content(view), ensure_ascii=False))
            self.assertNotIn('3 / 3', text)
        rows = self.store.files()
        self.assertTrue(archive_complete(rows))
        self.assertIn('1 个文件', NotificationWorker._completion(next(iter(read_results(self.store.db, service.config).values()))['result']))
        self.assertIn('预览文件：未归档 2 个', NotificationWorker._completion(next(iter(read_results(self.store.db, service.config).values()))['result']))
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(1, len(self.destination.uploads))
        self.assertEqual(1, len(self.source.downloads))

    def test_import_cleanup_precedes_remote_upload_and_ignored_files_never_claim_backup(self):
        service, assets = self.prepare(delete_source_after_import=True)
        self.destination.online = False
        self.assertFalse(service.cycle()['complete'])
        self.assertEqual([assets[0].path], self.source.deletes)
        self.assertFalse(self.source.assets)
        rows = self.store.files()
        self.assertEqual({'cached', 'excluded'}, {r['state'] for r in rows})
        self.assertTrue(all(r['cleanup'] == 'deleted' for r in rows))
        self.assertTrue(self.store.cached_path(next(r for r in rows if r['state'] == 'cached')).exists())
        view = self.view()
        self.assertEqual((1, 0, 1), (view['batch']['imported_files'], view['batch']['completed_files'], view['queues']['upload']['pending_files']))
        self.assertEqual(1, view['cleanup']['source_deleted'])
        self.assertEqual(2, view['cleanup']['preview_deleted'])
        self.destination.online = True
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(1, len(self.destination.uploads))
        self.assertFalse(list(self.store.cache.glob('*.bin')))
        self.assertEqual(1, self.view()['cleanup']['cache_cleared'])

    def test_missing_original_never_deletes_previews_or_marks_round_complete(self):
        service, assets = self.prepare(delete_source_after_import=True)
        self.source.fail_download = True
        self.assertFalse(service.cycle()['complete'])
        self.assertFalse(self.source.deletes)
        self.assertEqual(3, len(self.source.assets))
        self.assertFalse(archive_complete(self.store.files()))
        self.assertEqual(0, self.view()['batch']['imported_files'])

    def test_preview_policy_is_frozen_until_next_round_and_existing_records_are_immutable(self):
        service, assets = self.prepare()
        service.prepare_imports()
        round_id = self.store.reporting_run(service.config)
        later = [footage(2), footage(2, primary=False)]
        self.source.assets.update({a.path: a for a in later})
        service = self.service(archive_previews=True)
        service.prepare_imports()
        self.assertEqual(round_id, self.store.reporting_run(service.config))
        self.assertEqual(3, sum(r['state'] == 'excluded' for r in self.store.files()))
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(2, len(self.destination.uploads))
        before = {r['id']: (r['state'], r['remote_path']) for r in self.store.files()}
        self.source.assets.update({a.path: a for a in (footage(3), footage(3, primary=False))})
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(4, len(self.destination.uploads))
        for key, outcome in before.items():
            row = self.store.get(key)
            self.assertEqual(outcome, (row['state'], row['remote_path']))

    def test_legacy_tasks_cache_and_paths_survive_additive_upgrade_and_disabled_previews(self):
        service, assets = self.prepare()
        self.store.batch(service.destination_id, assets)
        self.store.save_groups(service.destination_id, GoProSource.groups(assets))
        service.import_asset(self.store.files()[1])
        before = self.store.files()
        cached = self.store.cached_path(before[1]).read_bytes()
        with self.store.db:
            self.store.db.execute('ALTER TABLE files DROP COLUMN role')
            self.store.db.execute('ALTER TABLE transfer_runs DROP COLUMN archive_previews')
        self.store.close()
        self.store = Store.create(self.root / 'data')
        self.assertEqual(before, self.store.files())
        self.assertEqual(cached, self.store.cached_path(before[1]).read_bytes())
        service = self.service(archive_previews=False)
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(3, len(self.destination.uploads))
        self.assertTrue(all(r['state'] == 'uploaded' for r in self.store.files()))

    def test_new_audio_or_changed_preview_prevents_deletion_until_rechecked(self):
        service, assets = self.prepare(delete_source_after_import=True)
        service.prepare_imports()
        service.import_asset(next(r for r in self.store.files() if r['state'] != 'excluded'))
        wav = replace(thumbnail(), path='100GOPRO/GH010001.WAV')
        self.source.assets[wav.path] = wav
        service.cleanup_groups(set())
        self.assertFalse(self.source.deletes)
        self.assertTrue(service.cycle()['complete'])
        self.assertIn(wav.path, self.source.downloads)
        self.assertEqual(2, len(self.destination.uploads))

    def test_changed_preview_identity_blocks_source_deletion(self):
        service, assets = self.prepare(delete_source_after_import=True)
        service.prepare_imports()
        service.import_asset(next(r for r in self.store.files() if r['state'] != 'excluded'))
        self.source.assets[assets[1].path] = replace(assets[1], modified='changed')
        service.cleanup_groups(set())
        self.assertFalse(self.source.deletes)
        self.assertTrue(any(r['source_cleanup_error'] for r in self.store.files()))

    def test_chaptered_recording_waits_for_every_original_but_not_previews(self):
        service, assets = self.prepare(delete_source_after_import=True)
        second = replace(footage(), path='100GOPRO/GH020001.MP4', gumi='chapter-2')
        preview = replace(footage(primary=False), path='100GOPRO/GL020001.lrv', parent_gumi='chapter-2')
        self.source.assets.update({a.path: a for a in (second, preview)})
        service.prepare_imports()
        originals = [r for r in self.store.files() if r['state'] != 'excluded']
        self.assertEqual(2, len(originals))
        service.import_asset(originals[0])
        service.cleanup_groups(set())
        self.assertFalse(self.source.deletes)
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual([second.path, assets[0].path], self.source.deletes)
        self.assertEqual(2, len(self.destination.uploads))
        self.assertEqual(3, self.view()['cleanup']['preview_deleted'])

    def test_restart_keeps_exclusion_and_notifications_use_required_file_counts(self):
        service, assets = self.prepare()
        service.prepare_imports()
        before = self.store.files()
        self.store.close()
        self.store = Store.create(self.root / 'data')
        self.assertEqual(before, self.store.files())
        service = self.service(archive_previews=True)
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(1, len(self.destination.uploads))
        worker = NotificationWorker(self.store.directory, self.settings, threading.Event())
        batches = read_results(self.store.db, service.config)
        rows = next(iter(batches.values()))
        view = self.view()
        card_view = worker._completed_view(view, rows)
        self.assertEqual(1, card_view['batch']['completed_files'])
        self.assertEqual(2, card_view['batch']['excluded_preview_files'])
        self.assertFalse(worker._problems(view))

    def test_lost_delete_ack_is_resolved_and_unremoved_preview_is_reported(self):
        for leftover in (False, True):
            with self.subTest(leftover=leftover):
                service, assets = self.prepare(delete_source_after_import=True)
                self.source.lose_delete_ack = True
                self.source.keep_sidecar = leftover
                self.assertTrue(service.cycle()['complete'])
                view = self.view()
                if leftover:
                    self.assertEqual(2, view['cleanup']['preview_pending'])
                    self.assertEqual(2, view['work']['pending_preview_cleanup'])
                    self.assertIn('source:import:source_cleanup', NotificationWorker._problems(view))
                    self.assertEqual(0, view['work']['pending_files'])
                else:
                    self.assertEqual(2, view['cleanup']['preview_deleted'])
                # A second isolated camera identity for the next subcase.
                self.store.close()
                self.store = Store.create(self.root / ('leftover' if not leftover else 'done'))



if __name__ == '__main__':
    unittest.main()
