"""A remembered camera can reconnect after Start without another approval."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_bot
from test_bot_cards import view
from test_source_connection import device
from test_wireless_connections import wireless
from test_service import FakeSource, FakeDestination, footage
from media_importer.bot_cards import progress_card, status_content
from media_importer.config import Config
from media_importer.connections import ConnectionManager
from media_importer.model import Cancelled, now
from media_importer.notifications import NotificationWorker
from media_importer.service import Service
from media_importer.source_connection import observe, decide, require, ConfirmationRequired
from media_importer.status import snapshot


class AutomaticWorkflowTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown

    def bind_offline(self):
        state = observe(self.store, device())
        decide(self.store, self.settings, state['id'], True)
        self.settings.set_enabled(False)
        self.store.set_meta('source_connection', {**state, 'state': 'offline', 'online': False})

    def test_start_offline_reconnect_and_archive_tiny_file_without_new_consent(self):
        self.bind_offline()
        self.bot.enqueue('start-offline', {'changes': {'backup': {'enabled': True}}})
        with patch('media_importer.bot.time.sleep'):
            self.bot.process_one()
        self.assertTrue(self.settings.read()['config']['backup']['enabled'])
        manager = ConnectionManager(self.store, self.settings, threading.Event())
        def link(kind, action, serial, stop):
            return wireless() if action == 'connect' else None
        with patch('media_importer.connections.discover_source', return_value=None), \
             patch('media_importer.connections.wireless_supported', return_value=True), \
             patch('media_importer.connections.source_link', side_effect=link) as radio:
            manager.tick()
        self.assertEqual('approved', self.store.get_meta('source_connection')['state'])
        self.assertEqual(1, sum(c.args[1] == 'connect' for c in radio.call_args_list))
        config = Config.from_values(self.settings.read()['config'])
        source, target = FakeSource([footage()]), FakeDestination()
        self.assertTrue(Service(config, self.store, source, target, threading.Event()).cycle()['complete'])
        self.assertEqual(1, len(target.uploads))
        self.assertEqual([], source.deletes)

    def test_unknown_wireless_identity_waits_for_confirmation_before_source_io(self):
        self.settings.set_enabled(True)
        manager = ConnectionManager(self.store, self.settings, threading.Event())
        result = {**wireless(), 'http_ready': False}
        with patch('media_importer.connections.discover_source', return_value=None), \
             patch('media_importer.connections.wireless_supported', return_value=True), \
             patch('media_importer.connections.source_link', side_effect=lambda kind, action, *args: result if action == 'discover' else None):
            manager.tick()
        self.assertEqual('waiting_confirmation', self.store.get_meta('source_link')['phase'])
        with self.assertRaises(ConfirmationRequired):
            require(self.store, Config.from_values(self.settings.read()['config']))

    def test_stop_during_join_cancels_attempt_and_does_not_announce_connected(self):
        self.bind_offline(); self.settings.set_enabled(True)
        manager = ConnectionManager(self.store, self.settings, threading.Event())
        def link(kind, action, serial, stop):
            if action == 'probe': return None
            self.settings.set_enabled(False)
            self.assertTrue(stop.is_set())
            raise Cancelled('stopped')
        with patch('media_importer.connections.discover_source', return_value=None), \
             patch('media_importer.connections.wireless_supported', return_value=True), \
             patch('media_importer.connections.source_link', side_effect=link):
            manager.tick()
        self.assertEqual('waiting_source', self.store.get_meta('source_link')['phase'])
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])

    def test_no_device_result_is_retry_not_permanent_connecting(self):
        self.bind_offline(); self.settings.set_enabled(True)
        manager = ConnectionManager(self.store, self.settings, threading.Event())
        with patch('media_importer.connections.discover_source', return_value=None), \
             patch('media_importer.connections.wireless_supported', return_value=True), \
             patch('media_importer.connections.source_link', return_value=None) as radio:
            manager.tick(); manager.tick()
        link = self.store.get_meta('source_link')
        self.assertEqual('waiting_retry', link['phase'])
        self.assertTrue(link['failure'])
        self.assertEqual(1, sum(c.args[1] == 'connect' for c in radio.call_args_list))

    def test_ble_only_approval_is_not_a_media_ready_notice(self):
        self.bind_offline()
        observe(self.store, {**wireless(), 'http_ready': False})
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(), configuration={'applied_revision': self.settings.read()['revision']})
        state = snapshot(self.store.directory, self.settings)
        self.assertFalse(state['health']['source']['online'])
        self.assertFalse(state['unplug']['ready'])
        self.bot.announce_source()
        self.assertFalse(self.bot.process_one())

    def test_fresh_discovery_does_not_reuse_stale_health_metrics(self):
        self.bind_offline(); observe(self.store, wireless())
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(), source={'online': False, 'checked_at': '2020-01-01T00:00:00+00:00',
            'telemetry': {'available': True, 'battery_percent': 100, 'observed_at': '2020-01-01T00:00:00+00:00'}})
        source = snapshot(self.store.directory, self.settings)['health']['source']
        self.assertTrue(source['online'])
        self.assertFalse(source['telemetry']['available'])


class AutomaticPresentationTests(unittest.TestCase):
    def test_connection_is_visible_before_scan_and_updates_same_card_signature(self):
        state = view(); state['batch'] = {'total_files': 0, 'scanned': False}
        state['health']['source'] = {'online': False}
        state['source_link'] = {'phase': 'connecting'}
        before = copy.deepcopy(state)
        for rendered in (status_content(state), progress_card(state, 300)):
            text = json.dumps(rendered, ensure_ascii=False)
            self.assertIn('正在连接相机', text)
            self.assertIn('正在连接 Wi-Fi', text)
            self.assertNotIn('0 / 0', text)
        self.assertEqual(before, state)
        worker = object.__new__(NotificationWorker)
        options = {'enabled': True, 'progress_interval_seconds': 300}
        old = worker._card_signature(state, options)
        state['source_link'] = {'phase': 'waiting_retry', 'failure': 'retry', 'reason': '自动重试'}
        self.assertNotEqual(old, worker._card_signature(state, options))
        expected = worker._card_signature(state, options)
        state['source_link']['observed_at'] = now()
        self.assertEqual(expected, worker._card_signature(state, options))

    def test_camera_reconnecting_does_not_hide_active_smb_upload(self):
        state = view(); state['source_link'] = {'phase': 'connecting'}
        state['health']['source'] = {'online': False}
        state['stages']['upload'] = {'phase': 'uploading'}
        title, _, elements = status_content(state)
        self.assertEqual('远端归档进行中', title)
        self.assertIn('正在连接 Wi-Fi', json.dumps(elements, ensure_ascii=False))

    def test_connection_failure_stays_one_episode_across_retry_attempts(self):
        state = view(); state['queues'] = {}; state['stages'] = {}
        state['source_link'] = {'phase': 'waiting_retry', 'failure': 'unavailable'}
        failed = NotificationWorker._problems(state)
        state['source_link']['phase'] = 'connecting'
        self.assertEqual(failed, NotificationWorker._problems(state))
        self.assertIn('source:import:source_network', failed)
        state['source_link'] = {'phase': 'connected'}
        self.assertEqual({}, NotificationWorker._problems(state))
