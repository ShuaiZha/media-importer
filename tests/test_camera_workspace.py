"""Tiny media, real gateway and command queues; no camera or user config."""
import asyncio
import io
import json
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from media_importer import camera_web, camera_library, capture
from media_importer.adapters.gopro import GoProSource
from media_importer.adapters import gopro_capture
from media_importer.config import Config
from media_importer.model import ConfigError, SourceError
from media_importer.preview_access import Gateway
from media_importer.runtime import Control
from media_importer.service import Service
from media_importer.source_connection import observe, decide
import test_bot
from test_service import FakeSource, FakeDestination, footage, configuration
from test_wireless_connections import wireless


class Fixture:
    def setUp(self):
        test_bot.BotTests.setUp(self)
        self.connection = observe(self.store, wireless())
        decide(self.store, self.settings, self.connection['id'], True)
        self.settings.set_enabled(False)
        self.control = Control(self.settings, threading.Event())
        self.source = FakeSource([footage(), footage(primary=False), footage(2)])
        self.source.fork = lambda *a, **kw: self.source
        self.source.thumbnail = Mock(return_value=b'\xff\xd8\xffsmall-jpeg')
        self.source.read_media = Mock(side_effect=lambda a, b, offset, length: b'x' * length)
        self.service = Service(Config.from_values(self.settings.read()['config']), self.store,
                               self.source, FakeDestination(), self.control.stop, self.control)
        self.library = self.control.library
        self.token = camera_web.issue(self.store, self.bot.scope)['token']
        self.workspace = camera_web.Workspace(self.store.directory, self.settings, self.control)

    tearDown = test_bot.BotTests.tearDown

    def read(self, action, identifier=None, header=None):
        camera_library.touch(self.store, self.connection['id'])
        future = self.library.submit(self.connection['id'], action, identifier, header)
        self.assertTrue(self.library.process_one(self.service))
        return future.result()


class LibraryTests(Fixture, unittest.TestCase):
    def test_one_mp4_per_item_uses_confirmed_lrv_without_task_or_cache_writes(self):
        before = self.path.read_bytes()
        items = self.read('list')['files']
        self.assertEqual(2, len(items)); self.assertEqual(footage().size, items[0]['size'])
        self.assertEqual(12, items[0]['playback_size']); self.assertEqual('预览副本', items[0]['quality'])
        data, mime, headers = self.read('media', items[0]['id'], 'bytes=3-6')
        self.assertEqual(b'xxxx', data); self.assertEqual('video/mp4', mime)
        self.assertEqual('bytes 3-6/12', headers['Content-Range'])
        self.assertEqual([], self.store.files()); self.assertEqual([], list(self.store.cache.glob('*.bin')))
        self.assertFalse(self.source.downloads); self.assertFalse(self.source.deletes)
        self.assertEqual(before, self.path.read_bytes())

    def test_read_lease_blocks_import_deletion_but_not_upload_policy_and_expires(self):
        from media_importer.camera_coordinator import source_wait_reason
        self.settings.set_enabled(True)
        self.settings.update_preferences({'backup': {'delete_source_after_import': True}})
        camera_library.touch(self.store, self.connection['id'])
        self.assertIn('浏览', source_wait_reason(self.store))
        self.assertFalse(self.service.allowed('delete_source_after_import'))
        self.assertTrue(self.service.allowed())
        with patch('media_importer.camera_library.time.time', return_value=time.time() + 30):
            self.assertIsNone(source_wait_reason(self.store))
        camera_library.touch(self.store, self.connection['id'], release=True)
        self.assertIsNone(source_wait_reason(self.store))

    def test_browsing_allows_existing_complete_cache_upload_without_new_imports(self):
        self.settings.set_enabled(True)
        config = self.control.adopt()
        remote = FakeDestination()
        service = Service(config, self.store, self.source, remote, self.control.stop, self.control)
        service.prepare_imports(); service.import_asset(service.rows()[0])
        count = self.source.snapshot_calls
        camera_library.touch(self.store, self.connection['id'])
        service.safe_cycle()
        self.assertEqual(1, len(remote.uploads))
        self.assertEqual(1, len(self.source.downloads))
        self.assertEqual(count, self.source.snapshot_calls)
        self.assertFalse(self.source.deletes)

    def test_preview_or_changed_session_never_reads_source(self):
        self.store.set_meta('live_preview', {'state': 'ready'})
        with self.assertRaisesRegex(ConfigError, '预览'): self.read('list')
        self.store.set_meta('live_preview', {'state': 'stopped'})
        future = self.library.submit('wrong-session', 'list')
        self.library.process_one(self.service)
        with self.assertRaisesRegex(ConfigError, '连接已变化'): future.result()
        self.assertEqual(0, self.source.snapshot_calls)

    def test_cancelled_browser_and_unknown_file_do_not_read_camera(self):
        camera_library.touch(self.store, self.connection['id'])
        future = self.library.submit(self.connection['id'], 'list'); future.cancel()
        self.library.process_one(self.service)
        self.assertFalse(self.source.snapshot_calls)
        with self.assertRaisesRegex(ConfigError, '清单'): self.read('thumbnail', 'f' * 32)
        self.source.thumbnail.assert_not_called()

    def test_range_parser_handles_ios_probe_seek_suffix_and_bounded_reads(self):
        for header, size, expected in [('bytes=0-1', 100, (0, 2)), ('bytes=20-', 100, (20, 80)),
                                      ('bytes=-8', 100, (92, 8)), ('bytes=0-', 8000000, (0, 1048576))]:
            self.assertEqual(expected, camera_library.byte_range(header, size))
        for header in [None, 'bytes=10-2', 'bytes=-0', 'bytes=100-', 'bytes=0-1,4-9', 'bytes=-', 'bytes=1e3-']:
            with self.assertRaises(ConfigError): camera_library.byte_range(header, 100)

    def test_control_capability_expires_and_binding_change_revokes_it(self):
        self.assertEqual(self.bot.scope, camera_web.authorize(self.store, self.token)['owner'])
        with self.assertRaises(ConfigError): camera_web.authorize(self.store, 'a' * 64)
        with patch('media_importer.camera_web.time.time', return_value=time.time() + 3601):
            with self.assertRaises(ConfigError): camera_web.authorize(self.store, self.token)
        binding = self.store.get_meta('source_selection'); binding['serial_number'] = 'different'
        self.store.set_meta('source_selection', binding)
        with self.assertRaises(ConfigError): camera_web.authorize(self.store, self.token)


class WebTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from websockets.asyncio.server import serve
        self.gateway = Gateway(self.store.directory, {'local': '', 'public': '', 'port': 0}, self.settings, self.control)
        self.server = await serve(self.gateway.stream, '127.0.0.1', 0, process_request=self.gateway.request)
        self.port = self.server.sockets[0].getsockname()[1]
        self.gateway.config['local'] = 'http://127.0.0.1:' + str(self.port)
        self.path = '/camera/' + self.token + '/'

    async def asyncTearDown(self):
        self.server.close(); await self.server.wait_closed()

    async def http(self, path, extra=''):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write((f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n{extra}Connection: close\r\n\r\n').encode())
        await writer.drain()
        result = await asyncio.wait_for(reader.read(), 3)
        writer.close(); await writer.wait_closed()
        return result

    async def test_owner_page_and_strict_origin_control_handshake(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        response = await self.http(self.path)
        self.assertIn(b'200 OK', response); self.assertIn('相机素材'.encode(), response)
        url = f'ws://127.0.0.1:{self.port}' + self.path + 'control'
        for origin in [None, 'https://evil.example']:
            with self.assertRaises(InvalidStatus):
                async with connect(url, origin=origin, proxy=None): pass
        async with connect(url, origin=self.gateway.config['local'], proxy=None) as ws:
            await ws.send(json.dumps({'id': 1, 'request': {'action': 'status'}}))
            value = json.loads(await ws.recv())
            self.assertIn('commands', value['result'])
            self.assertNotIn('example-secret', json.dumps(value)); self.assertNotIn('source_selection', json.dumps(value))
        self.assertIn(b'404', await self.http(self.path + 'media/../../env'))
        self.assertIn(b'410', await self.http('/camera/' + 'a' * 64 + '/'))

    async def test_click_retries_use_existing_capture_queue_and_preserve_sync(self):
        value = {'action': 'capture', 'command': 'photo', 'nonce': 'c' * 32,
                 'session_id': self.connection['id'], 'parameters': {}}
        with patch('media_importer.connections.wireless_supported', return_value=True):
            first = await self.workspace.dispatch(self.token, value)
            second = await self.workspace.dispatch(self.token, value)
        self.assertEqual(first, second)
        self.assertEqual(1, self.store.db.execute('select count(*) from capture_requests').fetchone()[0])
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
        self.assertFalse(self.source.deletes)

    async def test_control_tokens_not_interchangeable_with_viewer_and_no_arbitrary_commands(self):
        viewer = 'd' * 64
        self.store.set_meta('live_preview', {'state': 'ready', 'viewer_token': viewer})
        with self.assertRaises(ConfigError): await self.workspace.dispatch(viewer, {'action': 'status'})
        for value in [{'action': 'delete'}, {'action': 'status', 'owner': 'other'},
                      {'action': 'capture', 'command': 'format', 'session_id': self.connection['id'], 'nonce': 'c'*32, 'parameters': {}}]:
            with self.assertRaises(ConfigError): await self.workspace.dispatch(self.token, value)
        self.assertFalse(self.source.deletes)

    async def test_range_response_proxies_only_known_asset_and_supports_ios_probe(self):
        item = self.read('list')['files'][0]
        task = asyncio.create_task(self.http(self.path+'media/'+item['id'], 'Range: bytes=0-1\r\n'))
        for _ in range(30):
            await asyncio.sleep(.01)
            if self.library.process_one(self.service): break
        response = await task
        self.assertIn(b'206 Partial Content', response)
        self.assertIn(b'Content-Range: bytes 0-1/12', response)
        self.assertTrue(response.endswith(b'xx'))
        self.assertFalse(self.store.files()); self.assertFalse(self.source.deletes)


class AdapterTests(unittest.TestCase):
    def test_camera_thumbnail_is_bounded_jpeg_and_identity_checked(self):
        source = GoProSource(configuration()); source.verify_primary = Mock()
        source._read = Mock(return_value=(b'\xff\xd8\xffjpeg', 200, {}))
        self.assertTrue(source.thumbnail(footage()).startswith(b'\xff\xd8'))
        self.assertIn('path=100GOPRO/', source._read.call_args.args[0])
        source.verify_primary.assert_called_once()
        source._read.return_value = (b'<html>bad</html>', 200, {})
        with self.assertRaises(SourceError): source.thumbnail(footage())

    def test_playback_changed_source_or_wrong_proxy_cannot_be_served(self):
        source = GoProSource(configuration()); source.verify_primary = Mock(side_effect=SourceError('changed'))
        source._open = Mock()
        with self.assertRaises(SourceError): source.read_media(footage(), footage(primary=False), 0, 2)
        source._open.assert_not_called(); source.verify_primary.side_effect = None
        with self.assertRaises(SourceError): source.read_media(footage(), footage(2, primary=False), 0, 2)
        source._open.assert_not_called()


class ParameterTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_controls_require_camera_vocabulary_and_current_capabilities(self):
        camera = Mock(); camera.serial = 'example'; camera.state = AsyncMock(return_value={'preset': 1})
        raw = {'presetGroupArray': [{'presetArray': [{'id': 1, 'mode': 12, 'titleId': 'PRESET_TITLE_STANDARD'}]}]}
        definitions = {'settings': [{'id': 115, 'display_name': 'White Balance', 'options': [
            {'id': 0, 'display_name': 'Auto'}, {'id': 3, 'display_name': '6500K'}]},
            {'id': 116, 'display_name': 'Another setting', 'options': [{'id': 0, 'display_name': 'unsafe'}]}]}
        with patch('media_importer.adapters.gopro_radio.http_identity'), \
                patch('media_importer.adapters.gopro_radio.http', side_effect=[raw, {'settings': {'115': 0, '116': 0}}, definitions]), \
                patch.object(gopro_capture, 'choices', AsyncMock(return_value=[0, 3, 99])):
            result = await gopro_capture.catalog(camera)
        self.assertEqual(['white_balance'], [p['key'] for p in result['parameters']])
        self.assertEqual('自动', result['parameters'][0]['value_label'])
        self.assertEqual([0, 3], [o['value'] for o in result['parameters'][0]['options']])

    async def test_missing_legacy_api_keeps_standard_settings_available(self):
        camera = Mock(); camera.serial = 'example'; camera.state = AsyncMock(return_value={'preset': 1})
        raw = {'presetGroupArray': [{'presetArray': [{'id': 1, 'mode': 12, 'titleId': 'PRESET_TITLE_STANDARD'}]}]}
        with patch('media_importer.adapters.gopro_radio.http_identity'), \
                patch('media_importer.adapters.gopro_radio.http', side_effect=[raw, {'settings': {'2': 9, '115': 0}}, OSError()]), \
                patch.object(gopro_capture, 'choices', AsyncMock(return_value=[9])):
            result = await gopro_capture.catalog(camera)
        self.assertEqual(['resolution'], [p['key'] for p in result['parameters']])
