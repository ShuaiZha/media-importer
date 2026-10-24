"""Selected-operation reporting uses byte-sized local fixtures only."""
import json
import unittest

from media_importer.bot_cards import detail_content, progress_card, status_content
from media_importer.model import now
from media_importer.outbox import freeze_completion, initialize
from media_importer.storage import cache_evidence
import test_cache_status
from test_service import footage, FakeSource


class SelectedStatusTests(unittest.TestCase):
    setUp = test_cache_status.CacheStatusTests.setUp
    tearDown = test_cache_status.CacheStatusTests.tearDown
    view = test_cache_status.CacheStatusTests.view
    cache = test_cache_status.CacheStatusTests.cache

    def selected(self, state='running'):
        with self.store.db:
            self.store.db.execute('''INSERT OR IGNORE INTO transfer_runs
                (id,destination,source_type,serial,selection,created_at,archive_previews)
                VALUES ('selected-run',?,?,?,'selected:request',?,0)''',
                (self.config.destination_id, self.config.source['type'], self.config.source['serial'], now()))
            self.store.db.execute("INSERT OR IGNORE INTO transfer_run_files VALUES ('selected-run',?)",
                                  (self.row['id'],))
        self.store.status(selected_import={'id': 'request', 'run_id': 'selected-run', 'state': state},
                          configuration={'applied_revision': self.settings.read()['revision']})

    def other_cached(self):
        self.store.batch(self.config.destination_id, [footage(2)])
        row = next(r for r in self.store.files() if r['id'] != self.row['id'])
        path = self.store.cached_path(row)
        path.write_bytes(FakeSource.payload(footage(2)))
        self.store.update(row['id'], state='cached', cache_identity=json.dumps(
            cache_evidence(path, row['asset_id'], None)), error='unrelated failure', error_stage='upload')
        return self.store.get(row['id'])

    def test_sync_off_selection_shows_both_live_lanes_without_other_tasks(self):
        other = self.other_cached()
        self.selected()
        before = self.store.files()
        self.store.stage_status('import', phase='downloading', current_file_id=self.row['id'],
                               current_file=footage().path, current_bytes=16)
        view = self.view()
        self.assertFalse(view['configuration']['sync_enabled'])
        self.assertTrue(view['transfer_enabled'])
        self.assertEqual('running', view['controls']['import']['state'])
        self.assertEqual(50, view['batch']['import_percent'])
        self.assertEqual(1, view['batch']['total_files'])
        self.assertEqual(1, view['queues']['upload']['pending_files'])
        self.assertEqual(0, view['backlog']['files'])
        self.assertFalse(view['issues'])
        self.assertEqual([self.row['id']], [f['id'] for f in view['files']])
        self.assertEqual(before, self.store.files())
        card = progress_card(view, 300)
        text = json.dumps(card, ensure_ascii=False)
        self.assertEqual('所选文件导入进行中', card['header']['title']['content'])
        self.assertIn('50%', text)
        self.assertNotIn('自动进度更新已停止', text)
        self.assertNotIn('备份已停止', text)
        self.assertNotIn(other['id'], text)

    def test_upload_progress_does_not_require_full_sync(self):
        self.selected()
        self.cache()
        self.store.stage_status('upload', phase='uploading', current_file_id=self.row['id'],
                               current_file=footage().path, current_bytes=24)
        view = self.view()
        self.assertEqual(75, view['batch']['percent'])
        self.assertEqual(1, view['batch']['imported_files'])
        self.assertEqual(0, view['batch']['completed_files'])
        self.assertEqual('所选文件归档进行中', status_content(view)[0])
        self.assertEqual('upload', view['files'][0]['stage'])

    def test_unrelated_current_stage_is_not_displayed_as_selected_transfer(self):
        other = self.other_cached()
        self.selected()
        self.store.stage_status('upload', phase='uploading', current_file_id=other['id'],
                               current_file=footage(2).path, current_bytes=30)
        view = self.view()
        self.assertIsNone(view['stages']['upload']['current_file'])
        self.assertEqual(0, view['batch']['percent'])
        self.assertEqual('所选文件等待传输', status_content(view)[0])

    def test_selected_import_keeps_originals_even_if_automatic_delete_setting_is_on(self):
        self.settings.update_preferences({'backup': {'delete_source_after_import': True}})
        self.selected()
        view = self.view()
        self.assertFalse(view['policy']['delete_source'])
        self.assertFalse(view['policy']['delete_source_requested'])
        self.assertEqual(0, view['work']['pending_cleanup'])
        text = json.dumps(detail_content(view), ensure_ascii=False)
        self.assertIn('本次保留相机原片', text)
        self.assertNotIn('导入后自动删除', text)

    def test_paused_selection_keeps_denominator_and_other_tasks_outside_it(self):
        self.other_cached()
        self.selected('paused')
        view = self.view()
        self.assertFalse(view['transfer_enabled'])
        self.assertEqual('paused', view['controls']['upload']['state'])
        self.assertEqual(1, view['batch']['total_files'])
        self.assertEqual('备份已停止', status_content(view)[0])

    def test_complete_cache_uses_selection_run_verification_for_connection_guidance(self):
        self.selected()
        self.cache()
        with self.store.db:
            self.store.db.execute("UPDATE transfer_runs SET imported_at=? WHERE id='selected-run'", (now(),))
        view = self.view()
        self.assertTrue(view['unplug']['ready'])
        self.assertTrue(view['unplug']['can_disconnect'])
        self.assertIn('本次选定素材已完整导入', view['unplug']['message'])
        self.store.status(source_work=True)
        self.assertFalse(self.view()['unplug']['can_disconnect'])

    def test_done_result_survives_once_clear_without_borrowing_previous_batch(self):
        self.other_cached()
        self.store.status(selected_import={'id': 'request', 'run_id': 'retired-run', 'state': 'done',
                                          'files': 3, 'bytes': 96, 'excluded_preview_files': 2,
                                          'cleanup_pending': 0},
                          configuration={'applied_revision': self.settings.read()['revision']})
        view = self.view()
        self.assertEqual('retired-run', view['batch']['id'])
        self.assertEqual((3, 3, 3), tuple(view['batch'][key] for key in ('total_files', 'imported_files', 'completed_files')))
        self.assertEqual(96, view['batch']['confirmed_bytes'])
        self.assertEqual(2, view['batch']['excluded_preview_files'])
        self.assertEqual(3, view['cleanup']['cache_cleared'])
        self.assertFalse(view['files'])
        self.assertEqual(0, view['queues']['upload']['pending_files'])
        self.assertEqual('备份已停止', status_content(view)[0])
        self.assertIn('最近结果：已归档 3 个文件', str(status_content(view)))
        self.assertNotIn('100%', str(status_content(view)))

    def test_enabling_full_sync_returns_to_existing_full_scope(self):
        self.other_cached()
        self.selected('paused')
        self.settings.set_enabled(True)
        view = self.view()
        self.assertTrue(view['configuration']['sync_enabled'])
        self.assertFalse(view['selected_import'])
        self.assertEqual(2, view['batch']['total_files'])

    def test_selected_completion_does_not_generate_duplicate_full_sync_notification(self):
        self.selected()
        with self.store.db:
            initialize(self.store.db)
            run = dict(self.store.db.execute("SELECT * FROM transfer_runs WHERE id='selected-run'").fetchone())
            freeze_completion(self.store.db, self.config, run, self.store.files(), create=True)
        self.assertEqual(0, self.store.db.execute('SELECT COUNT(*) FROM notification_events').fetchone()[0])

    def test_existing_uploaded_without_current_proof_is_not_reported_complete(self):
        self.selected('waiting')
        self.store.update(self.row['id'], state='uploaded', remote_path='camera/original.MP4',
                          remote_identity='{"inode":1}', cache_cleared_at=now())
        before = self.store.get(self.row['id'])
        self.store.set_meta('selected_archive_checks', {'id': 'request', 'verified_file_ids': [],
            'errors': {self.row['id']: '既有归档缺失或身份已变化'}})
        view = self.view()
        self.assertEqual((0, 0), (view['batch']['imported_files'], view['batch']['completed_files']))
        self.assertEqual(0, view['batch']['confirmed_bytes'])
        self.assertEqual(1, view['selected_import']['archive_verification_pending'])
        self.assertEqual(1, view['queues']['upload']['verification_pending_files'])
        self.assertEqual(0, view['queues']['import']['pending_files'])
        self.assertIsNone(view['disk']['next_file'])
        self.assertFalse(view['unplug']['ready'])
        text = json.dumps(detail_content(view), ensure_ascii=False)
        self.assertIn('已有归档待核验', text)
        self.assertIn('既有归档缺失或身份已变化', text)
        self.assertNotIn('所选文件归档完成', text)
        self.assertEqual(before, self.store.get(self.row['id']))

    def test_archive_checks_publish_each_file_and_never_use_another_request_proof(self):
        self.selected()
        self.store.update(self.row['id'], state='uploaded', cache_cleared_at=now())
        self.store.set_meta('selected_archive_checks', {'id': 'other-request',
            'verified_file_ids': [self.row['id']], 'errors': {}})
        self.assertEqual(0, self.view()['batch']['completed_files'])
        self.store.set_meta('selected_archive_checks', {'id': 'request',
            'verified_file_ids': [self.row['id']], 'errors': {}})
        view = self.view()
        self.assertEqual((1, 1), (view['batch']['imported_files'], view['batch']['completed_files']))
        self.assertEqual(32, view['batch']['confirmed_bytes'])
        self.assertEqual(0, view['selected_import']['archive_verification_pending'])
        self.assertEqual(0, view['queues']['upload']['pending_files'])

    def test_pending_archive_with_valid_complete_cache_still_counts_local_import(self):
        self.selected('waiting')
        self.cache()
        self.store.update(self.row['id'], state='uploaded')
        self.store.set_meta('selected_archive_checks', {'id': 'request',
            'verified_file_ids': [], 'errors': {self.row['id']: '目的地暂时不可达'}})
        view = self.view()
        self.assertEqual(1, view['batch']['imported_files'])
        self.assertEqual(0, view['batch']['completed_files'])
        self.assertEqual(32, view['batch']['imported_bytes'])


if __name__ == '__main__':
    unittest.main()
