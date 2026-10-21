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
        self.values['source']['transport'] = 'usb'
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
        self.assertIn('相机连接已建立', text)
        self.assertIn('同步已停止', text)
        self.assertIn('USB 已就绪', text)
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
        self.assertIn('相机连接不可用', text)
        self.assertIn('暂时无法访问相机', text)
        self.assertNotIn('USB 已拔出', text)
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
        self.assertIn('连接已建立', self.sender.call_args.args[1]['header']['title']['content'])
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
        self.assertIn('连接不可用', self.sender.call_args.args[1]['header']['title']['content'])

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
