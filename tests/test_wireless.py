import asyncio
import copy
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError, URLError

from media_importer.adapters.gopro import GoProSource
from media_importer.config import Config, ConfigError
from media_importer.model import Cancelled, SourceConnectionError, SourceError
from media_importer.issues import describe
from media_importer.wireless_control import publish, demand, connection_status, write_json
from media_importer.wireless_host import Helper, LinkError, Packets, hardware_serial
from test_service import configuration, footage
from test_gopro import Response


class SourceTransportTests(unittest.TestCase):
    def test_transport_switch_keeps_material_and_destination_identity(self):
        original = configuration()
        values = copy.deepcopy(original.values)
        values['source']['transport'] = 'wifi'
        wireless = Config.from_values(values)
        self.assertEqual(GoProSource(original).base, original.source['url'])
        self.assertEqual(GoProSource(wireless).base, 'http://10.5.5.9:8080')
        self.assertEqual(original.destination_id, wireless.destination_id)
        self.assertEqual(GoProSource(wireless).fork().transport, 'wifi')

    def test_invalid_transport_is_rejected(self):
        values = copy.deepcopy(configuration().values)
        values['source']['transport'] = 'automatic'
        with self.assertRaises(ConfigError):
            Config.from_values(values)

    def test_read_timeout_retries_and_respects_stop(self):
        stop = threading.Event()
        source = GoProSource(configuration(), stop)
        with patch.object(source.opener, 'open', side_effect=[URLError('timeout'), Response(b'{"ok":true}')]) as request, patch.object(stop, 'wait', return_value=False):
            self.assertTrue(source._json('/gopro/camera/state')['ok'])
            self.assertEqual(request.call_count, 2)
        def cancel(_):
            stop.set()
            return True
        with patch.object(source.opener, 'open', side_effect=URLError('timeout')) as request, patch.object(stop, 'wait', side_effect=cancel):
            with self.assertRaises(Cancelled):
                source._json('/gopro/camera/state')
            self.assertEqual(request.call_count, 1)

    def test_http_not_found_and_delete_are_not_retried(self):
        source = GoProSource(configuration())
        error = HTTPError('http://camera', 404, 'missing', {}, io.BytesIO())
        with patch.object(source.opener, 'open', side_effect=error) as request:
            with self.assertRaises(SourceError):
                source._json('/gopro/media/info')
            self.assertEqual(request.call_count, 1)
        with patch.object(source, 'verify_primary'), patch.object(source.opener, 'open', side_effect=URLError('timeout')) as request:
            with self.assertRaises(SourceConnectionError):
                source.delete(footage())
            self.assertEqual(request.call_count, 1)

    def test_network_failure_does_not_skip_file(self):
        issue = describe(SourceConnectionError('timeout'), 'import')
        self.assertEqual(issue['scope'], 'source')

    def test_cache_write_failure_is_not_misreported_as_camera_network(self):
        source, asset = GoProSource(configuration()), footage()
        with patch.object(source, 'identify'), patch.object(source, 'ensure_idle'), \
             patch.object(source, 'file_stat', return_value=(asset.size, asset.modified)), \
             patch.object(source, 'verify_primary'), patch.object(source, '_open', return_value=Response()), \
             patch('media_importer.adapters.gopro.open', side_effect=OSError(28, 'No space')):
            with self.assertRaises(OSError) as error:
                source.download(asset, 'test.download', Mock())
        self.assertEqual(describe(error.exception, 'import')['scope'], 'cache')


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        values = copy.deepcopy(configuration().values)
        values['source']['transport'] = 'wifi'
        self.config = Config.from_values(values)

    def read(self):
        return demand(self.root, self.config.source['serial'], self.config.source['wireless_url'])

    def test_switch_off_and_stale_lease_never_connect(self):
        publish(self.root, self.config, False, {})
        self.assertFalse(self.read()['enabled'])
        publish(self.root, self.config, True, {'source_work': True})
        self.assertTrue(self.read()['needs_camera'])
        with patch('media_importer.wireless_control.time.time', return_value=time.time() + 9):
            self.assertEqual(self.read(), {})
        self.assertEqual(demand(self.root, 'different-camera', self.config.source['wireless_url']), {})

    def test_completed_import_releases_wake_demand(self):
        publish(self.root, self.config, True, {'inventory': {'verified': True}, 'source_work': False})
        self.assertFalse(self.read()['needs_camera'])
        self.assertTrue(self.read()['enabled'])

    def test_stale_helper_does_not_report_healthy(self):
        write_json(self.root / 'wireless/status.json', {'serial': self.config.source['serial'], 'updated_at': time.time() - 16, 'state': 'ready'})
        self.assertEqual(connection_status(self.root, self.config.source['serial'])['state'], 'unavailable')

    def test_missing_data_directory_is_not_recreated(self):
        self.root.rmdir()
        with self.assertRaises(FileNotFoundError):
            publish(self.root, self.config, True, {})
        self.assertFalse(self.root.exists())


class ProtocolTests(unittest.TestCase):
    def test_fragmented_extended_hardware_info(self):
        fields = [b'\0\0\0\x32', b'HERO9 Black', b'\0\0\0\1', b'HD9.01.01.72.00', b'C0000000000123', b'test-ssid', b'aa:bb']
        payload = b''.join(bytes([len(f)]) + f for f in fields)
        response = b'\x3c\0' + payload
        packets = Packets()
        self.assertIsNone(packets.feed(bytes([0x20, len(response)]) + response[:18]))
        self.assertIsNone(packets.feed(b'\x80' + response[18:37]))
        complete = packets.feed(b'\x81' + response[37:])
        self.assertEqual(hardware_serial(complete[2:]), 'C0000000000123')

    def test_bad_sequence_and_missing_serial_rejected(self):
        packets = Packets()
        with self.assertRaises(LinkError):
            packets.feed(b'\x80abc')
        with self.assertRaises(LinkError):
            hardware_serial(b'\x05abc')


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.helper = Helper({'SOURCE_SERIAL': 'C0000000000123'}, self.root)

    async def test_existing_manual_connection_does_not_need_bluetooth(self):
        with patch('media_importer.wireless_host.camera_identity', return_value=True), \
             patch.object(self.helper, 'bluetooth_bounded', new_callable=AsyncMock) as bluetooth:
            await self.helper.connect()
            bluetooth.assert_not_awaited()
            self.assertEqual(self.helper.state, 'ready')

    async def test_hotspot_ready_waits_for_manual_wifi_without_repeated_ble(self):
        with patch.object(self.helper, 'bluetooth_bounded', return_value='test-camera') as bluetooth, \
             patch('media_importer.wireless_host.camera_identity', side_effect=[False, False, True]):
            await self.helper.connect()
            self.assertEqual(self.helper.state, 'waiting_wifi')
            await self.helper.connect()
            bluetooth.assert_awaited_once()
            await self.helper.connect()
            self.assertEqual(self.helper.state, 'ready')

    async def test_wrong_serial_is_not_treated_as_manual_wifi_ready(self):
        with patch.object(self.helper, 'bluetooth_bounded', side_effect=LinkError('蓝牙相机序列号不符')), \
             patch('media_importer.wireless_host.camera_identity', return_value=False):
            with self.assertRaises(LinkError):
                await self.helper.connect()
            self.assertEqual(self.helper.ap_ready_until, 0)

    async def test_sync_off_cancels_pending_connect(self):
        state = {'enabled': True, 'needs_camera': True}
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def connect():
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
        with patch.object(self.helper, 'control', side_effect=lambda: state), patch.object(self.helper, 'connect', side_effect=connect):
            task = asyncio.create_task(self.helper.run())
            try:
                await asyncio.wait_for(started.wait(), 2)
                state['enabled'] = False
                await asyncio.wait_for(cancelled.wait(), 2)
                await asyncio.sleep(.6)
                self.assertEqual(self.helper.state, 'idle')
            finally:
                self.helper.stop.set()
                await asyncio.wait_for(task, 3)

    async def test_failed_attempt_retries_after_backoff(self):
        self.helper.retry_seconds = .01
        calls = 0
        async def connect():
            nonlocal calls
            calls += 1
            self.helper.attempt += 1
            if calls == 1:
                raise LinkError('测试超时')
            self.helper.stop.set()
        with patch.object(self.helper, 'control', return_value={'enabled': True, 'needs_camera': True}), \
             patch.object(self.helper, 'connect', side_effect=connect):
            await asyncio.wait_for(self.helper.run(), 4)
        self.assertEqual(calls, 2)

    async def test_stopped_daemon_lease_cancels_work(self):
        self.assertEqual(self.helper.control(), {})
        with patch.object(self.helper, 'connect', new_callable=AsyncMock) as connect:
            task = asyncio.create_task(self.helper.run())
            await asyncio.sleep(.6)
            self.helper.stop.set()
            await task
            connect.assert_not_awaited()

    async def test_removed_data_directory_is_not_recreated(self):
        self.root.rmdir()
        with self.assertRaises(OSError):
            self.helper.report('idle', 'test')
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
