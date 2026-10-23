"""Owner confirmation and cancellation use only isolated byte-sized fixtures."""
import json
import threading
import time
import unittest
from unittest.mock import Mock

from media_importer import selected_imports as operations
from media_importer.bot import Bot
from media_importer.config import Config
from media_importer.model import ConfigError, SourceError
from media_importer.service import Service
import test_camera_operations
import test_bot
from test_service import FakeSource, footage


class SelectedImportTests(unittest.TestCase):
    setUp = test_camera_operations.CameraOperationTests.setUp
    tearDown = test_camera_operations.CameraOperationTests.tearDown

    def parent(self):
        self.config = Config.from_values(self.settings.read()['config'])
        self.service = Service(self.config, self.store, self.source, self.remote, threading.Event())
        return self.service

    def browse(self):
        self.counter += 1
        op = operations.enqueue(self.store, self.settings, str(self.counter), self.owner)
        self.assertTrue(operations.process_one(self.parent(), self.settings))
        op = operations.get(self.store.db, op['id'], self.owner)
        self.assertEqual('choosing', op['state'], op['result'])
        return op

    def ready(self):
        op = self.browse()
        op = operations.choose(self.store, self.settings, op['id'], self.owner, [0], 0, 0)
        return operations.review(self.store, self.settings, op['id'], self.owner)

    def test_browse_does_not_create_tasks_or_change_preferences(self):
        op = self.browse()
        self.assertFalse(self.store.files())
        self.assertEqual(self.before, self.path.read_bytes())
        self.assertFalse(self.source.downloads or self.source.deletes or self.remote.uploads)
        self.assertEqual([], json.loads(op['plan'])['selected'])

    def test_full_sync_lane_can_browse_but_cannot_start_confirmed_transfer(self):
        # Simulate a stale lane whose coordinator is still cancelling after the
        # owner has stopped full sync and confirmed a new selected transfer.
        self.settings.set_enabled(True)
        old_lane = self.parent()
        self.settings.set_enabled(False)
        op = self.ready()
        operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])
        before = self.path.read_bytes()
        for state in ('queued_transfer', 'running', 'waiting'):
            with self.store.db:
                self.store.db.execute('UPDATE selected_imports SET state=? WHERE id=?', (state, op['id']))
            saved = operations.get(self.store.db, op['id'])
            self.assertFalse(operations.process_one(old_lane, self.settings, browsing_only=True))
            self.assertEqual(saved, operations.get(self.store.db, op['id']))
            self.assertFalse(self.store.files())
            self.assertFalse(self.source.downloads or self.remote.uploads or self.source.deletes)
            self.assertEqual(before, self.path.read_bytes())
        # Only the top-level daemon, after joining the old lanes, starts it.
        self.assertTrue(operations.process_one(self.parent(), self.settings))
        self.assertEqual('done', operations.get(self.store.db, op['id'])['state'])

    def test_full_sync_lane_browsing_filter_still_handles_read_only_scan(self):
        self.settings.set_enabled(True)
        op = operations.enqueue(self.store, self.settings, 'read-only-lane', self.owner)
        self.assertTrue(operations.process_one(self.parent(), self.settings, browsing_only=True))
        self.assertEqual('choosing', operations.get(self.store.db, op['id'])['state'])
        self.assertTrue(self.settings.read()['config']['backup']['enabled'])
        self.assertFalse(self.store.files())
        self.assertFalse(self.source.downloads or self.remote.uploads or self.source.deletes)

    def test_browsing_marks_source_work_and_reports_activity_then_clears_flags(self):
        op = operations.enqueue(self.store, self.settings, 'scan-activity', self.owner)
        parent = self.parent()
        parent.activity = Mock()
        old_activity = Mock()
        self.source.on_activity = old_activity
        def observing(_):
            status = self.store.get_meta('status')
            self.assertTrue(status['source_work'])
            self.assertTrue(status['maintenance_running'])
            self.source.on_activity()
        self.source.on_snapshot = observing
        operations.process_one(parent, self.settings, browsing_only=True)
        self.assertEqual('choosing', operations.get(self.store.db, op['id'])['state'])
        parent.activity.touch.assert_called_with('import')
        self.assertIs(old_activity, self.source.on_activity)
        self.assertFalse(self.store.get_meta('status')['source_work'])
        self.assertFalse(self.store.get_meta('status')['maintenance_running'])

    def test_failed_browsing_clears_work_flags_and_preserves_media(self):
        op = operations.enqueue(self.store, self.settings, 'scan-failure', self.owner)
        def failed(_):
            self.assertTrue(self.store.get_meta('status')['source_work'])
            raise SourceError('fixture inventory error')
        self.source.on_snapshot = failed
        operations.process_one(self.parent(), self.settings, browsing_only=True)
        self.assertEqual('paused', operations.get(self.store.db, op['id'])['state'])
        self.assertFalse(self.store.get_meta('status')['source_work'])
        self.assertFalse(self.store.get_meta('status')['maintenance_running'])
        self.assertFalse(self.source.downloads or self.remote.uploads or self.source.deletes)
        self.assertFalse(self.store.files())

    def test_selection_requires_current_owner_page_and_revision(self):
        op = self.browse()
        with self.assertRaises(ConfigError):
            operations.choose(self.store, self.settings, op['id'], 'someone-else', [0], 0, 0)
        for ids in ([12], [0, 0], [True], ['0']):
            with self.assertRaises(ConfigError):
                operations.choose(self.store, self.settings, op['id'], self.owner, ids, 0, 0)
        operations.choose(self.store, self.settings, op['id'], self.owner, [0], 0, 0)
        with self.assertRaises(ConfigError):
            operations.choose(self.store, self.settings, op['id'], self.owner, [], 0, 0)

    def test_confirm_requires_preview_and_refuses_full_sync(self):
        op = self.browse()
        with self.assertRaises(ConfigError):
            operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])
        self.settings.set_enabled(True)
        op = self.ready()
        with self.assertRaisesRegex(ConfigError, '全量备份'):
            operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])
        self.assertFalse(self.store.files())

    def test_choice_has_expiry_and_does_not_override_changed_settings(self):
        op = self.ready()
        with self.store.db:
            self.store.db.execute('UPDATE selected_imports SET updated=? WHERE id=?',
                                  (time.time() - 301, op['id']))
        with self.assertRaisesRegex(ConfigError, '5 分钟'):
            operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])
        op = self.ready()
        self.settings.update_preferences({'limits': {'max_cache_gib': 55}})
        with self.assertRaises(ConfigError):
            operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])

    def test_confirmed_selection_runs_once_and_preserves_all_originals(self):
        self.settings.update_preferences({'backup': {'archive_previews': False,
                                                    'delete_source_after_import': True}})
        self.source = FakeSource([footage(n, primary=p) for n in (1, 2) for p in (True, False)])
        before = self.path.read_bytes()
        op = self.ready()
        operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])
        operations.process_one(self.parent(), self.settings)
        result = operations.get(self.store.db, op['id'])
        self.assertEqual('done', result['state'], result['result'])
        self.assertEqual(1, len(self.source.downloads))
        self.assertEqual(1, len(self.remote.uploads))
        self.assertFalse(self.source.deletes)
        self.assertEqual(4, len(self.source.assets))
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.store.files())
        operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])
        self.assertFalse(operations.process_one(self.parent(), self.settings))

    def test_smb_offline_keeps_complete_cache_and_cancel_keeps_tasks(self):
        self.settings.update_preferences({'backup': {'archive_previews': False}})
        op = self.ready()
        operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])
        self.remote.online = False
        operations.process_one(self.parent(), self.settings)
        op = operations.get(self.store.db, op['id'])
        self.assertEqual('waiting', op['state'], op['result'])
        cached = {p.name: p.read_bytes() for p in self.store.cache.glob('*.bin')}
        self.assertTrue(cached)
        with self.store.db:
            operations.cancel(self.store.db, op['id'], self.owner)
        operations.process_one(self.parent(), self.settings)
        self.assertEqual('paused', operations.get(self.store.db, op['id'])['state'])
        self.assertEqual(cached, {p.name: p.read_bytes() for p in self.store.cache.glob('*.bin')})
        self.remote.online = True
        operations.resume(self.store, self.settings, op['id'], self.owner)
        operations.process_one(self.parent(), self.settings)
        self.assertEqual('done', operations.get(self.store.db, op['id'])['state'])
        self.assertEqual(1, len(self.source.downloads))
        self.assertFalse(self.source.deletes)

    def test_settings_change_pauses_confirmed_task_before_any_transfer(self):
        op = self.ready()
        operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])
        self.settings.update_preferences({'limits': {'max_cache_gib': 55}})
        operations.process_one(self.parent(), self.settings)
        self.assertEqual('paused', operations.get(self.store.db, op['id'])['state'])
        self.assertFalse(self.source.downloads or self.source.deletes)

    def test_stop_menu_stops_full_sync_even_when_browse_is_queued(self):
        self.settings.set_enabled(True)
        bot = Bot(self.settings, self.store.directory, sender=Mock(return_value='om_test'),
                  updater=Mock())
        operations.enqueue(self.store, self.settings, 'browse', bot.scope)
        message = test_bot.BotTests.message(self, '立即停止')
        self.assertTrue(bot.receive_message(message))
        bot.process_one()
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])

    def test_cancelled_draft_cannot_be_resumed_as_transfer(self):
        op = self.ready()
        with self.store.db:
            operations.cancel(self.store.db, op['id'], self.owner)
        with self.assertRaises(ConfigError):
            operations.resume(self.store, self.settings, op['id'], self.owner)
        with self.assertRaises(ConfigError):
            operations.confirm(self.store, self.settings, op['id'], self.owner, op['choice_revision'])


if __name__ == '__main__':
    unittest.main()
