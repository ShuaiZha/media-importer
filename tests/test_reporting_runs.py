"""Small-file statistics, migration and cleanup truth; no network access."""
import json
import os
import unittest
from unittest.mock import patch

from media_importer.model import now
from media_importer.store import Store
from media_importer.status import cleanup_summary
from media_importer.bot_cards import cleanup_text, progress_card, status_content
import test_cache_status
from test_service import footage


class ReportingRunTests(unittest.TestCase):
    setUp = test_cache_status.CacheStatusTests.setUp
    tearDown = test_cache_status.CacheStatusTests.tearDown
    view = test_cache_status.CacheStatusTests.view
    cache = test_cache_status.CacheStatusTests.cache

    def test_old_thirty_plus_new_two_share_one_denominator_across_restart(self):
        old = self.store.batch(self.config.destination_id, [footage(i) for i in range(1, 31)])
        for row in self.store.files(batch_id=old)[:25]:
            self.store.update(row['id'], state='uploaded')
        new = self.store.batch(self.config.destination_id, [footage(31), footage(32)])
        self.store.status(batch_id=new, inventory={'scanned': True})
        view = self.view()
        self.assertEqual(self.batch, view['batch']['id'])
        self.assertEqual(new, view['batch']['scan_batch_id'])
        self.assertEqual(32, view['batch']['total_files'])
        self.assertEqual(25, view['batch']['completed_files'])
        self.assertEqual(7, view['queues']['upload']['pending_files'])
        self.assertEqual(32 * 32, view['batch']['total_bytes'])
        # Completing the latest small scan must not finish the reporting round.
        for row in self.store.files(batch_id=new):
            self.store.update(row['id'], state='uploaded')
        self.assertIsNone(self.store.db.execute('SELECT finished_at FROM transfer_runs WHERE id=?', (self.batch,)).fetchone()[0])
        before = self.store.files()
        self.store.close()
        self.store = Store(self.root / 'data')
        self.assertEqual(before, self.store.files())
        after = self.view()
        self.assertEqual((32, 27), (after['batch']['total_files'], after['batch']['completed_files']))
        self.assertEqual(5, after['queues']['upload']['pending_files'])
        for row in self.store.files():
            self.store.update(row['id'], state='uploaded')
        self.assertEqual(32, self.view()['batch']['completed_files'])
        self.assertEqual(0, self.view()['queues']['upload']['pending_files'])

    def test_new_round_has_its_own_progress_without_history_query(self):
        self.store.update(self.row['id'], state='uploaded', remote_path='Camera-serial/original.MP4')
        new = self.store.batch(self.config.destination_id, [footage(2)])
        self.store.status(batch_id=new)
        view = self.view()
        self.assertEqual((1, 0), (view['batch']['total_files'], view['batch']['completed_files']))
        self.assertEqual(new, view['batch']['id'])
        self.assertNotIn('recent_result', view)
        self.assertNotIn('totals', view)
        self.assertEqual(1, view['work']['pending_files'])

    def test_additive_migration_keeps_files_and_complete_cache_and_separates_history(self):
        self.cache()
        self.store.batch(self.config.destination_id, [footage(2)])
        old = self.store.files()[-1]
        self.store.update(old['id'], state='uploaded', remote_path='Camera-serial/history.MP4', cleanup='deleted')
        before = self.store.files()
        content = self.path.read_bytes()
        with self.store.db:
            self.store.db.execute('DROP TABLE transfer_run_files')
            self.store.db.execute('DROP TABLE transfer_runs')
            self.store.db.execute("DELETE FROM meta WHERE key='reporting_rounds_v1'")
        self.store.close()
        self.store = Store(self.root / 'data')
        self.assertEqual(before, self.store.files())
        self.assertEqual(content, self.path.read_bytes())
        view = self.view()
        self.assertEqual(1, view['batch']['total_files'])
        self.assertFalse(view['batch']['historical'])
        self.assertEqual(1, view['batch']['imported_files'])
        self.assertNotIn('recent_result', view)
        self.assertEqual('Camera-serial/history.MP4', self.store.get(old['id'])['remote_path'])

    def test_complete_bytes_do_not_mean_formal_archive_and_bad_cache_is_not_imported(self):
        self.settings.set_enabled(True)
        self.cache()
        self.store.update(self.row['id'], state='publishing')
        self.store.stage_status('upload', phase='publishing', current_file_id=self.row['id'], current_bytes=32)
        view = self.view()
        self.assertEqual(100, view['batch']['percent'])
        self.assertEqual(0, view['batch']['completed_files'])
        self.assertEqual(1, view['batch']['imported_files'])
        self.assertIn('确认归档', json.dumps(progress_card(view, 300), ensure_ascii=False))
        self.assertNotIn('本轮已归档', progress_card(view, 300)['header']['title']['content'])
        previous = self.path.stat().st_mtime_ns
        self.path.write_bytes(b'z' * 32)
        # The display cache invalidates on metadata changes; make that change
        # explicit rather than depending on the tmpfs clock resolution.
        os.utime(self.path, ns=(previous + 1_000_000, previous + 1_000_000))
        view = self.view()
        self.assertEqual(0, view['batch']['imported_files'])
        self.assertTrue(view['attention']['required'])
        self.assertFalse(view['unplug']['ready'])

    def test_cleanup_uses_observations_not_the_current_delete_switch(self):
        self.cache()
        self.store.update(self.row['id'], state='uploaded', cleanup='deleted')
        view = self.view()
        self.assertFalse(view['policy']['delete_source'])
        self.assertEqual(1, view['cleanup']['source_deleted'])
        self.assertEqual(1, view['cleanup']['cache_pending'])
        text = cleanup_text(view['cleanup'])
        self.assertIn('原片：已删除 1 个', text)
        self.assertIn('缓存：待清理 1 个', text)
        self.assertNotIn('原片保留', text)
        self.path.unlink()
        self.assertEqual(1, self.view()['cleanup']['cache_cleared'])
        outcome = cleanup_summary([dict(view['files'][0], cache_state='unavailable')])
        self.assertEqual(0, outcome['cache_cleared'])
        self.assertEqual(1, outcome['cache_unknown'])

    def test_source_work_prevents_unplug_even_when_all_cache_is_complete(self):
        self.settings.set_enabled(True)
        self.cache()
        self.store.status(inventory={'scanned': True, 'verified': True, 'batch_id': self.batch,
            'destination': self.config.destination_id, 'serial': self.config.source['serial']}, source_work=True)
        view = self.view()
        self.assertEqual(1, view['batch']['imported_files'])
        self.assertFalse(view['unplug']['can_disconnect'])
        self.assertIn('相机正在执行读写操作', json.dumps(status_content(view), ensure_ascii=False))
        self.store.status(source_work=False, source={'online': True, 'checked_at': now()})
        self.assertTrue(self.view()['unplug']['ready'])
