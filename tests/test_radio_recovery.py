"""Transient query recovery, bounded waits and non-replayed camera writes."""
import asyncio
import socket
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError, URLError

from media_importer.adapters import gopro_radio as radio
from media_importer.model import SourceConnectionError


class ReadRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_read_reopens_the_complete_camera_context_once(self):
        result = {'preset': 7, 'parameters': []}
        for action in ('catalog', 'state'):
            with self.subTest(action=action), \
                    patch.object(radio, 'perform', AsyncMock(side_effect=[
                        radio.TransientRadioError('相机蓝牙命令响应超时'), result])) as perform, \
                    patch.object(radio.asyncio, 'sleep', AsyncMock()) as pause:
                self.assertEqual(result, await radio.read_with_reconnect(action, 'C0000000000123'))
                self.assertEqual(2, perform.await_count)
                self.assertEqual([action, action], [c.args[0] for c in perform.await_args_list])
                pause.assert_awaited_once_with(.5)

    async def test_second_failure_retains_the_actual_stage_and_stops(self):
        with patch.object(radio, 'perform', AsyncMock(side_effect=[
                radio.TransientRadioError('相机蓝牙连接超时'),
                radio.TransientRadioError('相机蓝牙通知初始化超时')])) as perform, \
                patch.object(radio.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(SourceConnectionError, '通知初始化'):
                await radio.read_with_reconnect('catalog', 'C0000000000123')
            self.assertEqual(2, perform.await_count)

    async def test_attempt_timeout_is_retried_but_identity_rejection_is_not(self):
        with patch.object(radio, 'perform', AsyncMock(side_effect=[TimeoutError(), {'preset': 1}])) as perform, \
                patch.object(radio.asyncio, 'sleep', AsyncMock()):
            self.assertEqual({'preset': 1}, await radio.read_with_reconnect('catalog', 'C0000000000123'))
            self.assertEqual(2, perform.await_count)
        with patch.object(radio, 'perform', AsyncMock(side_effect=SourceConnectionError('蓝牙相机序列号不符'))) as perform, \
                patch.object(radio.asyncio, 'sleep', AsyncMock()) as pause:
            with self.assertRaisesRegex(SourceConnectionError, '序列号不符'):
                await radio.read_with_reconnect('catalog', 'C0000000000123')
            perform.assert_awaited_once()
            pause.assert_not_awaited()

    async def test_cancellation_is_not_retried(self):
        with patch.object(radio, 'perform', AsyncMock(side_effect=asyncio.CancelledError())) as perform, \
                patch.object(radio.asyncio, 'sleep', AsyncMock()) as pause:
            with self.assertRaises(asyncio.CancelledError):
                await radio.read_with_reconnect('catalog', 'C0000000000123')
            perform.assert_awaited_once()
            pause.assert_not_awaited()

    async def test_catalog_does_not_read_ble_state_twice(self):
        camera = radio.Camera('C0000000000123')
        camera.state = AsyncMock()
        with patch('media_importer.adapters.gopro_capture.catalog', AsyncMock(return_value={'parameters': []})) as catalog:
            self.assertEqual({'parameters': []}, await camera.execute('catalog'))
            catalog.assert_awaited_once_with(camera)
            camera.state.assert_not_awaited()


class NoReplayTests(unittest.TestCase):
    def test_shutter_preset_and_parameter_writes_never_use_read_retry(self):
        for action in ('start_recording', 'stop_recording', 'photo', 'load_preset', 'set_parameter', 'hilight'):
            with self.subTest(action=action), \
                    patch.object(radio, 'perform', AsyncMock(side_effect=radio.TransientRadioError('相机蓝牙命令发送超时'))) as perform, \
                    patch.object(radio, 'read_with_reconnect', AsyncMock()) as retry:
                with self.assertRaisesRegex(SourceConnectionError, '命令发送超时'):
                    radio.run(action, 'C0000000000123')
                perform.assert_awaited_once()
                retry.assert_not_called()


class PhaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_step_cancels_stuck_operation_and_names_the_stage(self):
        closed = asyncio.Event()
        async def stuck():
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        with self.assertRaisesRegex(radio.TransientRadioError, '连接超时'):
            await radio.ble_wait(stuck(), .001, '相机蓝牙连接超时')
        self.assertTrue(closed.is_set())

    async def test_notification_timeout_disconnects_before_retrying(self):
        char = types.SimpleNamespace(properties=['notify'], uuid=radio.characteristic(0x73))
        client = types.SimpleNamespace(connect=AsyncMock(), services=[types.SimpleNamespace(characteristics=[char])],
            start_notify=AsyncMock(side_effect=TimeoutError()), is_connected=True, disconnect=AsyncMock())
        module = types.ModuleType('bleak')
        module.BleakScanner = types.SimpleNamespace(find_device_by_filter=AsyncMock(return_value=object()))
        module.BleakClient = Mock(return_value=client)
        camera = radio.Camera('C0000000000123')
        camera.command = AsyncMock()
        with patch.dict(sys.modules, {'bleak': module}):
            with self.assertRaisesRegex(radio.TransientRadioError, '通知初始化超时'):
                await camera.__aenter__()
        client.disconnect.assert_awaited_once()
        camera.command.assert_not_awaited()

    async def test_permission_failure_is_precise_and_not_retried(self):
        class BleakError(Exception):
            pass
        class Unavailable(BleakError):
            reason = types.SimpleNamespace(name='DENIED_BY_USER')
        module = types.ModuleType('bleak.exc')
        module.BleakError, module.BleakBluetoothNotAvailableError = BleakError, Unavailable
        parent = types.ModuleType('bleak'); parent.exc = module
        with patch.dict(sys.modules, {'bleak': parent, 'bleak.exc': module}), \
                patch.object(radio, 'perform', AsyncMock(side_effect=Unavailable('private-device-secret'))) as perform, \
                patch.object(radio.asyncio, 'sleep', AsyncMock()) as pause:
            with self.assertRaisesRegex(SourceConnectionError, '蓝牙权限') as caught:
                await radio.read_with_reconnect('catalog', 'C0000000000123')
            self.assertNotIn('private-device-secret', str(caught.exception))
            perform.assert_awaited_once()
            pause.assert_not_awaited()


class HttpTests(unittest.TestCase):
    def test_http_timeout_is_retryable_and_hides_url_and_os_error(self):
        for cause in (socket.timeout('private-network-secret'), URLError('private-network-secret')):
            with self.subTest(error=type(cause).__name__), patch.object(radio, 'build_opener') as opener:
                opener.return_value.open.side_effect = cause
                with self.assertRaises(radio.TransientRadioError) as caught:
                    radio.http('/gopro/camera/state')
                self.assertIn('Wi-Fi', str(caught.exception))
                self.assertNotIn('private-network-secret', str(caught.exception))

    def test_http_rejection_is_not_a_retryable_disconnect(self):
        with patch.object(radio, 'build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError('http://private-example', 403, 'private-secret', {}, None)
            with self.assertRaises(SourceConnectionError) as caught:
                radio.http('/gopro/camera/state')
            self.assertNotIsInstance(caught.exception, radio.TransientRadioError)
            self.assertIn('403', str(caught.exception))
            self.assertNotIn('private-secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
