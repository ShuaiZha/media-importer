"""Command/lifecycle contracts using disposable records; no camera or external sends."""
import copy
import json
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import test_bot
import test_notifications
from media_importer.bot_routes import COMMANDS, MENUS, MENU_ACTIONS, TEXT_ACTIONS
from media_importer.bot_cards import event_card, format_time, progress_card, detail_content
from media_importer.camera_cards import _source
from media_importer.message_facts import progress_rows, selected_view
from media_importer.model import now
from media_importer.notifications import NotificationWorker, _digest
from media_importer.source_connection import observe, decide
from test_source_connection import device


def rendered(card):
    return json.dumps(card, ensure_ascii=False)


class CommandContracts(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    message = test_bot.BotTests.message
    callback = test_bot.BotTests.callback

    def process(self):
        with patch('media_importer.bot.time.monotonic', side_effect=[1, 6]):
            return self.bot.process_one()

    def test_menu_text_and_legacy_queries_resolve_without_starting_camera(self):
        self.assertEqual(['备份', '相机', '设置'], [name for name, _ in MENUS])
        for _, entries in MENUS:
            self.assertLessEqual(len(entries), 5)
            for key in entries:
                label, action, _ = COMMANDS[key]
                self.assertEqual(MENU_ACTIONS[key], TEXT_ACTIONS[label])
                self.assertTrue(self.bot.supported(action), key)
        before = self.path.read_bytes()
        for i, command in enumerate(('相机页面', '拍摄控制', '实时预览', '同步状态', '不认识的命令')):
            self.bot.receive_message(self.message(command, event_id=f'query-{i}'))
            self.assertTrue(self.process())
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([], self.store.files())
        for table in ('capture_requests', 'preview_requests'):
            if self.store.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                self.assertEqual(0, self.store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        self.assertIsNone(self.store.get_meta('source_connect_request'))
        guide = self.sender.call_args.args[1]
        self.assertEqual('未识别的指令', guide['header']['title']['content'])
        self.assertEqual(1, len(guide['elements']))

    def test_camera_approval_preserves_both_possible_sync_settings(self):
        for enabled in (False, True):
            self.settings.set_enabled(enabled)
            candidate = device(serial='C0000000000123' if enabled else 'C0000000000124')
            connection = observe(self.store, candidate)
            before = self.path.read_bytes()
            decide(self.store, self.settings, connection['id'], True)
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(enabled, self.settings.read()['config']['backup']['enabled'])

    def test_different_callback_ids_for_same_mutation_execute_once(self):
        value = self.bot.action({'changes': {'backup': {'archive_previews': True}}, 'page': 'previews'}, self.settings.read()['revision'])
        self.bot.receive_card(self.callback(value, event_id='first'))
        again = self.bot.receive_card(self.callback(value, event_id='second'))
        self.assertIn('原回执', again['toast']['content'])
        with self.bot.db() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM bot_jobs').fetchone()[0])
        self.process()
        self.assertTrue(self.settings.read()['config']['backup']['archive_previews'])
        self.assertFalse(self.settings.read()['config']['backup']['delete_source_after_import'])

    def test_user_start_gets_one_progress_card_with_notifications_disabled(self):
        self.bot.receive_message(self.message('开启同步'))
        self.assertTrue(self.process())
        self.sender.assert_not_called()
        from media_importer.status import snapshot
        sender, updater = Mock(return_value='om_manual_progress'), Mock()
        worker = NotificationWorker(self.root / 'data', self.settings, threading.Event(), sender=sender, updater=updater)
        worker.tick(snapshot(self.root / 'data', self.settings))
        sender.assert_called_once()
        self.assertFalse(self.settings.read()['config']['notifications']['enabled'])
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(), configuration={'applied_revision': self.settings.read()['revision'], 'pending': False})
        worker.tick(snapshot(self.root / 'data', self.settings))
        self.assertEqual(1, sender.call_count)
        self.assertEqual('om_manual_progress', updater.call_args.args[1])
        self.settings.set_enabled(False)
        self.store.status(worker_heartbeat_at=now(), configuration={'applied_revision': self.settings.read()['revision'], 'pending': False})
        worker.tick(snapshot(self.root / 'data', self.settings))
        self.assertIn('备份已停止', rendered(updater.call_args.args[2]))

    def test_stop_menu_updates_existing_progress_without_stopping_preview_or_recording(self):
        self.settings.set_enabled(True)
        recipient = _digest([self.bot.credentials['app_id'], 'open_id', self.bot.owner])
        self.store.set_meta('notification_progress_card:test', {'message_id': 'om_progress', 'recipient': recipient, 'updated_at': time.time()})
        self.store.set_meta('live_preview', {'state': 'running', 'id': 'a' * 32})
        self.bot.receive_message(self.message('停止同步'))
        self.assertTrue(self.process())
        self.sender.assert_not_called()
        self.assertEqual('om_progress', self.updater.call_args.args[1])
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
        self.assertEqual('running', self.store.get_meta('live_preview')['state'])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM capture_requests").fetchone())

    def test_advanced_notification_form_does_not_reset_main_preferences(self):
        from media_importer.bot_settings import parse
        self.settings.update_preferences({'notifications': {'enabled': True, 'progress_interval_seconds': 600, 'keyword': 'test'}})
        changes = parse('notification_advanced', {'connection_events': 'off', 'keyword': 'custom'}, 4)
        self.settings.update_preferences(changes)
        self.assertEqual({'enabled': True, 'progress_interval_seconds': 600, 'connection_events': False, 'keyword': 'custom'},
                         self.settings.read()['config']['notifications'])


class MessageFacts(unittest.TestCase):
    def test_shared_counts_wait_for_formal_commit_and_keep_details_out_of_summary(self):
        result = {'total_files': 2, 'total_bytes': 2048, 'imported_files': 2, 'uploaded_files': 1,
                  'import_bytes': 2048, 'upload_bytes': 2048,
                  'stages': {'upload': {'phase': 'publishing', 'current_file': 'example.MP4', 'speed_bytes_per_second': 100}}}
        view = selected_view(result, 'running')
        rows = progress_rows(view)
        self.assertEqual((100, 2), (rows[0]['percent'], rows[0]['done']))
        self.assertEqual((99.9, 1, '归档确认中'), (rows[1]['percent'], rows[1]['done'], rows[1]['phase']))
        self.assertNotIn('example.MP4', rendered(progress_card(view, 300)))
        self.assertIn('example.MP4', rendered(detail_content(view)))

    def test_stale_health_is_not_normal_and_events_have_no_extra_timestamp(self):
        identity = {'model_name': 'Camera', 'transport': 'usb', 'telemetry': {
            'available': True, 'battery_percent': 100, 'overheating': True,
            'ready': True, 'recording': False, 'busy': False, 'storage_status': 'ready'}}
        body = rendered(_source(identity, stale=True))
        self.assertIn('状态已过期', body)
        self.assertNotIn('100%', body)
        self.assertNotIn('就绪', body)
        self.assertIn('温度告警 未知', rendered(_source(identity, stale=True, details=True)))
        card = event_card('test', 'unused', 'unused', {}, updated_at=now())
        self.assertEqual('通知测试成功', card['header']['title']['content'])
        self.assertNotIn('更新 ', rendered(card))
        self.assertNotIn('北京时间', rendered(card))

    def test_snapshot_time_changes_date_granularity_without_changing_stored_time(self):
        moment = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
        for stamp, expected in [('2026-10-22T08:28:00+00:00', '16:28'),
                                ('2026-10-21T08:28:00+00:00', '10-21 16:28'),
                                ('2025-10-21T08:28:00+00:00', '2025-10-21 16:28')]:
            self.assertEqual(expected, format_time(stamp, now=moment))


class NotificationLifecycle(unittest.TestCase):
    setUp = test_notifications.WorkerTests.setUp
    tearDown = test_notifications.WorkerTests.tearDown
    make_worker = test_notifications.WorkerTests.make_worker
    tick = test_notifications.WorkerTests.tick
    archive = test_notifications.WorkerTests.archive

    def test_requested_progress_is_sent_while_background_has_not_applied_settings(self):
        from media_importer.outbox import initialize
        initialize(self.store.db)
        scope = self.store.get_meta('notification_state')['scope']
        with self.store.db:
            self.worker._enqueue(self.store.db, 'manual', 'requested_progress', scope, '', '', self.time)
        self.view['configuration']['pending'] = True
        self.tick()
        self.sender.assert_called_once()
        self.assertIn('等待生效', rendered(self.sender.call_args.args[1]))

    def test_simultaneous_import_and_archive_finish_sends_only_completion(self):
        self.tick()
        self.archive(cleanup='retained')
        self.view['batch'].update(imported_files=2, completed_files=2, percent=100, import_percent=100)
        self.view['unplug'] = {'ready': True, 'can_disconnect': True}
        self.tick()
        titles = [c.args[1]['header']['title']['content'] for c in self.sender.call_args_list]
        self.assertEqual(['归档完成'], titles)
