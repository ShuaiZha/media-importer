"""Remote shooting, stateful forms and small-file handoff; no real devices."""
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from media_importer import capture, capture_cards, capture_forms, capture_sessions
from media_importer.adapters.gopro import GoProSource
from media_importer.adapters.gopro_radio import Camera
from media_importer.adapters import gopro_capture
from media_importer.model import ConfigError, SourceConnectionError, now
import test_capture
from test_service import footage


def catalog(preset=7):
    return {'preset': preset, 'recording': False, 'busy': False, 'mode': 'video',
            'preset_label': '标准', 'observed_at': now(),
            'presets': [{'id': preset, 'label': '标准', 'mode': 'video', 'specification': '1080p · 30 fps'}],
            'parameters': [{'key': 'fps', 'label': '帧率', 'value': 5, 'value_label': '60 fps',
                            'options': [{'value': 5, 'label': '60 fps'}, {'value': 8, 'label': '30 fps'}]}]}


class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_capability_tlv_repeats_setting_but_rejects_malformed_or_duplicate_options(self):
        self.assertEqual([5, 8], gopro_capture.decode_options(bytes([3, 1, 5, 3, 1, 8]), 3))
        self.assertEqual([], gopro_capture.decode_options(bytes([3, 0]), 3))
        for value in [bytes([3]), bytes([2, 1, 5]), bytes([3, 2, 5]), bytes([3, 1, 5, 3, 1, 5])]:
            with self.assertRaises(SourceConnectionError): gopro_capture.decode_options(value, 3)

    def test_presets_discovered_with_real_ids_and_safe_modes(self):
        values = gopro_capture.presets({'presetGroupArray': [{'presetArray': [
            {'id': 704, 'mode': 'FLAT_MODE_PHOTO', 'titleId': 'PRESET_TITLE_PHOTO'},
            {'id': 900, 'mode': 13, 'titleId': 'PRESET_TITLE_TIME_LAPSE'},
            {'id': 5, 'mode': 'FLAT_MODE_LIVE_BURST'},
            {'id': True, 'mode': 'FLAT_MODE_VIDEO'}]}]})
        self.assertEqual([704, 900], [p['id'] for p in values])
        self.assertEqual(['photo', 'timelapse'], [p['mode'] for p in values])

    async def test_hidden_duration_and_stabilization_not_lost_with_caption_only_preset(self):
        camera = Camera('C0000000000123')
        camera.state = AsyncMock(return_value=catalog())
        raw = {'presetGroupArray': [{'presetArray': [
            {'id': 7, 'mode': 'FLAT_MODE_VIDEO', 'titleId': 'PRESET_TITLE_STANDARD',
             'settingArray': [{'id': 2, 'value': 9}]}]}]}
        with patch('media_importer.adapters.gopro_radio.http_identity'), \
                patch('media_importer.adapters.gopro_radio.http', side_effect=[raw, {'settings': {'2': 9, '135': 1, '156': 100}}]), \
                patch.object(gopro_capture, 'choices', AsyncMock(side_effect=[[9], [0, 1], [1, 100]])):
            result = await gopro_capture.catalog(camera)
        self.assertEqual(['resolution', 'stabilization', 'duration'], [p['key'] for p in result['parameters']])
        self.assertEqual([1, 100], [o['value'] for o in result['parameters'][-1]['options']])

    async def test_parameter_ack_loss_reads_back_once_without_replaying_write(self):
        camera = Camera('C0000000000123')
        camera.state = AsyncMock(return_value=catalog())
        async def command(code, payload=b'', channel=0x72):
            if channel == 0x74: raise TimeoutError()
            return bytes([3, 1, 8])
        camera.command = AsyncMock(side_effect=command)
        with patch.object(gopro_capture, 'catalog', AsyncMock(return_value=catalog())), \
                patch.object(gopro_capture, 'choices', AsyncMock(return_value=[5, 8])):
            result = await gopro_capture.set_parameter(camera, {'key': 'fps', 'value': 8, 'expected_preset': 7})
        self.assertTrue(result['ack_lost']); self.assertEqual('30 fps', result['value_label'])
        self.assertEqual(1, sum(c.args[-1] == 0x74 for c in camera.command.call_args_list))

    async def test_changed_preset_or_options_never_write(self):
        for expected, available in [(8, [5, 8]), (7, [5])]:
            camera = Camera('C0000000000123'); camera.command = AsyncMock()
            with patch.object(gopro_capture, 'catalog', AsyncMock(return_value=catalog())), \
                    patch.object(gopro_capture, 'choices', AsyncMock(return_value=available)):
                with self.assertRaises(SourceConnectionError):
                    await gopro_capture.set_parameter(camera, {'key': 'fps', 'value': 8, 'expected_preset': expected})
            camera.command.assert_not_called()

    async def test_parameter_readback_mismatch_is_uncertain(self):
        camera = Camera('C0000000000123'); camera.state = AsyncMock(return_value=catalog())
        camera.command = AsyncMock(return_value=bytes([3, 1, 5]))
        with patch.object(gopro_capture, 'catalog', AsyncMock(return_value=catalog())), \
                patch.object(gopro_capture, 'choices', AsyncMock(return_value=[5, 8])):
            result = await gopro_capture.set_parameter(camera, {'key': 'fps', 'value': 8, 'expected_preset': 7})
        self.assertTrue(result['uncertain'])

    async def test_state_change_between_options_and_write_is_rejected(self):
        camera = Camera('C0000000000123'); camera.command = AsyncMock()
        camera.state = AsyncMock(return_value={**catalog(), 'recording': True})
        with patch.object(gopro_capture, 'catalog', AsyncMock(return_value=catalog())), \
                patch.object(gopro_capture, 'choices', AsyncMock(return_value=[5, 8])):
            with self.assertRaises(SourceConnectionError):
                await gopro_capture.set_parameter(camera, {'key': 'fps', 'value': 8, 'expected_preset': 7})
        camera.command.assert_not_called()

    async def test_unknown_parameter_never_reads_or_writes_camera(self):
        camera = Camera('C0000000000123'); camera.command = AsyncMock()
        with self.assertRaises(SourceConnectionError):
            await gopro_capture.set_parameter(camera, {'key': 'format', 'value': 1, 'expected_preset': 7})
        camera.command.assert_not_called()

    async def test_hilight_requires_encoding_and_observed_count(self):
        camera = Camera('C0000000000123'); camera.state = AsyncMock(return_value={'recording': True, 'mode': 'video'})
        camera.statuses = AsyncMock(side_effect=[{58: 2}, {58: 3}]); camera.command = AsyncMock(side_effect=TimeoutError())
        self.assertTrue((await camera.execute('hilight'))['hilight_added'])
        camera.command.assert_awaited_once_with(0x18)
        camera.command.reset_mock(); camera.state.return_value = {'recording': False, 'mode': 'video'}
        with self.assertRaises(SourceConnectionError): await camera.execute('hilight')
        camera.command.assert_not_called()

    async def test_timelapse_start_does_not_switch_to_video(self):
        camera = Camera('C0000000000123'); camera.command = AsyncMock(); camera.set_mode = AsyncMock()
        camera.state = AsyncMock(side_effect=[{'mode': 'timelapse', 'recording': False, 'busy': False},
                                              {'mode': 'timelapse', 'recording': True, 'busy': True}])
        self.assertTrue((await camera.execute('start_timelapse'))['recording'])
        camera.set_mode.assert_not_called(); camera.command.assert_awaited_once_with(1, b'\x01\x01')

    async def test_timelapse_requires_explicitly_selected_preset(self):
        camera = Camera('C0000000000123'); camera.command = AsyncMock()
        camera.state = AsyncMock(return_value={'mode': 'video', 'recording': False, 'busy': False})
        with self.assertRaises(SourceConnectionError): await camera.execute('start_timelapse')
        camera.command.assert_not_called()

    async def test_start_different_mode_does_not_claim_success(self):
        camera = Camera('C0000000000123'); camera.command = AsyncMock()
        camera.state = AsyncMock(return_value={'mode': 'timelapse', 'recording': True, 'busy': True})
        with self.assertRaises(SourceConnectionError): await camera.execute('start_recording')
        camera.command.assert_not_called()

    def test_telemetry_reports_actual_duration_and_unknown_fields(self):
        state = GoProSource.decode_status({'8': 0, '10': 1, '13': 15, '96': 1002, '97': 7})
        self.assertEqual((15, 'timelapse', None), (state['recording_seconds'], state['mode'], state['battery_percent']))


class RequestAndCardTests(unittest.TestCase):
    setUp, tearDown = test_capture.CaptureTests.setUp, test_capture.CaptureTests.tearDown
    connected, service = test_capture.CaptureTests.connected, test_capture.CaptureTests.service
    callback = test_capture.CaptureTests.callback

    def test_additive_upgrade_preserves_old_requests_and_data(self):
        connection = self.connected()
        with patch('media_importer.connections.wireless_supported', return_value=True):
            op = capture.enqueue(self.store, self.bot.scope, 'old', 'state', connection['id'])
        with self.store.db: self.store.db.execute('ALTER TABLE capture_requests DROP COLUMN parameters')
        capture.initialize(self.store.db)
        self.assertEqual('{}', capture.get(self.store.db, op['id'], self.bot.scope)['parameters'])

    def test_settings_requests_preserve_configuration_and_do_not_repeat_mutations(self):
        connection = self.connected(); original = self.path.read_bytes()
        parameters = {'key': 'fps', 'value': 8, 'expected_preset': 7}
        with patch('media_importer.connections.wireless_supported', return_value=True), \
                patch('media_importer.adapters.source_link', return_value=catalog()) as radio:
            op = capture.enqueue(self.store, self.bot.scope, 'parameter', 'set_parameter', connection['id'], parameters)
            capture.process_one(self.service(), self.settings)
            self.assertEqual(op['id'], capture.enqueue(self.store, self.bot.scope, 'parameter', 'set_parameter', connection['id'], parameters)['id'])
            self.assertFalse(capture.process_one(self.service(), self.settings))
            radio.assert_called_once()
            self.assertEqual(parameters, radio.call_args.kwargs['parameters'])
        self.assertEqual(original, self.path.read_bytes()); self.assertFalse(self.store.files())

    def test_parameter_request_restart_only_reads_state(self):
        connection = self.connected()
        with patch('media_importer.connections.wireless_supported', return_value=True):
            capture.enqueue(self.store, self.bot.scope, 'x', 'load_preset', connection['id'], {'preset': 7})
            with self.store.db: self.store.db.execute("UPDATE capture_requests SET state='running'")
            with patch('media_importer.adapters.source_link', return_value=catalog()) as radio:
                capture.process_one(self.service(), self.settings)
        self.assertEqual('state', radio.call_args.args[1])

    def test_parameter_validation_rejects_bool_unknown_fields_and_large_values(self):
        for values in [{'preset': True}, {'preset': -1}, {'preset': 2**32}, {'preset': 1, 'path': '/delete'}]:
            with self.assertRaises(ConfigError): capture.validate_parameters('load_preset', values)
        for value in (True, -1, 256, '8'):
            with self.assertRaises(ConfigError): capture.validate_parameters('set_parameter', {'key': 'fps', 'expected_preset': 7, 'value': value})

    def test_preview_must_stop_before_photo_and_parameter_writes(self):
        connection = self.connected(); serial = connection['device']['serial_number']
        self.store.set_meta('live_preview', {'id': 'c' * 32, 'serial': serial, 'state': 'ready', 'url': 'private'})
        with patch('media_importer.connections.wireless_supported', return_value=True):
            with self.assertRaises(ConfigError): capture.enqueue(self.store, self.bot.scope, 'param', 'load_preset', connection['id'], {'preset': 7})
            capture.enqueue(self.store, self.bot.scope, 'photo', 'preview_photo', connection['id'])
            self.assertTrue(self.store.get_meta('live_preview')['stop_requested'])
            with patch('media_importer.adapters.source_link') as radio:
                self.assertFalse(capture.process_one(self.service(), self.settings))
                radio.assert_not_called()

    def test_explicit_shoot_and_inventory_work_with_sync_disabled(self):
        from media_importer.runtime import Control
        from media_importer.source_session import SourceSession
        from media_importer.model import Cancelled
        connection = self.connected(); service = self.service()
        control = Control(self.settings, service.stop)
        service.source = SourceSession(service.config, self.store.directory, control.transfer_stop)
        self.assertTrue(control.transfer_stop.is_set())
        assets = [footage(1)]
        def inventory(client):
            if client.stop.is_set(): raise Cancelled('stopped')
            return list(assets)
        with patch('media_importer.connections.wireless_supported', return_value=True), \
                patch.object(GoProSource, 'snapshot', inventory), \
                patch('media_importer.adapters.source_link', return_value={**catalog(), 'mode': 'photo'}) as radio:
            op = capture.enqueue(self.store, self.bot.scope, 'off-photo', 'photo', connection['id'])
            capture.process_one(service, self.settings)
            self.assertNotIn('error', json.loads(capture.get(self.store.db, op['id'], self.bot.scope)['result']))
            radio.assert_called_once()
            assets.append(footage(2))
            self.store.set_meta('capture_session', {**self.store.get_meta('capture_session'), 'retry_at': 0})
            capture_sessions.settle(service)
        session = self.store.get_meta('capture_session')
        self.assertEqual('ready', session['state'])
        self.assertEqual([footage(2).path], [a['path'] for a in session['assets']])
        self.assertTrue(control.transfer_stop.is_set())
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
        self.assertFalse(self.store.files())

    def test_shoot_inventory_still_obeys_daemon_shutdown(self):
        from media_importer.source_session import SourceSession
        from media_importer.model import Cancelled
        connection = self.connected(); service = self.service()
        service.source = SourceSession(service.config, self.store.directory, service.stop)
        service.stop.set()
        def inventory(client):
            if client.stop.is_set(): raise Cancelled('service stopped')
            return []
        with patch('media_importer.connections.wireless_supported', return_value=True), \
                patch.object(GoProSource, 'snapshot', inventory), \
                patch('media_importer.adapters.source_link') as radio:
            op = capture.enqueue(self.store, self.bot.scope, 'shutdown-photo', 'photo', connection['id'])
            capture.process_one(service, self.settings)
        radio.assert_not_called()
        self.assertFalse(json.loads(capture.get(self.store.db, op['id'], self.bot.scope)['result'])['uncertain'])

    def test_setting_forms_parse_only_owner_inputs_and_real_values(self):
        connection = self.connected()
        value = {**catalog(), 'session_id': connection['id']}
        self.store.set_meta('camera_catalog', value)
        response = capture_forms.settings_panel(self.bot, self.store)
        self.assertEqual('拍摄设置', response['header']['title']['content'])
        action = {'version': 1, 'action': 'apply_parameter', 'key': 'fps', 'expected_preset': 7,
                  'session_id': connection['id'], 'nonce': 'a' * 32}
        token = self.bot.action({'capture': action})
        self.assertEqual('error', self.bot.receive_card(self.callback(token, owner='other', fields={'choice': '8'}))['toast']['type'])
        self.assertEqual('error', self.bot.receive_card(self.callback(token, fields={}))['toast']['type'])
        self.assertEqual({'key': 'fps', 'expected_preset': 7, 'value': 8}, capture_forms.parse(action, {'choice': '8'}))
        self.store.set_meta('camera_catalog', {**value, 'observed_at': '2000-01-01T00:00:00+00:00'})
        with self.assertRaises(ConfigError): capture_forms.parameter_panel(self.bot, self.store, action)

    def test_catalog_is_fetched_at_menu_and_returns_form_in_same_message(self):
        self.connected()
        self.sender.return_value = 'om_camera_settings'
        self.bot.receive_card(self.callback(self.bot.action({'capture': {'version': 1, 'action': 'settings'}})))
        with patch('media_importer.connections.wireless_supported', return_value=True):
            self.assertTrue(self.bot.process_one())
            with patch('media_importer.adapters.source_link', return_value=catalog()): capture.process_one(self.service(), self.settings)
            with self.bot.db() as db: db.execute('UPDATE bot_jobs SET retry_at=0')
            self.assertTrue(self.bot.process_one())
        self.sender.assert_called_once()
        self.assertEqual('拍摄设置', self.updater.call_args.args[-1]['header']['title']['content'])

    def test_new_telemetry_overrides_start_receipt_and_no_fake_live_status(self):
        connection = self.connected()
        self.store.status(source={'serial_number': connection['device']['serial_number'], 'telemetry': {
            'available': True, 'recording': False, 'busy': False, 'mode': 'video', 'observed_at': now()}})
        response = capture_cards.panel(self.bot, self.store, {'recording': True, 'command': 'start_recording', 'observed_at': now()})
        text = json.dumps(response, ensure_ascii=False)
        self.assertIn('拍摄已结束', text); self.assertNotIn('录像已开始', text)

    def test_mode_switch_does_not_display_previous_video_specification(self):
        from datetime import datetime, timezone
        connection = self.connected(); serial = connection['device']['serial_number']
        self.store.status(source={'serial_number': serial, 'telemetry': {'available': True,
            'recording': False, 'mode': 'video', 'preset': 1, 'specification': '4K · 60 fps',
            'observed_at': datetime.fromtimestamp(time.time()-2, timezone.utc).isoformat()}})
        self.store.set_meta('camera_control', {'serial': serial, 'recording': False,
                                             'mode': 'photo', 'preset': 2, 'observed_at': now()})
        self.assertNotIn('specification', capture.observation(self.store, connection['device']))

    def test_failed_read_is_not_an_uncertain_write(self):
        connection = self.connected()
        with patch('media_importer.connections.wireless_supported', return_value=True):
            op = capture.enqueue(self.store, self.bot.scope, 'timeout-read', 'catalog', connection['id'])
            with patch('media_importer.adapters.source_link', side_effect=SourceConnectionError('timeout')):
                capture.process_one(self.service(), self.settings)
        result = json.loads(capture.get(self.store.db, op['id'], self.bot.scope)['result'])
        self.assertFalse(result['uncertain'])

    def test_preview_monitor_does_not_overwrite_newer_capture_view(self):
        self.bot.enqueue('old-preview', {'live_preview': {'version': 1, 'action': 'panel'},
                         'response_message_id': 'om_shared'})
        self.bot.enqueue('new-capture', {'capture': {'version': 1, 'action': 'panel'},
                         'response_message_id': 'om_shared'})
        with self.bot.db() as db:
            old = db.execute("SELECT * FROM bot_jobs WHERE id='old-preview'").fetchone()
            new = db.execute("SELECT * FROM bot_jobs WHERE id='new-capture'").fetchone()
        self.bot.reply(old, json.loads(old['payload']), {'old': True}, waiting=True)
        self.updater.assert_not_called()
        self.bot.reply(new, json.loads(new['payload']), {'current': True})
        self.updater.assert_called_once_with(self.bot.credentials, 'om_shared', {'current': True})
        with self.bot.db() as db:
            self.assertEqual('waiting', db.execute("SELECT state FROM bot_jobs WHERE id='old-preview'").fetchone()[0])

    def test_shooting_scope_transfers_only_new_small_file_preserves_camera_and_settings(self):
        connection = self.connected()
        self.settings.update_preferences({'backup': {'archive_previews': False}})
        service = self.service()
        before = footage(1); after = footage(2); preview = footage(2, primary=False)
        service.source.assets = {before.path: before}
        op = {'id': 'a' * 32, 'owner': self.bot.scope, 'serial': before.serial, 'device_type': before.source_type}
        capture_sessions.prepare(service, op, 'start_recording')
        service.source.assets.update({after.path: after, preview.path: preview})
        capture_sessions.record_result(self.store, op, 'stop_recording', {'recording': False, 'busy': False})
        self.store.set_meta('capture_session', {**self.store.get_meta('capture_session'), 'retry_at': 0})
        self.store.set_meta('camera_control', {**catalog(), 'serial': before.serial})
        capture_sessions.settle(service)
        self.assertEqual('ready', self.store.get_meta('capture_session')['state'])
        original = self.path.read_bytes()
        selected = capture_sessions.selection(self.store, self.settings, self.bot.scope, op['id'])
        result = json.loads(selected['result']); self.assertEqual(1, result['total_files'])
        from media_importer.selected_imports import confirm, process_one, get
        confirm(self.store, self.settings, selected['id'], self.bot.scope, selected['choice_revision'])
        self.assertTrue(process_one(service, self.settings))
        self.assertEqual('done', get(self.store.db, selected['id'])['state'])
        self.assertEqual([after.path], service.source.downloads)
        self.assertEqual(1, len(service.destination.uploads)); self.assertFalse(service.source.deletes)
        self.assertEqual(3, len(service.source.assets)); self.assertEqual(original, self.path.read_bytes())

    def test_shooting_scope_unknown_owner_and_changed_device_rejected(self):
        self.connected()
        self.store.set_meta('capture_session', {'id': 'a'*32, 'owner': 'someone_else', 'state': 'ready'})
        with self.assertRaises(ConfigError): capture_sessions.selection(self.store, self.settings, self.bot.scope, 'a'*32)

    def test_no_new_file_does_not_claim_capture_or_archive_completion(self):
        connection = self.connected(); service = self.service()
        op = {'id': 'a'*32, 'owner': self.bot.scope, 'serial': connection['device']['serial_number'], 'device_type': 'gopro'}
        capture_sessions.prepare(service, op, 'photo')
        self.store.set_meta('camera_control', {**catalog(), 'serial': op['serial']})
        capture_sessions.settle(service)
        self.assertEqual('awaiting_media', self.store.get_meta('capture_session')['state'])
        with self.assertRaises(ConfigError): capture_sessions.selection(self.store, self.settings, self.bot.scope, op['id'])

    def test_idle_empty_capture_stops_repeated_scans_without_claiming_success(self):
        connection = self.connected(); service = self.service()
        op = {'id': 'a'*32, 'owner': self.bot.scope, 'serial': connection['device']['serial_number'], 'device_type': 'gopro'}
        capture_sessions.prepare(service, op, 'photo')
        self.store.set_meta('camera_control', {**catalog(), 'serial': op['serial']})
        self.store.set_meta('capture_session', {**self.store.get_meta('capture_session'), 'idle_since': time.time() - 121})
        capture_sessions.settle(service)
        self.assertEqual('empty', self.store.get_meta('capture_session')['state'])
        with patch.object(service, 'source_snapshot') as scan:
            capture_sessions.settle(service)
            scan.assert_not_called()

    def test_failed_refresh_does_not_erase_ready_capture(self):
        session = {'id': 'a'*32, 'owner': self.bot.scope, 'serial': 'test', 'device_type': 'gopro', 'state': 'ready'}
        self.store.set_meta('capture_session', session)
        capture_sessions.record_result(self.store, session, 'state', {'error': 'offline', 'uncertain': True})
        self.assertEqual(session, self.store.get_meta('capture_session'))


if __name__ == '__main__': unittest.main()
