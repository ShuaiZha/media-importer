"""Manual connection shares discovery, with no transfer or camera side effects."""
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_bot
from test_source_connection import device
from test_wireless_connections import wireless
from media_importer.bot import Bot
from media_importer.bot_routes import MENUS, MENU_ACTIONS, TEXT_ACTIONS
from media_importer.connections import ConnectionManager
from media_importer.source_connection import observe, decide


class ManualConnectionTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    message = test_bot.BotTests.message

    def request(self, event='connect', text='连接相机'):
        self.bot.receive_message(self.message(text, event_id=event))
        with patch('media_importer.connections.wireless_supported', return_value=True):
            return self.bot.process_one()

    def bind_offline(self):
        state = observe(self.store, wireless())
        decide(self.store, self.settings, state['id'], True)
        self.store.set_meta('source_connection', {**state, 'state': 'offline', 'online': False})

    def ready_jobs(self):
        with self.bot.db() as db:
            db.execute('UPDATE bot_jobs SET retry_at=0')

    def assert_no_business(self, original):
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([], self.store.files())
        self.assertIsNone(self.store.get_meta('live_preview'))
        self.assertIsNone(self.store.get_meta('capture_card_watch'))
        for table in ('capture_requests', 'preview_requests'):
            if self.store.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                self.assertEqual(0, self.store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0])

    def test_menu_and_text_aliases_use_one_connection_command(self):
        self.assertIn('camera.connect', dict(MENUS)['相机'])
        for name in ('连接相机', '立即连接相机', '连接无线相机'):
            self.assertEqual(MENU_ACTIONS['camera.connect'], TEXT_ACTIONS[name])

    def test_already_connected_usb_needs_no_radio_and_no_settings_change(self):
        state = observe(self.store, device()); decide(self.store, self.settings, state['id'], True)
        original = self.path.read_bytes()
        self.bot.receive_message(self.message('连接相机'))
        with patch('media_importer.connections.wireless_supported', return_value=False):
            self.bot.process_one()
        response = self.sender.call_args.args[1]
        self.assertEqual('相机已连接', response['header']['title']['content'])
        self.assertIn('USB', json.dumps(response, ensure_ascii=False))
        self.assertIsNone(self.store.get_meta('source_connect_request'))
        self.assert_no_business(original)

    def test_usb_only_offline_does_not_start_wireless(self):
        self.settings.update_preferences({'backup': {'source_transport': 'usb'}})
        original = self.path.read_bytes(); self.request()
        self.assertEqual('等待 USB 连接', self.sender.call_args.args[1]['header']['title']['content'])
        self.assertIsNone(self.store.get_meta('source_connect_request'))
        self.assert_no_business(original)

    def test_connection_progress_survives_restart_updates_original_card_and_preserves_sync_off(self):
        self.bind_offline(); original = self.path.read_bytes()
        self.sender.return_value = 'om_connect_test'
        self.request()
        request = self.store.get_meta('source_connect_request')
        self.assertAlmostEqual(time.time() + 150, request['until'], delta=2)
        self.assertEqual('正在连接相机', self.sender.call_args.args[1]['header']['title']['content'])
        self.bot = Bot(self.settings, self.store.directory, sender=self.sender, updater=self.updater)
        manager = ConnectionManager(self.store, self.settings, threading.Event())
        manager.retry_at = time.monotonic() + 999
        def radio(kind, action, *args):
            return None if action == 'probe' else wireless()
        with patch('media_importer.connections.discover_source', return_value=None), \
                patch('media_importer.connections.wireless_supported', return_value=True), \
                patch('media_importer.connections.source_link', side_effect=radio) as link:
            manager.tick()
        self.assertEqual(['probe', 'connect'], [c.args[1] for c in link.call_args_list])
        self.ready_jobs(); self.bot.process_one()
        self.sender.assert_called_once()
        self.assertEqual('om_connect_test', self.updater.call_args.args[1])
        self.assertEqual('相机已连接', self.updater.call_args.args[2]['header']['title']['content'])
        self.assert_no_business(original)

    def test_duplicate_clicks_reuse_request_without_extending_timeout_or_extra_cards(self):
        self.bind_offline(); self.sender.return_value = 'om_connect_test'
        self.request('first'); original = self.store.get_meta('source_connect_request')
        self.request('second')
        self.assertEqual(original, self.store.get_meta('source_connect_request'))
        self.sender.assert_called_once()
        with self.bot.db() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM bot_jobs WHERE state='waiting'").fetchone()[0])

    def test_unchanged_connecting_state_does_not_rewrite_card(self):
        self.bind_offline(); self.sender.return_value = 'om_connect_test'; self.request()
        self.ready_jobs()
        with patch('media_importer.connections.wireless_supported', return_value=True):
            self.assertFalse(self.bot.process_one())
        self.sender.assert_called_once(); self.updater.assert_not_called()

    def test_timeout_finishes_receipt_and_does_not_start_another_attempt(self):
        self.bind_offline(); self.sender.return_value = 'om_connect_test'; self.request()
        until = time.time() - 1
        self.store.set_meta('source_connect_request', {'until': until})
        with self.bot.db() as db:
            row = db.execute('SELECT id,payload FROM bot_jobs').fetchone()
            payload = json.loads(row['payload']); payload['connect_until'] = until
            db.execute('UPDATE bot_jobs SET payload=?,retry_at=0 WHERE id=?', (json.dumps(payload), row['id']))
        with patch('media_importer.connections.wireless_supported', return_value=True):
            self.bot.process_one()
        self.assertEqual('相机连接超时', self.updater.call_args.args[2]['header']['title']['content'])
        self.assertEqual(until, self.store.get_meta('source_connect_request')['until'])

    def test_new_camera_still_requires_confirmation_with_sync_off(self):
        observe(self.store, wireless('C0000000000456'))
        original = self.path.read_bytes(); self.request()
        response = self.sender.call_args.args[1]
        self.assertEqual('确认相机', response['header']['title']['content'])
        self.assertNotIn('相机已连接', json.dumps(response, ensure_ascii=False))
        self.assertIsNone(self.store.get_meta('source_connect_request'))
        self.assert_no_business(original)

    def test_unavailable_platform_has_actionable_receipt_without_request(self):
        original = self.path.read_bytes()
        self.bot.receive_message(self.message('连接相机'))
        with patch('media_importer.connections.wireless_supported', return_value=False):
            self.bot.process_one()
        self.assertIn('请使用 USB', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.assertIsNone(self.store.get_meta('source_connect_request'))
        self.assert_no_business(original)
