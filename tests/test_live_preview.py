"""Isolated preview ownership, parallel transfers, packaging and Feishu controls."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

import test_bot
import test_runtime
from test_service import FakeSource, FakeDestination, footage
from test_wireless_connections import wireless
from media_importer import live_preview as preview
from media_importer import preview_worker
from media_importer.adapters.gopro import GoProSource, preview_support
from media_importer.capture import enqueue as capture
from media_importer.config import Config
from media_importer.model import ConfigError, SourceError, Cancelled
from media_importer.runtime import Control
from media_importer.service import Service
from media_importer.source_connection import observe, decide
from media_importer.preview_cards import supported
from scripts.preview_bundle import unpack, bundle


class PreviewTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    message = test_bot.BotTests.message
    callback = test_bot.BotTests.callback

    def bind(self):
        state = observe(self.store, wireless())
        decide(self.store, self.settings, state['id'], True)
        self.settings.set_enabled(False)
        return state

    def request(self, key='one'):
        with patch.object(preview, 'available'):
            return preview.request(self.store, self.settings, self.bot.scope, key)

    def test_open_deduplicates_without_changing_settings_tasks_or_cache(self):
        self.bind(); before = self.path.read_bytes()
        state = self.request()
        again = self.request('second-click')
        self.assertEqual(state['id'], again['id'])
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.store.files())
        self.assertFalse(list(self.store.cache.glob('*.download')))

    def test_unconfirmed_camera_or_usb_only_never_starts_preview(self):
        with self.assertRaisesRegex(ConfigError, '确认设备'):
            self.request()
        self.bind(); self.settings.update_preferences({'backup': {'source_transport': 'usb'}})
        with self.assertRaisesRegex(ConfigError, '自动或无线'):
            self.request()

    def test_stop_is_owner_and_session_bound(self):
        self.bind(); state = self.request()
        for owner, identifier in [('someone', state['id']), (self.bot.scope, 'b' * 32)]:
            with self.assertRaises(ConfigError): preview.stop_request(self.store, owner, identifier)
        self.assertFalse(self.store.get_meta(preview.KEY).get('stop_requested'))
        preview.stop_request(self.store, self.bot.scope, state['id'])
        self.assertTrue(self.store.get_meta(preview.KEY)['stop_requested'])

    def test_cancelled_queued_request_does_not_start_worker(self):
        self.bind(); state = self.request()
        preview.stop_request(self.store, self.bot.scope, state['id'])
        service = Service(Config.from_values(self.settings.read()['config']), self.store,
                          FakeSource([]), FakeDestination(), threading.Event())
        with patch.object(preview, 'available'), patch.object(preview.subprocess, 'Popen') as spawn:
            self.assertTrue(preview.process_one(service, self.settings))
        spawn.assert_not_called()
        self.assertEqual('stopped', self.store.get_meta(preview.KEY)['state'])

    def test_restart_only_queues_cleanup_never_resumes_viewing(self):
        self.bind(); state = self.request()
        preview.update(self.store, state['id'], state='ready', source={'type': 'gopro'}, url='http://127.0.0.1/test')
        preview.restore(self.store)
        result = self.store.get_meta(preview.KEY)
        self.assertTrue(result['stop_requested']); self.assertTrue(result['cleanup_pending'])
        self.assertIsNone(result['url'])
        self.assertEqual('queued', result['state'])

    def test_cleanup_reconnects_while_sync_off_without_starting_a_new_preview(self):
        connection = self.bind(); state = self.request()
        before = self.path.read_bytes()
        preview.update(self.store, state['id'], state='queued', stop_requested=True,
                       cleanup_pending=True, source={'type': 'gopro'})
        self.store.set_meta('source_connection', {**connection, 'state': 'approved', 'online': False, 'last_seen': 0})
        stop = threading.Event()
        service = Service(Config.from_values(self.settings.read()['config']), self.store,
                          FakeSource([]), FakeDestination(), stop)
        process = Mock(pid=12345678)
        process.poll.return_value = 0
        process.stdin.closed = False
        process.stdout = io.StringIO(json.dumps({'event': 'closed', 'camera_stopped': True}) + '\n')
        def reconnect(_delay):
            intent = self.store.get_meta('source_connect_request')
            self.assertEqual(state['id'], intent['preview_id'])
            self.assertGreater(intent['until'], time.time())
            observe(self.store, wireless())
        with patch.object(preview, 'available'), patch.object(stop, 'wait', side_effect=reconnect), \
                patch.object(preview.subprocess, 'Popen', return_value=process), \
                patch.object(preview.os, 'killpg'):
            self.assertTrue(preview.process_one(service, self.settings))
        command = json.loads(process.stdin.write.call_args_list[0].args[0])
        self.assertTrue(command['cleanup_only'])
        self.assertEqual('stopped', self.store.get_meta(preview.KEY)['state'])
        self.assertFalse(self.store.get_meta(preview.KEY)['cleanup_pending'])
        self.assertFalse(self.store.get_meta('source_connect_request'))
        self.assertEqual(before, self.path.read_bytes())

    def test_unknown_stop_blocks_source_cleanup_and_can_be_retried(self):
        connection = self.bind(); state = self.request()
        preview.update(self.store, state['id'], state='error', cleanup_pending=True)
        self.assertTrue(preview.active(self.store))
        self.assertEqual('queued', self.request('retry')['state'])
        with self.assertRaisesRegex(ConfigError, '待核验'):
            capture(self.store, self.bot.scope, 'key', 'photo', connection['id'])
        service = Service(Config.from_values(self.settings.read()['config']), self.store,
                          FakeSource([]), FakeDestination(), threading.Event())
        self.assertFalse(service.allowed('delete_source_after_import'))

    def test_queued_camera_write_waits_for_preview_stop_confirmation(self):
        connection = self.bind()
        with patch('media_importer.connections.wireless_supported', return_value=True):
            capture(self.store, self.bot.scope, 'photo-before-preview', 'photo', connection['id'])
        with self.assertRaisesRegex(ConfigError, '相机操作正在执行'):
            self.request()
        # Previously persisted or concurrent intents must also be rechecked at
        # execution, even when they bypass today's admission contract.
        self.store.set_meta(preview.KEY, {'id': 'a' * 32, 'state': 'error', 'cleanup_pending': True})
        from media_importer.capture import process_one
        service = Service(Config.from_values(self.settings.read()['config']), self.store,
                          FakeSource([]), FakeDestination(), threading.Event())
        with patch('media_importer.adapters.source_link') as radio:
            self.assertFalse(process_one(service, self.settings))
        radio.assert_not_called()

    def test_overview_explains_preview_without_reporting_file_verification(self):
        from media_importer.bot_cards import headline_elements
        text = json.dumps(headline_elements({'running': True, 'status': {'source_work': True, 'preview_work': True}}), ensure_ascii=False)
        self.assertIn('预览进行中', text)
        self.assertNotIn('正在执行读写', text)

    def test_active_preview_does_not_block_enabled_upload_pipeline_start(self):
        self.bind(); self.request(); self.settings.set_enabled(True)
        service = Service(Config.from_values(self.settings.read()['config']), self.store,
                          FakeSource([]), FakeDestination(), threading.Event())
        with patch.object(preview.subprocess, 'Popen') as spawn:
            self.assertFalse(preview.process_one(service, self.settings))
        spawn.assert_not_called()

    def test_menu_card_updates_to_ready_then_stopped_on_same_message(self):
        self.bind(); before = self.path.read_bytes()
        self.sender.return_value = 'om_preview_fixture'
        with patch.object(preview, 'available'):
            self.assertTrue(self.bot.receive_card(self.callback(self.bot.action({'live_preview': {'version': 1, 'action': 'open'}}))))
            self.assertTrue(self.bot.process_one())
        state = self.store.get_meta(preview.KEY)
        preview.update(self.store, state['id'], state='ready', reason='实时画面已就绪',
                       url='http://127.0.0.1:12345/test/stream.html?src=preview&mode=mse')
        with self.bot.db() as db: db.execute('UPDATE bot_jobs SET retry_at=0')
        self.assertTrue(self.bot.process_one())
        content = json.dumps(self.updater.call_args.args[-1], ensure_ascii=False)
        self.assertIn('结束预览', content)
        self.assertEqual('om_preview_fixture', self.updater.call_args.args[1])
        preview.update(self.store, state['id'], state='stopped', url=None, reason='预览已结束')
        with self.bot.db() as db: db.execute('UPDATE bot_jobs SET retry_at=0')
        self.assertTrue(self.bot.process_one())
        self.assertNotIn('结束预览', json.dumps(self.updater.call_args.args[-1], ensure_ascii=False))
        self.assertEqual(before, self.path.read_bytes())
        self.sender.assert_called_once()

    def test_old_start_message_and_other_owner_are_rejected(self):
        self.bind()
        self.assertFalse(self.bot.receive_message(self.message('实时预览', owner='other')))
        self.bot.receive_message(self.message('实时预览', seconds=-600))
        self.bot.process_one()
        self.assertFalse(preview.active(self.store))
        token = self.bot.action({'live_preview': {'version': 1, 'action': 'open'}})
        self.assertEqual('error', self.bot.receive_card(self.callback(token, owner='other'))['toast']['type'])

    def test_only_known_callback_shapes_are_accepted(self):
        self.assertTrue(supported({'version': 1, 'action': 'open'}))
        self.assertFalse(supported({'version': 1, 'action': 'open', 'url': 'https://untrusted.example'}))
        self.assertFalse(supported({'version': 1, 'action': 'stop', 'id': '../'}))

    def test_source_failure_updates_card_and_sends_one_deduplicated_alert(self):
        self.bind(); self.sender.return_value='om_preview_fixture'
        with patch.object(preview,'available'):
            self.bot.receive_card(self.callback(self.bot.action({'live_preview': {'version': 1, 'action': 'open'}}))); self.bot.process_one()
        state=self.store.get_meta(preview.KEY)
        preview.update(self.store,state['id'],state='error',reason='相机视频流已中断',cleanup_pending=False)
        for _ in range(5):
            with self.bot.db() as db:db.execute('UPDATE bot_jobs SET retry_at=0')
            self.bot.process_one()
        self.assertEqual(2,self.sender.call_count)
        self.assertIn('相机视频流已中断',json.dumps(self.sender.call_args.args[1],ensure_ascii=False))
        with self.bot.db() as db:
            self.assertEqual(1,db.execute("SELECT count(*) FROM bot_jobs WHERE id LIKE 'preview:error:%'").fetchone()[0])

    def test_adapter_rejects_recording_unknown_state_and_existing_preview(self):
        self.bind()
        client = GoProSource(Config.from_values(self.settings.read()['config']))
        client.transport = 'wifi'
        for status in [{'10': 1, '8': 0, '32': 0}, {'10': 0, '8': 0, '32': 1}, {}]:
            with patch.object(client, 'identify', return_value=wireless()), patch.object(client, '_json', return_value={'status': status}):
                with self.assertRaises(SourceError): client.preview_info()
        self.assertFalse(preview_support({**wireless(), 'model_name': 'HERO13 Black'})['supported'])
        self.assertFalse(preview_support({**wireless(), 'transport': 'usb'})['supported'])

    def test_start_is_not_retried_and_stop_checks_actual_camera_status(self):
        self.bind(); client = GoProSource(Config.from_values(self.settings.read()['config']))
        with patch.object(client, '_open', side_effect=SourceError('response lost')) as request:
            with self.assertRaises(SourceError): client.preview_start()
            request.assert_called_once()
        with patch.object(client, 'identify'), patch.object(client, '_open'), patch.object(client, '_json', return_value={'status': {'32': 1}}):
            with self.assertRaisesRegex(SourceError, '确认停止'): client.preview_stop()

    def test_worker_exit_persists_uncertain_stop_and_reaps_owned_children(self):
        self.bind()
        for closed, pending in [(None, True), ({'event': 'closed', 'camera_stopped': True}, False)]:
            self.store.set_meta(preview.KEY, {})
            state = self.request(str(pending))
            process = Mock(pid=99999999)
            process.stdin = io.StringIO()
            process.stdout = io.StringIO(json.dumps(closed) + '\n' if closed else '')
            process.poll.return_value = 0
            service = Service(Config.from_values(self.settings.read()['config']), self.store,
                              FakeSource([]), FakeDestination(), threading.Event())
            with patch.object(preview, 'available'), patch.object(preview.subprocess, 'Popen', return_value=process), \
                    patch.object(preview.os, 'killpg') as kill:
                self.assertTrue(preview.process_one(service, self.settings))
            saved = self.store.get_meta(preview.KEY)
            self.assertEqual(pending, saved['cleanup_pending'])
            self.assertEqual('error' if pending else 'stopped', saved['state'])
            self.assertIsNone(saved['url'])
            self.assertFalse(self.store.get_meta('source_connect_request', {}))
            kill.assert_called_once_with(process.pid, preview.signal.SIGKILL)
            self.assertFalse(self.store.files())
            self.assertFalse(self.store.get_meta('status', {}).get('source_work'))
            self.assertFalse(self.store.get_meta('status', {}).get('preview_work'))


class WorkerTests(unittest.TestCase):
    def test_parent_eof_stops_worker_without_orphaning_preview(self):
        observed = []
        def work(source, stop, **kwargs):
            self.assertTrue(stop.wait(2)); observed.append(source)
        with patch.object(preview_worker.sys, 'stdin', io.StringIO('{"source": {"type": "fixture"}}\n')), \
                patch.object(preview_worker.signal, 'signal'), patch.object(preview_worker, 'run', side_effect=work):
            self.assertEqual(0, preview_worker.main())
        self.assertEqual([{'type': 'fixture'}], observed)

    def test_lost_start_response_still_stops_camera_and_component(self):
        source = Mock()
        source.preview_info.return_value = {'host': '192.0.2.1', 'port': 8554}
        source.preview_start.side_effect = SourceError('start response lost')
        source.preview_stop.return_value = None
        process = Mock(); process.poll.return_value = None
        port = Mock(); port.__enter__ = Mock(return_value=port); port.__exit__ = Mock()
        port.getsockname.return_value = ('127.0.0.1', 4321)
        udp = Mock()
        def receive(*_):
            time.sleep(.005)
            raise preview_worker.socket.timeout()
        udp.recvfrom.side_effect = receive
        response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock()
        response.status = 200
        events = []
        for failed_stop in (False, True):
            events.clear(); source.reset_mock(); process.reset_mock()
            source.preview_stop.side_effect = SourceError('offline') if failed_stop else None
            with patch.object(preview_worker, 'create_source', return_value=source), \
                    patch.object(preview_worker.socket, 'socket', side_effect=[udp, port]), \
                    patch.object(preview_worker, 'urlopen', return_value=response), \
                    patch.object(preview_worker, 'component', return_value=Path('/fixture/go2rtc')), \
                    patch('media_importer.preview_rtc.endpoint', return_value=None), \
                    patch.object(preview_worker.subprocess, 'Popen', return_value=process), \
                    patch.object(preview_worker.http.client, 'HTTPConnection'):
                preview_worker.run({}, threading.Event(), lambda event, **values: events.append((event, values)))
            source.preview_start.assert_called_once(); source.preview_stop.assert_called_once()
            process.terminate.assert_called_once(); process.wait.assert_called_once()
            self.assertEqual(not failed_stop, events[-1][1]['camera_stopped'])
            self.assertTrue(any(event == 'error' for event, _ in events))


class BundleTests(unittest.TestCase):
    def test_only_pinned_archive_is_extracted(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('go2rtc', b'fixture')
            archive.writestr('../outside', b'ignored')
        payload = content.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'components/go2rtc'
            with self.assertRaises(ValueError): unpack(payload, 'bad', target)
            self.assertFalse(target.exists())
            unpack(payload, hashlib.sha256(payload).hexdigest(), target)
            self.assertEqual(b'fixture', target.read_bytes())
            self.assertFalse((Path(folder) / 'outside').exists())

    def test_non_mac_packages_do_not_fetch_unusable_preview_components(self):
        with patch('scripts.preview_bundle.urlopen') as fetch:
            self.assertIsNone(bundle(Path('unused'), 'windows-amd64'))
        fetch.assert_not_called()


class PreviewParallelTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown

    def test_preview_preempts_partial_import_but_upload_completes(self):
        control = Control(self.settings, self.stop); config = control.adopt()
        assets = [footage(), footage(2)]
        source, remote = FakeSource(assets), FakeDestination()
        uploading, viewing, uploaded = threading.Event(), threading.Event(), threading.Event()
        original_download, original_upload = source.download, remote.upload
        attempts = []
        def download(asset, path, progress):
            attempts.append(asset.identity)
            if asset.identity == assets[1].identity and attempts.count(asset.identity) == 1:
                self.assertTrue(uploading.wait(3))
                path.write_bytes(b'partial')
                control.source_interrupt.set()
                raise Cancelled('preview requested')
            return original_download(asset, path, progress)
        def upload(*args, **kwargs):
            uploading.set(); self.assertTrue(viewing.wait(3))
            result = original_upload(*args, **kwargs); uploaded.set(); return result
        def preview_at_boundary(task, settings):
            if attempts.count(assets[1].identity) == 1 and not viewing.is_set():
                self.assertFalse(any(task.store.cache.glob('*.download')))
                viewing.set(); self.assertTrue(uploaded.wait(3))
                self.assertFalse(control.transfer_stop.is_set())
                return True
            return False
        source.download, remote.upload = download, upload
        with patch.object(preview, 'process_one', side_effect=preview_at_boundary):
            result = Service(config, self.store, source, remote, self.stop, control).safe_cycle()
        self.assertTrue(result['complete']); self.assertTrue(uploaded.is_set())
        self.assertEqual(2, len(remote.uploads)); self.assertFalse(source.deletes)
