"""Remote playback is read-only, revocable and independent of camera scheduling."""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from media_importer.preview_access import Gateway, links, configuration, media_request
from media_importer.store import Store
from media_importer.model import ConfigError, now
from media_importer.runtime import Control
from media_importer.service import Service
from media_importer import live_preview, capture
from test_service import FakeSource, FakeDestination, footage
import test_runtime
import test_bot
from test_live_preview import PreviewTests


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from websockets.asyncio.server import serve
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Store(self.root)
        self.token = 'a' * 64
        self.streams = 0
        self.commands = []
        async def upstream(ws):
            self.streams += 1
            try:
                async for command in ws:
                    self.commands.append(json.loads(command))
                    await ws.send(b'tiny-media-frame')
            except Exception: pass
        self.media = await serve(upstream, '127.0.0.1', 0)
        media_port = self.media.sockets[0].getsockname()[1]
        self.state = {'id': 'session-one', 'state': 'ready', 'viewer_token': self.token,
                      'upstream': f'http://127.0.0.1:{media_port}/' + 'b' * 32 + '/stream.html?src=preview'}
        self.db.set_meta('live_preview', self.state)
        self.config = {'local': '', 'public': 'https://preview.example.ts.net:8443', 'port': 0}
        self.gateway = Gateway(self.root, self.config)
        self.server = await serve(self.gateway.stream, '127.0.0.1', 0, process_request=self.gateway.request,
                                  close_timeout=.1, max_size=2048)
        self.config.update(port=self.server.sockets[0].getsockname()[1])
        self.config['local'] = 'http://127.0.0.1:' + str(self.config['port'])
        self.path = '/view/' + self.token + '/'

    async def asyncTearDown(self):
        self.server.close(); await self.server.wait_closed()
        self.media.close(); await self.media.wait_closed()
        self.db.close(); self.tmp.cleanup()

    async def http(self, path, host=None):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.config['port'])
        writer.write(('GET '+path+' HTTP/1.1\r\nHost: '+(host or self.config['local'][7:])+ '\r\nConnection: close\r\n\r\n').encode())
        await writer.drain()
        result = await asyncio.wait_for(reader.read(), 3)
        writer.close(); await writer.wait_closed()
        return result

    async def test_page_host_session_and_management_routes(self):
        result = await self.http(self.path)
        self.assertIn(b'200 OK', result)
        self.assertIn('关闭窗口不会结束预览'.encode(), result)
        for path in ('/api/streams', '/config', self.path+'socket?src=exec:evil', self.path+'../api'):
            self.assertIn(b'404', await self.http(path))
        self.assertIn(b'403', await self.http(self.path, 'untrusted.example'))
        self.assertIn(b'410', await self.http('/view/'+'c'*64+'/'))

    async def test_media_transfers_and_stop_revokes_an_open_viewer(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        url = 'ws'+self.config['local'][4:]+self.path+'socket'
        async with connect(url, proxy=None, origin=self.config['local']) as viewer:
            await viewer.send(json.dumps({'type': 'mse', 'value': 'avc1.640029'}))
            self.assertEqual(b'tiny-media-frame', await viewer.recv())
            self.assertEqual([{'type':'mse','value':'avc1.640029'}], self.commands)
            self.db.set_meta('live_preview', {**self.state, 'stop_requested': True})
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(viewer.recv(), 2)
        self.assertIn(b'410', await self.http(self.path))

    async def test_viewer_disconnect_preserves_session_and_reconnect(self):
        from websockets.asyncio.client import connect
        url = 'ws'+self.config['local'][4:]+self.path+'socket'
        for _ in range(2):
            async with connect(url, proxy=None) as viewer:
                await viewer.send('{"type":"mse","value":"avc1.640029"}')
                self.assertEqual(b'tiny-media-frame', await viewer.recv())
            self.assertEqual(self.state, self.db.get_meta('live_preview'))
        self.assertEqual(2,self.streams)

    async def test_management_messages_never_reach_upstream(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        async with connect('ws'+self.config['local'][4:]+self.path+'socket', proxy=None) as viewer:
            await viewer.send('{"type":"webrtc/offer","value":"arbitrary"}')
            with self.assertRaises(ConnectionClosed): await asyncio.wait_for(viewer.recv(),2)
        self.assertFalse(self.commands)

    async def test_foreign_origin_does_not_open_media(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        with self.assertRaises(InvalidStatus):
            async with connect('ws'+self.config['local'][4:]+self.path+'socket',proxy=None,origin='https://evil.example'): pass
        self.assertEqual(0,self.streams)

    async def test_hls_uses_only_bound_resources_and_revokes_in_flight_segments(self):
        master = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=192000\nhls/playlist.m3u8?id=abcDEF12'
        playlist = b'#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:0.500,\nsegment.ts?id=abcDEF12&n=1'
        with patch.object(self.gateway.hls, 'fetch', side_effect=[master, playlist, b'fragment']) as fetch:
            self.assertIn(b'abcDEF12/playlist.m3u8', await self.http(self.path+'hls/master.m3u8'))
            self.assertTrue(fetch.call_args.args[0].endswith('/api/stream.m3u8?src=preview'))
            response = await self.http(self.path+'hls/abcDEF12/playlist.m3u8')
            self.assertIn(b'segment.ts?n=1', response); self.assertNotIn(b'id=abcDEF12',response)
            response = await self.http(self.path+'hls/abcDEF12/segment.ts?n=1')
            self.assertIn(b'fragment', response); self.assertIn(b'video/mp2t',response)
            self.assertTrue(fetch.call_args.args[0].endswith('/api/hls/segment.ts?id=abcDEF12&n=1'))
            for path in ('hls/master.m3u8?src=other', 'hls/unknown1/segment.ts?n=1', 'hls/abcDEF12/segment.ts',
                         'hls/abcDEF12/playlist.m3u8?n=1', 'hls/abcDEF12/init.mp4'):
                self.assertIn(b'404', await self.http(self.path+path))
            self.assertEqual(3,fetch.call_count)
        def revoke(_url):
            db=Store(self.root)
            try:db.set_meta('live_preview',{**self.state,'stop_requested':True})
            finally:db.close()
            return b'fragment'
        with patch.object(self.gateway.hls,'fetch',side_effect=revoke):
            self.assertIn(b'410',await self.http(self.path+'hls/abcDEF12/segment.ts?n=2'))
        self.assertIn(b'410',await self.http(self.path+'hls/master.m3u8'))


class ConfigurationTests(unittest.TestCase):
    def test_launchd_probe_selects_tailscale_cli_without_a_terminal(self):
        from media_importer.host_network import preview_mapping
        mapping={'TCP':{'8443':{'HTTPS':True}},'Web':{'preview.example.ts.net:8443':{
            'Handlers':{'/':{'Proxy':'http://127.0.0.1:18765'}}}}}
        with patch.dict(os.environ,{},clear=True),patch('shutil.which',return_value='/test/tailscale'), \
                patch('media_importer.host_network.subprocess.run',return_value=Mock(stdout=json.dumps(mapping))) as run:
            self.assertEqual('configured',preview_mapping('https://preview.example.ts.net:8443',18765)['remote_state'])
            self.assertEqual('dumb',run.call_args.kwargs['env']['TERM'])

    def test_existing_configuration_defaults_to_loopback_and_remote_link_uses_origin(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(18765,configuration()['port'])
            self.assertTrue(links({'viewer_token':'a'*64})['url'].startswith('http://127.0.0.1:18765/'))
        with patch.dict(os.environ, {'PREVIEW_PUBLIC_URL':'https://preview.example.ts.net:8443'},clear=True):
            urls=links({'viewer_token':'a'*64})
            self.assertTrue(urls['remote_configured'])
            self.assertTrue(urls['url'].startswith('https://preview.example.ts.net:8443/view/'))
        for value in ('http://host','https://u:p@host','https://host/path','https://host/?x=1'):
            with patch.dict(os.environ, {'PREVIEW_PUBLIC_URL':value},clear=True):
                with self.assertRaises(ConfigError):configuration()

    def test_only_fixed_media_negotiation_is_forwarded(self):
        for value in ('{}','[]','{"type":"mse","value":"http://other"}',
                      '{"type":"mse","value":"avc1","src":"different"}'):
            with self.assertRaises(ValueError):media_request(value)


class SchedulingTests(unittest.TestCase):
    setUp=test_runtime.RuntimeTests.setUp
    tearDown=test_runtime.RuntimeTests.tearDown

    def test_preview_started_while_sync_off_does_not_block_later_cached_upload(self):
        control=Control(self.settings,self.stop); config=control.adopt()
        source,remote=FakeSource([footage()]),FakeDestination()
        service=Service(config,self.store,source,remote,self.stop,control)
        inventory=service.prepare_imports(); service.import_asset(service.rows()[0])
        self.settings.set_enabled(False); control.adopt()
        self.store.set_meta('live_preview',{'id':'session','state':'queued'})
        entered=threading.Event()
        def session(proxy,settings):
            proxy.store.set_meta('live_preview',{'id':'session','state':'ready'})
            entered.set();self.stop.wait(5)
        try:
            with patch.object(live_preview,'_run_session',side_effect=session):
                started=time.monotonic()
                self.assertTrue(live_preview.process_one(service,self.settings))
                self.assertLess(time.monotonic()-started,.5)
                self.assertTrue(entered.wait(2))
                self.settings.set_enabled(True); control.adopt()
                service.safe_cycle()
                self.assertEqual(1,len(remote.uploads))
                self.assertTrue(control.camera.thread.is_alive())
                self.settings.set_enabled(False); control.refresh()
                self.assertTrue(control.camera.thread.is_alive())
                self.assertFalse(source.deletes)
        finally:self.stop.set();control.camera.close()


class ControlTests(unittest.TestCase):
    setUp=test_bot.BotTests.setUp
    tearDown=test_bot.BotTests.tearDown
    bind=PreviewTests.bind

    def test_status_refresh_is_done_without_pending_camera_io_even_during_preview(self):
        connection=self.bind()
        self.store.set_meta('live_preview',{'state':'ready'})
        self.store.status(source={'serial_number':connection['device']['serial_number'],
                                 'telemetry':{'available':True,'recording':False,'observed_at':now()}})
        with patch('media_importer.connections.wireless_supported',return_value=True):
            op=capture.enqueue(self.store,'owner','query','state',connection['id'])
        self.assertEqual('done',op['state'])
        self.assertFalse(capture.mutating_pending(self.store.db))
        self.assertFalse(json.loads(op['result'])['recording'])

    def test_stop_is_allowed_while_preview_is_pending_cleanup(self):
        connection=self.bind()
        self.store.set_meta('live_preview',{'state':'error','cleanup_pending':True})
        with patch('media_importer.connections.wireless_supported',return_value=True):
            op=capture.enqueue(self.store,'owner','stop','stop_recording',connection['id'])
        self.assertEqual('queued',op['state'])

    def test_preview_to_recording_waits_for_confirmed_stop_and_never_replays(self):
        connection=self.bind()
        self.store.set_meta('live_preview',{'id':'preview','state':'ready',
                            'serial':connection['device']['serial_number'],'url':'private'})
        from media_importer.config import Config
        service=Service(Config.from_values(self.settings.read()['config']),self.store,
                        FakeSource([]),FakeDestination(),threading.Event())
        with patch('media_importer.connections.wireless_supported',return_value=True), \
                patch('media_importer.adapters.source_link',return_value={'recording':True}) as radio:
            op=capture.enqueue(self.store,'owner','transition','preview_record',connection['id'])
            self.assertTrue(self.store.get_meta('live_preview')['stop_requested'])
            self.assertIsNone(self.store.get_meta('live_preview')['url'])
            self.assertFalse(capture.process_one(service,self.settings)); radio.assert_not_called()
            self.store.set_meta('live_preview',{'state':'error','cleanup_pending':True})
            self.assertFalse(capture.process_one(service,self.settings)); radio.assert_not_called()
            self.store.set_meta('live_preview',{'state':'stopped','cleanup_pending':False})
            self.assertTrue(capture.process_one(service,self.settings))
            self.assertEqual('start_recording',radio.call_args.args[1])
            self.assertFalse(capture.process_one(service,self.settings)); radio.assert_called_once()
            self.assertEqual('done',capture.get(self.store.db,op['id'],'owner')['state'])

    def test_stop_has_priority_over_an_older_queued_capture(self):
        connection=self.bind()
        from media_importer.config import Config
        service=Service(Config.from_values(self.settings.read()['config']),self.store,
                        FakeSource([]),FakeDestination(),threading.Event())
        with patch('media_importer.connections.wireless_supported',return_value=True), \
                patch('media_importer.adapters.source_link',return_value={'recording':False}) as radio:
            capture.enqueue(self.store,'owner','photo','photo',connection['id'])
            capture.enqueue(self.store,'owner','stop','stop_recording',connection['id'])
            capture.process_one(service,self.settings)
            self.assertEqual('stop_recording',radio.call_args.args[1])
            self.assertFalse(capture.process_one(service,self.settings))
            radio.assert_called_once()

    def test_stable_menu_events_are_owner_checked_deduplicated_and_expire(self):
        event={'header':{'app_id':test_bot.ENV['FEISHU_APP_ID'],'event_id':'menu-one'},
               'event':{'event_key':'sync.status','timestamp':int(time.time()*1000),
                        'operator':{'operator_id':{'open_id':self.bot.owner}}}}
        self.assertTrue(self.bot.receive_menu(event));self.assertTrue(self.bot.receive_menu(event))
        with self.bot.db() as db:self.assertEqual(1,db.execute('select count(*) from bot_jobs').fetchone()[0])
        event['event']['operator']['operator_id']['open_id']='someone-else'
        self.assertFalse(self.bot.receive_menu(event))
        event['event']['operator']['operator_id']['open_id']=self.bot.owner
        event['event']['timestamp']-=600000
        self.assertFalse(self.bot.receive_menu(event))
