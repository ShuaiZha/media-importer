"""Small isolated live fixtures: credentials, scheduling, recovery and audio."""
import asyncio
import json
from pathlib import Path
import plistlib
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_bot_settings import SettingsFormsTests
from test_wireless_connections import wireless
from media_importer import live_preview, livestream_worker
from media_importer.connections import ConnectionManager
from media_importer.credentials import prune, read, credential_path
from media_importer.model import ConfigError, SourceConnectionError
from media_importer.source_connection import observe, decide
from media_importer.preview_rtc import signaling
from media_importer.livestream_network import discover
from media_importer.adapters.gopro_livestream import LiveCamera, decode, field, one


class ConfigurationTests(unittest.TestCase):
    setUp = SettingsFormsTests.setUp
    tearDown = SettingsFormsTests.tearDown
    callback = SettingsFormsTests.callback
    submit = SettingsFormsTests.submit
    process = SettingsFormsTests.process

    def configure(self):
        result = self.submit('livestream_settings', {'ssid': 'Example network', 'password': 'fixture-WiFi-secret', 'port': '1935'})
        self.assertEqual('info', result['toast']['type']); self.process()

    def test_secret_bypasses_jobs_and_config_and_survives_pruning(self):
        before = self.settings.read()['config']
        token = self.settings.token()[1]
        self.configure()
        self.assertEqual(token, self.settings.token()[1])
        after = self.settings.read()['config']
        for group in ('backup', 'source', 'archive', 'limits'):
            self.assertEqual(before[group], after[group])
        value = after['livestream']
        secret = read(credential_path(self.path.parent, value['credential_id']))
        self.assertEqual('fixture-WiFi-secret', secret)
        with self.bot.db() as db:
            persisted = json.dumps([tuple(r) for r in db.execute('SELECT * FROM bot_jobs')])
        self.assertNotIn(secret, persisted + self.path.read_text() + json.dumps(self.bot.panel('livestream_settings')))
        with patch('media_importer.credentials.time.time', return_value=9999999999):
            prune(self.path.parent, [value['credential_id'], after['archive']['credential_id']])
        self.assertEqual(secret, read(credential_path(self.path.parent, value['credential_id'])))

    def test_new_network_requires_password_and_running_stream_blocks_save(self):
        self.configure()
        original = self.path.read_bytes()
        self.assertEqual('error', self.submit('livestream_settings', {'ssid': 'Changed', 'password': '', 'port': '1935'}, 'two')['toast']['type'])
        self.store.set_meta('live_preview', {'state': 'ready', 'mode': 'rtmp'})
        with self.assertRaises(ConfigError):
            self.settings.update_preferences({'livestream': {**self.settings.read()['config']['livestream'], 'port': 1936}})
        self.assertEqual(original, self.path.read_bytes())

    def test_mode_is_explicit_idempotent_and_does_not_start_sync(self):
        connection = observe(self.store, wireless()); decide(self.store, self.settings, connection['id'], True)
        self.configure()
        original = self.path.read_bytes()
        with patch.object(live_preview, 'available'), patch.object(live_preview, 'component'):
            state = live_preview.request(self.store, self.settings, self.bot.scope, 'live', mode='rtmp')
            again = live_preview.request(self.store, self.settings, self.bot.scope, 'live', mode='rtmp')
            self.assertEqual(state['id'], again['id'])
            with self.assertRaises(ConfigError): live_preview.request(self.store, self.settings, self.bot.scope, 'silent')
        self.assertEqual(original, self.path.read_bytes()); self.assertFalse(self.store.files())
        with patch('media_importer.connections.discover_source') as probe, patch('media_importer.connections.source_link') as radio:
            ConnectionManager(self.store, self.settings, threading.Event()).tick()
        radio.assert_not_called(); probe.assert_not_called()
        from media_importer.capture import enqueue
        with self.assertRaises(ConfigError): enqueue(self.store, self.bot.scope, 'capture', 'stop_recording', connection['id'])


class NetworkTests(unittest.TestCase):
    def test_shared_interface_is_discovered_without_fixed_names_or_subnets(self):
        text = 'uplink9: flags=UP\n\tinet 10.1.1.4 netmask 0xffffff00\nbridge42: flags=UP\n\tinet 172.29.3.1 netmask 0xffffff00\n\tmember: radio7 flags=3\n'
        with patch('media_importer.livestream_network.Path.exists', return_value=True), \
             patch('media_importer.livestream_network.Path.read_bytes', return_value=plistlib.dumps({'NAT': {'Enabled': 1, 'SharingDevices': ['radio7']}})), \
             patch('media_importer.livestream_network.subprocess.run', return_value=Mock(stdout=text)):
            self.assertEqual({'address': '172.29.3.1', 'interface': 'bridge42', 'hotspot': True}, discover())

    def test_ambiguous_or_vpn_network_never_guesses_a_receiver(self):
        with patch('media_importer.livestream_network.Path.exists', return_value=False), \
             patch('media_importer.livestream_network.subprocess.run', return_value=Mock(stdout='utun9: flags=UP\n\tinet 10.0.0.2 netmask 0xffffff00\n')), \
             patch('media_importer.livestream_network.route', return_value='utun9'):
            with self.assertRaises(ConfigError): discover()


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_recovery_never_claims_camera_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'recovery.json'; path.write_text('{broken')
            events = []
            with patch('media_importer.adapters.livestream_camera') as camera:
                await livestream_worker.session({'type': 'fixture', 'serial': 'fixture'}, threading.Event(),
                    lambda event, **values: events.append((event, values)), network={}, secret='', recovery_path=path, cleanup_only=True)
            camera.assert_not_called(); self.assertTrue(path.exists())
            self.assertFalse(events[-1][1]['camera_stopped'])

    async def test_rtmp_disables_sd_recording_and_does_not_repeat_lost_start(self):
        camera = LiveCamera('fixture')
        camera.proto = AsyncMock(return_value={1: [1]})
        camera.live_status = AsyncMock(return_value={1: [2]})
        camera.keep = AsyncMock(); camera.command = AsyncMock(side_effect=TimeoutError())
        with patch('media_importer.adapters.gopro_livestream.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(TimeoutError): await camera.start_live('rtmp://192.0.2.1/fixture/live', threading.Event())
        payload = decode(camera.proto.call_args.args[3])
        self.assertEqual(0, one(payload, 2)); camera.command.assert_awaited_once_with(1, b'\x01\x01')

    async def test_restore_verifies_idle_before_releasing_network(self):
        camera = LiveCamera('fixture'); events = []
        camera.live_status = AsyncMock(side_effect=[{1: [3]}, {1: [0]}])
        camera.state = AsyncMock(return_value={'recording': False, 'busy': False, 'preset': 1})
        async def command(code, *args):
            events.append(('command', code)); return bytes([47, 1, 4]) if code == 0x12 else b''
        camera.command = AsyncMock(side_effect=command)
        async def proto(*args): events.append(('proto', args[2])); return {1: [1]}
        camera.proto = AsyncMock(side_effect=proto)
        camera.statuses = AsyncMock(return_value={69: 1})
        async def close(): events.append(('closed', None))
        await camera.restore({'preset': 1, 'settings': {'47': 4}}, close)
        self.assertLess(events.index(('closed', None)), events.index(('proto', 0x78)))
        self.assertLess(events.index(('proto', 0x78)), events.index(('command', 0x17)))

    async def test_cancelled_or_failed_session_restores_camera_and_preserves_recovery_on_failure(self):
        for failed in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'recovery.json'
                camera = Mock(client=Mock(is_connected=True))
                camera.__aenter__ = AsyncMock(return_value=camera)
                camera.before = AsyncMock(return_value={'preset': 1, 'settings': {'47': 4}})
                async def join(*args):
                    self.assertTrue(path.exists())
                    self.assertNotIn('private-secret', path.read_text())
                    raise SourceConnectionError('Network unavailable')
                camera.join = AsyncMock(side_effect=join)
                camera.restore = AsyncMock(side_effect=SourceConnectionError('offline') if failed else None)
                camera.close = AsyncMock()
                relay = Mock()
                events = []
                with patch('media_importer.adapters.livestream_camera', return_value=camera), \
                     patch('media_importer.livestream_network.discover', return_value={'address': '192.0.2.1'}), \
                     patch.object(livestream_worker, 'Relay', return_value=relay):
                    await livestream_worker.session({'type': 'fixture', 'serial': 'fixture'}, threading.Event(),
                        lambda event, **values: events.append((event, values)), network={'ssid': 'Example', 'port': 1935},
                        secret='private-secret', recovery_path=path)
                self.assertEqual(failed, path.exists())
                self.assertEqual(not failed, events[-1][1]['camera_stopped'])
                camera.restore.assert_awaited(); relay.close.assert_called()
                self.assertNotIn('private-secret', json.dumps(events))

    async def test_restart_cleanup_never_starts_live_or_receiver(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'recovery.json'; path.write_text(json.dumps({'serial': 'fixture', 'before': {'preset': 1, 'settings': {}}}))
            camera = Mock(client=Mock(is_connected=True)); camera.__aenter__ = AsyncMock(return_value=camera)
            camera.restore = AsyncMock(); camera.close = AsyncMock()
            with patch('media_importer.adapters.livestream_camera', return_value=camera), patch.object(livestream_worker, 'Relay') as relay:
                await livestream_worker.session({'type': 'fixture', 'serial': 'fixture'}, threading.Event(), Mock(),
                    network={}, secret='', recovery_path=path, cleanup_only=True)
            relay.assert_not_called(); camera.start_live.assert_not_called(); self.assertFalse(path.exists())


class AudioTests(unittest.TestCase):
    def test_audio_is_allowed_only_when_receiving_a_live_session(self):
        offer = 'v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=recvonly\r\n'
        with self.assertRaises(ValueError): signaling('webrtc/offer', offer)
        signaling('webrtc/offer', offer, audio=True)
        with self.assertRaises(ValueError): signaling('webrtc/offer', offer.replace('recvonly', 'sendrecv'), audio=True)
