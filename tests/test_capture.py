"""Camera control safety and owner-only receipts; all media are tiny fixtures."""
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_bot
import test_runtime
from test_service import FakeSource, FakeDestination, footage
from test_source_connection import device
from test_wireless_connections import wireless
from media_importer import capture
from media_importer.config import Config
from media_importer.model import ConfigError, Cancelled, now
from media_importer.source_connection import observe, decide
from media_importer.service import Service
from media_importer.store import Store
from media_importer.runtime import Control


class CaptureTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    callback = test_bot.BotTests.callback
    message = test_bot.BotTests.message

    def connected(self):
        connection = observe(self.store, wireless())
        decide(self.store, self.settings, connection['id'], True)
        self.settings.set_enabled(False)
        return connection

    def service(self):
        return Service(Config.from_values(self.settings.read()['config']), self.store,
                       FakeSource([]), FakeDestination(), threading.Event())

    def test_duplicate_button_nonce_is_one_command_even_after_completion(self):
        state = self.connected()
        with patch('media_importer.connections.wireless_supported', return_value=True):
            op = capture.enqueue(self.store, 'owner', 'nonce', 'photo', state['id'])
            same = capture.enqueue(self.store, 'owner', 'nonce', 'photo', state['id'])
            self.assertEqual(op['id'], same['id'])
            with patch('media_importer.adapters.source_link', return_value={'recording': False, 'photos': 1}) as radio:
                capture.process_one(self.service(), self.settings)
                same = capture.enqueue(self.store, 'owner', 'nonce', 'photo', state['id'])
                self.assertFalse(capture.process_one(self.service(), self.settings))
            radio.assert_called_once()
            self.assertEqual('done', same['state'])
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
        self.assertFalse(self.store.files())

    def test_stale_session_and_expired_requests_do_not_issue_commands(self):
        state = self.connected()
        with patch('media_importer.connections.wireless_supported', return_value=True):
            op = capture.enqueue(self.store, 'owner', 'first', 'start_recording', state['id'])
        with self.store.db:
            self.store.db.execute('UPDATE capture_requests SET created=?', (time.time() - 31,))
        with patch('media_importer.adapters.source_link') as radio:
            capture.process_one(self.service(), self.settings)
        radio.assert_not_called()
        self.assertFalse(json.loads(capture.get(self.store.db, op['id'], 'owner')['result'])['uncertain'])
        observe(self.store, device())
        with self.assertRaises(ConfigError):
            capture.enqueue(self.store, 'owner', 'new', 'photo', state['id'])

    def test_restart_queries_state_and_never_replays_shutter(self):
        state = self.connected()
        with patch('media_importer.connections.wireless_supported', return_value=True):
            op = capture.enqueue(self.store, 'owner', 'nonce', 'photo', state['id'])
        with self.store.db:
            self.store.db.execute("UPDATE capture_requests SET state='running'")
        with patch('media_importer.connections.wireless_supported', return_value=True), \
                patch('media_importer.adapters.source_link', return_value={'recording': False}) as radio:
            capture.process_one(self.service(), self.settings)
        self.assertEqual('state', radio.call_args.args[1])
        self.assertTrue(json.loads(capture.get(self.store.db, op['id'], 'owner')['result'])['uncertain'])

    def test_usb_and_unsupported_platform_do_not_offer_capture(self):
        with patch('media_importer.connections.wireless_supported', return_value=True):
            self.assertEqual([], capture.capabilities(device())['commands'])
        with patch('media_importer.connections.wireless_supported', return_value=False):
            self.assertEqual([], capture.capabilities(wireless())['commands'])

    def test_pending_capture_suspends_source_deletion(self):
        state = self.connected()
        with patch('media_importer.connections.wireless_supported', return_value=True):
            capture.enqueue(self.store, 'owner', 'nonce', 'photo', state['id'])
        self.settings.update_preferences({'backup': {'delete_source_after_import': True}})
        self.assertFalse(self.service().allowed('delete_source_after_import'))

    def test_owner_callbacks_progress_and_completion_update_the_same_card(self):
        state = self.connected()
        action = {'capture': {'version': 1, 'action': 'photo', 'session_id': state['id'], 'nonce': 'a' * 32}}
        token = self.bot.action(action)
        self.assertEqual('error', self.bot.receive_card(self.callback(token, owner='another'))['toast']['type'])
        with patch('media_importer.connections.wireless_supported', return_value=True):
            self.bot.receive_card(self.callback(token))
            self.sender.return_value = 'om_capture_fixture'
            self.assertTrue(self.bot.process_one())
            with patch('media_importer.adapters.source_link', return_value={'recording': False, 'mode': 'photo', 'observed_at': now()}):
                capture.process_one(self.service(), self.settings)
            with self.bot.db() as db: db.execute('UPDATE bot_jobs SET retry_at=0')
            self.assertTrue(self.bot.process_one())
        self.sender.assert_called_once()
        self.assertEqual('om_capture_fixture', self.updater.call_args.args[1])
        self.assertIn('拍照已完成', json.dumps(self.updater.call_args.args[-1], ensure_ascii=False))
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])

    def test_menu_command_returns_control_without_touching_settings(self):
        before = self.path.read_bytes()
        self.bot.receive_message(self.message('相机控制'))
        self.assertTrue(self.bot.process_one())
        self.assertIn('画面与拍摄', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.assertEqual(before, self.path.read_bytes())


class PreemptionTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown

    def test_capture_cancels_only_partial_import_while_complete_cache_uploads(self):
        control = Control(self.settings, self.stop)
        config = control.adopt()
        assets = [footage(), footage(2)]
        source, remote = FakeSource(assets), FakeDestination()
        upload_started, captured, release = threading.Event(), threading.Event(), threading.Event()
        original_download, original_upload = source.download, remote.upload
        tries = []
        def download(asset, path, progress):
            tries.append(asset.identity)
            if asset.identity == assets[1].identity and tries.count(asset.identity) == 1:
                self.assertTrue(upload_started.wait(3))
                path.write_bytes(b'partial')
                control.source_interrupt.set()
                raise Cancelled('capture priority')
            return original_download(asset, path, progress)
        def upload(*args, **kwargs):
            upload_started.set()
            self.assertTrue(captured.wait(3))
            self.assertFalse(control.transfer_stop.is_set())
            return original_upload(*args, **kwargs)
        def operation(task, settings):
            if tries.count(assets[1].identity) == 1 and not captured.is_set():
                self.assertFalse(any(task.store.cache.glob('*.download')))
                captured.set()
                return True
            return False
        source.download, remote.upload = download, upload
        with patch('media_importer.capture.process_one', side_effect=operation):
            result = Service(config, self.store, source, remote, self.stop, control).safe_cycle()
        self.assertTrue(result['complete'])
        self.assertTrue(captured.is_set())
        self.assertEqual(2, tries.count(assets[1].identity))
        self.assertEqual(2, len(remote.uploads))
        self.assertFalse(source.deletes)
        self.assertFalse(any(self.store.cache.glob('*.download')))


class OfflineControlTests(unittest.TestCase):
    setUp = CaptureTests.setUp
    tearDown = CaptureTests.tearDown
    connected = CaptureTests.connected
    service = CaptureTests.service
    def test_lost_wifi_still_allows_only_trusted_read_and_stop_over_ble(self):
        state = self.connected()
        state.update(state='offline', online=False)
        self.store.set_meta('source_connection', state)
        with patch('media_importer.connections.wireless_supported', return_value=True):
            for action in ('photo', 'start_recording', 'mode_video'):
                with self.assertRaises(ConfigError):
                    capture.enqueue(self.store, 'owner', action, action, state['id'])
            op = capture.enqueue(self.store, 'owner', 'stop', 'stop_recording', state['id'])
            with patch('media_importer.adapters.source_link', return_value={'recording': False}) as radio:
                capture.process_one(self.service(), self.settings)
        self.assertEqual('stop_recording', radio.call_args.args[1])
        self.assertEqual('done', capture.get(self.store.db, op['id'], 'owner')['state'])

    def test_unknown_offline_identity_never_offers_controls(self):
        state = self.connected()
        state.update(state='offline', online=False)
        state['device']['serial_number'] = 'C0000000000999'
        self.store.set_meta('source_connection', state)
        with patch('media_importer.connections.wireless_supported', return_value=True):
            self.assertEqual([], capture.allowed_commands(self.store, state))
