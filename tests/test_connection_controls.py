import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from media_importer.adapters.gopro import GoProSource
from media_importer.bot import Bot
from media_importer.config import Config, ConfigError, preferences
from media_importer.notifications import _meta
from media_importer.runtime import Control, Reconfigure
from media_importer.settings import Settings
from media_importer.status import snapshot
from media_importer.store import Store
from media_importer.model import now
from test_bot import ENV
import test_notifications as notification_tests
from test_service import configuration, footage, FakeSource, FakeDestination
from media_importer.service import Service


class ConnectionNotificationTests(unittest.TestCase):
    make_worker = notification_tests.WorkerTests.make_worker
    tick = notification_tests.WorkerTests.tick
    tearDown = notification_tests.WorkerTests.tearDown

    def setUp(self):
        notification_tests.WorkerTests.setUp(self)
        self.values['backup']['enabled'] = False
        self.values['notifications'].update(enabled=False, connection_events=True)
        self.view['stages'] = {name: {'phase': 'paused'} for name in ('import', 'upload')}
        self.view['controls'] = {name: {'requested_stop': True} for name in ('import', 'upload')}

    def observe(self, online, advance=10, **fields):
        self.time += advance
        self.view['health']['source'] = {'online': online, 'stale': False, 'transport': self.values['source']['transport'],
            'serial_number': self.values['source']['serial'],
            'checked_at': datetime.fromtimestamp(self.time, timezone.utc).isoformat(), **fields}
        self.tick()

    def test_connected_notifies_with_sync_off_once_and_survives_restart(self):
        self.observe(False)
        self.sender.assert_not_called()
        self.observe(True)
        self.assertEqual(1, self.sender.call_count)
        text = json.dumps(self.sender.call_args.args[1], ensure_ascii=False)
        self.assertIn('相机已连接', text)
        self.assertIn('同步已关闭', text)
        self.assertIn('USB 直连', text)
        self.observe(True)
        self.worker = self.make_worker()
        self.observe(True)
        self.assertEqual(1, self.sender.call_count)

    def test_disconnect_requires_three_distinct_checks_and_temporary_failure_is_silent(self):
        self.observe(True)
        self.sender.reset_mock()
        self.observe(False)
        self.tick(5)
        self.tick(5)
        self.sender.assert_not_called()
        self.observe(True)
        self.sender.assert_not_called()
        self.observe(False)
        self.observe(False)
        self.sender.assert_not_called()
        self.observe(False)
        self.assertEqual(1, self.sender.call_count)
        text = json.dumps(self.sender.call_args.args[1], ensure_ascii=False)
        self.assertIn('相机连接已断开', text)
        self.assertIn('不等同于确认 USB 已拔出', text)
        self.observe(False)
        self.assertEqual(1, self.sender.call_count)
        self.observe(True)
        self.assertEqual(2, self.sender.call_count)

    def test_unknown_stale_wrong_identity_or_stopped_worker_never_claim_connection(self):
        self.observe(True, stale=True)
        self.observe(None)
        self.observe(True, serial_number='ANOTHER_CAMERA')
        self.observe(True, transport='wifi')
        self.view['running'] = False
        self.observe(True)
        self.view['running'] = True
        self.view['configuration']['pending'] = True
        self.observe(True)
        self.sender.assert_not_called()

    def test_mode_change_ignores_old_observation_and_reports_new_mode(self):
        self.observe(True)
        self.sender.reset_mock()
        self.values['source']['transport'] = 'wifi'
        self.values['backup']['source_transport'] = 'wifi'
        self.tick(5)
        self.sender.assert_not_called()
        self.observe(True)
        self.assertEqual(1, self.sender.call_count)
        self.assertIn('手动 Wi-Fi', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.assertNotIn('已断开', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))

    def test_failed_disconnect_delivery_is_cancelled_on_reconnect(self):
        self.observe(True)
        self.sender.reset_mock()
        self.sender.side_effect = RuntimeError('offline')
        for _ in range(3):
            self.observe(False)
        self.assertEqual(1, self.sender.call_count)
        self.worker = self.make_worker()
        self.sender.side_effect = None
        self.observe(True)
        self.tick(60)
        self.assertEqual(2, self.sender.call_count)
        self.assertIn('已连接', self.sender.call_args.args[1]['header']['title']['content'])
        row = self.store.db.execute("SELECT state FROM notification_events WHERE kind='disconnected'").fetchone()
        self.assertEqual('cancelled', row[0])

    def test_connection_delivery_retries_without_starting_sync(self):
        self.sender.side_effect = RuntimeError('offline')
        self.observe(True)
        self.worker = self.make_worker()
        self.sender.side_effect = None
        self.observe(True, advance=31)
        self.assertEqual(2, self.sender.call_count)
        delivery = _meta(self.store.db, 'notification_delivery')
        self.assertFalse(delivery['sync_enabled'])
        self.assertEqual('sent', delivery['state'])
        self.assertTrue(all(r['state'] == 'pending' for r in self.store.files()))

    def test_delayed_online_message_is_not_sent_while_link_is_unreachable(self):
        self.sender.side_effect = RuntimeError('offline')
        self.observe(True)
        self.sender.reset_mock()
        self.sender.side_effect = None
        self.observe(False, advance=31)
        self.sender.assert_not_called()
        self.observe(False)
        self.observe(False)
        self.assertEqual(1, self.sender.call_count)
        self.assertIn('已断开', self.sender.call_args.args[1]['header']['title']['content'])

    def test_connection_switch_is_independent_and_cancels_pending_messages(self):
        self.sender.side_effect = RuntimeError('offline')
        self.observe(True)
        self.values['notifications']['connection_events'] = False
        self.sender.reset_mock()
        self.sender.side_effect = None
        self.tick(60)
        self.observe(False)
        self.sender.assert_not_called()
        self.assertEqual(0, self.store.db.execute("SELECT COUNT(*) FROM notification_events WHERE state='pending'").fetchone()[0])
        self.values['notifications']['connection_events'] = True
        self.observe(True)
        self.assertEqual(1, self.sender.call_count)


class TransportSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, ENV, clear=True)
        self.environment.start()
        self.path = self.root / 'settings.json'
        self.path.write_text(json.dumps(preferences(configuration(enabled=False).values)))
        self.settings = Settings(self.path)
        self.store = Store(self.root / 'data')

    def tearDown(self):
        self.store.close()
        self.environment.stop()
        self.tmp.cleanup()

    def test_modes_keep_addresses_identity_records_and_saved_switch(self):
        old = Config.load(self.path)
        batch = self.store.batch(old.destination_id, [footage()])
        before = self.store.files()
        control = Control(self.settings, threading.Event())
        control.adopt()
        self.settings.update_preferences({'backup': {'source_transport': 'wifi'}})
        self.assertNotEqual(control.applied_token, self.settings.token()[1])
        wifi = Config.load(self.path)
        self.assertEqual('wifi', wifi.source['transport'])
        self.assertEqual(old.source['url'], wifi.source['url'])
        self.assertEqual(old.source['wireless_url'], wifi.source['wireless_url'])
        self.assertEqual(wifi.source['wireless_url'], GoProSource(wifi).base)
        self.assertEqual(old.destination_id, wifi.destination_id)
        self.assertFalse(wifi.backup['enabled'])
        self.assertFalse(wifi.backup['delete_source_after_import'])
        self.assertEqual('wifi', Control(Settings(self.path), threading.Event()).adopt().source['transport'])
        self.settings.update_preferences({'backup': {'source_transport': 'usb'}})
        self.assertEqual(old.source['url'], GoProSource(Config.load(self.path)).base)
        self.assertEqual(before, self.store.files())

    def test_running_switch_and_invalid_mode_rejected_without_saving(self):
        self.settings.set_enabled(True)
        before = self.path.read_bytes()
        for mode in ('wifi', 'auto', None):
            with self.assertRaises(ConfigError):
                self.settings.update_preferences({'backup': {'source_transport': mode}})
            self.assertEqual(before, self.path.read_bytes())

    def test_existing_env_mode_is_kept_until_json_choice_is_saved(self):
        raw = json.loads(self.path.read_text())
        raw['backup'].pop('source_transport')
        self.path.write_text(json.dumps(raw))
        os.environ['SOURCE_TRANSPORT'] = 'wifi'
        self.assertEqual('wifi', Config.load(self.path).source['transport'])
        self.settings.update_preferences({'backup': {'source_transport': 'usb'}})
        self.assertEqual('usb', Config.load(self.path).source['transport'])
        self.assertEqual('wifi', os.environ['SOURCE_TRANSPORT'])

    def test_old_usb_observation_is_unknown_after_selecting_wifi(self):
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(), source={'online': True, 'checked_at': now(), 'transport': 'usb'})
        self.settings.update_preferences({'backup': {'source_transport': 'wifi'}})
        source = snapshot(self.store.directory, self.settings)['health']['source']
        self.assertIsNone(source['online'])
        self.assertTrue(source['stale'])

    def test_manual_wifi_is_observed_after_import_and_while_sync_is_off(self):
        self.settings.update_preferences({'backup': {'source_transport': 'wifi'}})
        source, probe = FakeSource([footage()]), FakeSource([footage()])
        source.timeout, probe.timeout, probe.attempts = 20, 20, 3
        observed = threading.Event()
        identify = probe.identify
        def checked():
            observed.set()
            return identify()
        probe.identify = checked
        source.fork = lambda stop: probe
        self.store.status(inventory={'verified': True}, source_work=False)
        service = Service(Config.load(self.path), self.store, source, FakeDestination(), threading.Event())
        with service.monitoring():
            self.assertTrue(observed.wait(2))
            self.assertLessEqual(probe.timeout, 3)
            self.assertEqual(1, probe.attempts)
        self.assertEqual(20, source.timeout)
        self.assertEqual(0, probe.keep_alive_calls)
        self.assertFalse(source.downloads + source.deletes)

    def test_feishu_controls_switch_transport_and_connection_notifications(self):
        sender = Mock()
        bot = Bot(self.settings, self.store.directory, sender=sender)
        for page, label in [('transfer', '切换为手动 Wi-Fi'), ('notifications', '关闭连接提醒')]:
            card = bot.panel(page)
            button = next(b for e in card['elements'] if e['tag'] == 'action' for b in e['actions'] if b['text']['content'] == label)
            with bot.db() as db:
                action = db.execute('SELECT * FROM bot_actions WHERE token=?', (button['value']['token'],)).fetchone()
                payload = json.loads(action['payload'])
                payload['revision'] = action['revision']
            bot.enqueue(label, payload)
            with patch('media_importer.bot.time.monotonic', side_effect=[1, 6]):
                self.assertTrue(bot.process_one())
        config = Config.load(self.path)
        self.assertEqual('wifi', config.source['transport'])
        self.assertFalse(config.values['notifications']['connection_events'])
        self.assertFalse(config.backup['enabled'])
        self.assertFalse(config.backup['delete_source_after_import'])


if __name__ == '__main__':
    unittest.main()
