"""BLE packet parsing and non-replayed shutter behavior; no hardware required."""
import unittest
from unittest.mock import AsyncMock, patch

from media_importer.adapters.gopro_radio import Camera, Packets, decode_statuses, hardware, perform
from media_importer.model import SourceConnectionError


class ProtocolTests(unittest.TestCase):
    def test_fragmented_identity(self):
        fields = [b'9', b'HERO9 Black', b'board', b'firmware', b'C0000000000123']
        payload = b''.join(bytes([len(x)]) + x for x in fields)
        packets = Packets()
        frame = bytes([0x20, len(payload)]) + payload[:5]
        self.assertIsNone(packets.feed(frame))
        self.assertEqual('C0000000000123', hardware(packets.feed(b'\x80' + payload[5:]))['serial_number'])

    def test_malformed_frames_and_tlv_do_not_become_valid_states(self):
        for data in [b'', b'\x80oops', b'\x01too-long', b'\x60']:
            with self.assertRaises(ValueError): Packets().feed(data)
        for data in [b'\x01', b'\x01\x02\x00', b'\x01\x01\x00\x01\x01\x00']:
            with self.assertRaises(ValueError): decode_statuses(data)
        self.assertEqual({10: 1, 96: 1000}, decode_statuses(bytes([10, 1, 1, 96, 2, 3, 232])))


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_loss_is_resolved_by_state_without_replaying_record(self):
        camera = Camera('C0000000000123')
        camera.state = AsyncMock(side_effect=[{'recording': False, 'busy': False, 'mode': 'video'},
                                               {'recording': True, 'busy': True, 'mode': 'video'}])
        camera.command = AsyncMock(side_effect=TimeoutError())
        result = await camera.execute('start_recording')
        self.assertTrue(result['recording']); self.assertTrue(result['ack_lost'])
        camera.command.assert_awaited_once_with(1, bytes([1, 1]))

    async def test_already_recording_and_already_stopped_do_not_write_shutter(self):
        for action, recording in [('start_recording', True), ('stop_recording', False)]:
            camera = Camera('C0000000000123')
            camera.state = AsyncMock(return_value={'recording': recording, 'mode': 'video'})
            camera.command = AsyncMock()
            self.assertTrue((await camera.execute(action))['already_satisfied'])
            camera.command.assert_not_called()

    async def test_photo_completion_requires_new_photo_and_idle(self):
        camera = Camera('C0000000000123')
        initial = {'mode': 'photo', 'recording': False, 'busy': False, 'photos': 2}
        camera.state = AsyncMock(side_effect=[initial, {**initial, 'photos': 3, 'busy': True}, {**initial, 'photos': 3}])
        camera.set_mode = AsyncMock(return_value=initial)
        camera.command = AsyncMock()
        result = await camera.execute('photo')
        self.assertEqual(3, result['photos']); camera.command.assert_awaited_once()
        camera.set_mode.assert_awaited_once_with('photo')

    async def test_mode_switch_requires_enumerated_preset_group(self):
        camera = Camera('C0000000000123'); camera.command = AsyncMock()
        with patch('media_importer.adapters.gopro_radio.http', return_value={'presetGroupArray': []}):
            with self.assertRaises(SourceConnectionError): await camera.set_mode('photo')
        camera.command.assert_not_called()

    async def test_no_independent_uplink_never_opens_bluetooth_or_switches_wifi(self):
        with patch('media_importer.adapters.gopro_radio.wifi_interface', return_value='en1'), \
                patch('media_importer.adapters.gopro_radio.route', return_value='en1'), \
                patch('media_importer.adapters.gopro_radio.Camera') as camera:
            with self.assertRaises(SourceConnectionError): await perform('connect', 'C0000000000123')
            camera.assert_not_called()

    async def test_expired_shutter_is_never_sent_after_slow_connection(self):
        import time
        camera = Camera('C0000000000123')
        camera.expires_at = time.time() - 1
        camera.state = AsyncMock(return_value={'recording': False, 'busy': False, 'mode': 'video'})
        camera.command = AsyncMock()
        with self.assertRaises(SourceConnectionError):
            await camera.execute('start_recording')
        camera.command.assert_not_called()

    async def test_successful_system_exit_does_not_prove_wifi_ready(self):
        camera = Camera('C0000000000123')
        camera.identity = {'model_name': 'HERO9 Black', 'firmware_version': 'HD9.01.01.72.00',
                           'serial_number': 'C0000000000123'}
        camera.client = AsyncMock()
        camera.client.read_gatt_char.side_effect = [b'Example-camera', b'example-password']
        camera.command = AsyncMock()
        camera.statuses = AsyncMock(return_value={69: 1})
        context = AsyncMock(); context.__aenter__.return_value = camera
        with patch('media_importer.adapters.gopro_radio.Camera', return_value=context), \
                patch('media_importer.adapters.gopro_radio.wifi_interface', return_value='en1'), \
                patch('media_importer.adapters.gopro_radio.route', return_value='en0'), \
                patch('media_importer.adapters.gopro_radio.join_wifi', return_value=0), \
                patch('media_importer.adapters.gopro_radio.http_identity', side_effect=OSError('offline')) as check, \
                patch('media_importer.adapters.gopro_radio.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(SourceConnectionError, 'HTTP'):
                await perform('connect', 'C0000000000123')
            self.assertEqual(10, check.call_count)

    async def test_http_identity_rejects_other_camera_before_any_shutter(self):
        from media_importer.adapters.gopro_radio import http_identity
        with patch('media_importer.adapters.gopro_radio.http', return_value={'info': {'serial_number': 'different'}}):
            with self.assertRaises(SourceConnectionError):
                http_identity('C0000000000123')


class PresetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        identity = patch('media_importer.adapters.gopro_radio.http_identity', return_value={})
        identity.start(); self.addCleanup(identity.stop)

    async def test_real_enum_names_load_returned_single_photo_preset_not_burst(self):
        camera = Camera('C0000000000123')
        camera.command = AsyncMock()
        camera.statuses = AsyncMock(return_value={94: 70003})
        camera.state = AsyncMock(return_value={'mode': 'photo', 'preset': 70001, 'busy': False})
        response = {'presetGroupArray': [{'id': 'PRESET_GROUP_ID_PHOTO', 'presetArray': [
            {'id': 70003, 'mode': 'FLAT_MODE_PHOTO_BURST'},
            {'id': 70001, 'mode': 'FLAT_MODE_PHOTO', 'userDefined': False}]}]}
        with patch('media_importer.adapters.gopro_radio.http', return_value=response):
            await camera.set_mode('photo')
        camera.command.assert_awaited_once_with(0x40, b'\x04'+(70001).to_bytes(4, 'big'))

    async def test_numeric_group_keeps_last_normal_video_preset(self):
        camera = Camera('C0000000000123')
        camera.command = AsyncMock()
        camera.statuses = AsyncMock(return_value={93: 5})
        camera.state = AsyncMock(return_value={'mode': 'video', 'preset': 5, 'busy': False})
        response = {'presetGroupArray': [{'id': 1000, 'presetArray': [
            {'id': 1, 'mode': 'FLAT_MODE_VIDEO'}, {'id': 5, 'mode': 'FLAT_MODE_VIDEO'}]}]}
        with patch('media_importer.adapters.gopro_radio.http', return_value=response):
            await camera.set_mode('video')
        camera.command.assert_awaited_once_with(0x40, b'\x04\x00\x00\x00\x05')

    async def test_wrong_active_preset_is_not_success_and_mode_command_is_not_replayed(self):
        camera = Camera('C0000000000123')
        camera.command = AsyncMock()
        camera.statuses = AsyncMock(return_value={94: 9})
        camera.state = AsyncMock(return_value={'mode': 'photo', 'preset': 8, 'busy': False})
        response = {'presetGroupArray': [{'id': 1001, 'presetArray': [{'id': 9, 'mode': 'FLAT_MODE_PHOTO'}]}]}
        with patch('media_importer.adapters.gopro_radio.http', return_value=response), \
                patch('media_importer.adapters.gopro_radio.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(SourceConnectionError, '结果尚未确认'):
                await camera.set_mode('photo')
        camera.command.assert_awaited_once()

    async def test_unknown_or_invalid_photo_preset_never_sends_mode_or_shutter(self):
        for preset in [{'id': 1, 'mode': 'FLAT_MODE_PHOTO_BURST'},
                       {'id': -1, 'mode': 'FLAT_MODE_PHOTO'},
                       {'id': True, 'mode': 'FLAT_MODE_PHOTO'}]:
            camera = Camera('C0000000000123'); camera.command = AsyncMock()
            with patch('media_importer.adapters.gopro_radio.http', return_value={
                    'presetGroupArray': [{'id': 1001, 'presetArray': [preset]}]}):
                with self.assertRaises(SourceConnectionError): await camera.set_mode('photo')
            camera.command.assert_not_called()


class WifiJoinTests(unittest.TestCase):
    def test_networksetup_zero_exit_with_network_not_found_requests_retry(self):
        from types import SimpleNamespace
        from media_importer.adapters.gopro_radio import join_wifi
        with patch('media_importer.adapters.gopro_radio.subprocess.run', return_value=SimpleNamespace(
                stdout='Could not find network camera-example.', stderr='', returncode=0)):
            self.assertEqual('not_found', join_wifi('en1', 'camera-example', 'private-example'))

    def test_permission_diagnostic_does_not_expose_os_message_or_credentials(self):
        from types import SimpleNamespace
        from media_importer.adapters.gopro_radio import join_wifi
        with patch('media_importer.adapters.gopro_radio.subprocess.run', return_value=SimpleNamespace(
                stdout='Authorization denied private-example', stderr='', returncode=1)):
            with self.assertRaises(SourceConnectionError) as caught:
                join_wifi('en1', 'camera-example', 'private-example')
        self.assertNotIn('private-example', str(caught.exception))


class SeparateControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_ble_stop_does_not_depend_on_http_media_route(self):
        camera = Camera('C0000000000123')
        camera.identity = {'model_name': 'HERO9 Black', 'firmware_version': 'HD9.01.01.72.00',
                           'serial_number': 'C0000000000123'}
        camera.execute = AsyncMock(return_value={'recording': False, 'busy': False})
        context = AsyncMock(); context.__aenter__.return_value = camera
        with patch('media_importer.adapters.gopro_radio.Camera', return_value=context), \
                patch('media_importer.adapters.gopro_radio.http', side_effect=OSError('route down')) as http:
            self.assertFalse((await perform('stop_recording', 'C0000000000123'))['recording'])
        http.assert_not_called()
        camera.execute.assert_awaited_once_with('stop_recording')

    async def test_preset_from_another_http_camera_is_never_applied(self):
        camera = Camera('C0000000000123'); camera.command = AsyncMock()
        with patch('media_importer.adapters.gopro_radio.http', return_value={
                'info': {'serial_number': 'C0000000000999'}}):
            with self.assertRaisesRegex(SourceConnectionError, '身份不符'):
                await camera.set_mode('photo')
        camera.command.assert_not_called()
