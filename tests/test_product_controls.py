import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_runtime
import test_bot
from test_service import FakeSource, FakeDestination, footage
from media_importer.config import Config
from media_importer.model import now
from media_importer.runtime import Control
from media_importer.service import Service
from media_importer.store import Store
from media_importer.status import snapshot
from media_importer.operations import enqueue, initialize, perform


class DrainTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown
    def test_finish_current_preserves_parallel_current_files_and_does_not_start_next(self):
        self.settings.update_preferences({'backup': {'delete_source_after_import': True}})
        control = Control(self.settings, self.stop)
        config = control.adopt()
        assets = [footage(i) for i in range(1, 5)]
        source, destination = FakeSource(assets), FakeDestination()
        second, uploading, release = threading.Event(), threading.Event(), threading.Event()
        download, upload = source.download, destination.upload
        seen = []
        def importing(asset, *args):
            seen.append(asset.path)
            if asset.identity == assets[1].identity:
                uploading.wait(3)
                second.set()
                release.wait(3)
            return download(asset, *args)
        def sending(*args):
            uploading.set()
            release.wait(3)
            return upload(*args)
        source.download, destination.upload = importing, sending
        result = {}
        def run():
            db = Store(self.data)
            try:
                result['value'] = Service(config, db, source, destination, self.stop, control).safe_cycle()
            finally:
                db.close()
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(second.wait(4))
            deleted_before = list(source.deletes)
            self.settings.update_preferences({'backup': {'stop_after_current': True}})
            control.refresh()
            self.assertFalse(control.transfer_stop.is_set())
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(self.settings.read()['config']['backup']['enabled'])
            self.assertEqual(assets[:2], [a for a in assets if a.path in seen])
            rows = self.store.files()
            self.assertEqual(['uploaded', 'cached', 'pending', 'pending'], [r['state'] for r in rows])
            self.assertEqual(deleted_before, source.deletes)
            self.assertTrue(self.store.cached_path(rows[1]).exists())
        finally:
            release.set()
            thread.join(5)

    def test_drain_survives_restart_without_starting_a_file_and_start_cancels_it(self):
        self.settings.update_preferences({'backup': {'stop_after_current': True}})
        control = Control(self.settings, self.stop)
        config = control.adopt()
        source, remote = FakeSource([footage()]), FakeDestination()
        result = Service(config, self.store, source, remote, self.stop, control).safe_cycle()
        self.assertEqual('paused', result['reason'])
        self.assertFalse(self.store.files())
        self.settings.set_enabled(True)
        self.settings.update_preferences({'backup': {'stop_after_current': True}})
        self.settings.set_enabled(True)
        self.assertFalse(self.settings.finish_stop())
        self.assertTrue(self.settings.read()['config']['backup']['enabled'])

    def test_drain_failure_does_not_restart_failed_file(self):
        source, remote = FakeSource([footage(), footage(2)]), FakeDestination()
        control = Control(self.settings, self.stop)
        config = control.adopt()
        def download(*args):
            self.settings.update_preferences({'backup': {'stop_after_current': True}})
            raise OSError('test interrupted')
        source.download = download
        result = Service(config, self.store, source, remote, self.stop, control).safe_cycle()
        self.assertEqual('paused', result['reason'])
        self.assertTrue(all(r['state'] == 'pending' for r in self.store.files()))
        self.assertEqual([], source.deletes)

    def test_cancel_drain_resumes_import_before_current_upload_finishes(self):
        control = Control(self.settings, self.stop)
        config = control.adopt()
        assets = [footage(i) for i in range(1, 4)]
        source, remote = FakeSource(assets), FakeDestination()
        upload_started, release_upload = threading.Event(), threading.Event()
        upload, download = remote.upload, source.download
        def sending(*args):
            upload_started.set()
            release_upload.wait(8)
            return upload(*args)
        remote.upload = sending
        # Begin with one full cache so uploading is already active when import drains.
        self.store.batch(config.destination_id, [assets[0]])
        first = self.store.files()[0]
        Service(config, self.store, source, remote, self.stop, control).import_asset(first)
        # The next source file requests drain only after the upload worker has started.
        def request_drain(asset, *args):
            if asset.identity == assets[1].identity:
                self.assertTrue(upload_started.wait(3))
                self.settings.update_preferences({'backup': {'stop_after_current': True}})
            return download(asset, *args)
        source.download = request_drain
        result = {}
        def run():
            db = Store(self.data)
            try:
                result['value'] = Service(config, db, source, remote, self.stop, control).safe_cycle()
            finally:
                db.close()
        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if self.store.get_meta('status', {}).get('stages', {}).get('import', {}).get('phase') == 'draining':
                    break
                time.sleep(.02)
            self.assertEqual('draining', self.store.get_meta('status')['stages']['import']['phase'])
            self.settings.set_enabled(True)
            control.refresh()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and assets[2].path not in source.downloads:
                time.sleep(.02)
            self.assertIn(assets[2].path, source.downloads)
            self.assertFalse(release_upload.is_set())
            self.assertTrue(self.settings.read()['config']['backup']['enabled'])
        finally:
            release_upload.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result['value']['complete'])


class MaintenanceTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    callback = test_bot.BotTests.callback

    def test_check_and_cache_verification_results_display_beijing_time(self):
        for kind in ('check', 'verify_cache'):
            with self.subTest(kind=kind):
                self.request(kind, identifier=kind)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE maintenance_requests SET state='done', result=?, updated_at=? WHERE id=?",
                        (json.dumps({'state': 'ok', 'checks': {}}), '2026-10-16T16:58:23+00:00', kind))
                text = json.dumps(self.bot.maintenance_panel(kind), ensure_ascii=False)
                self.assertIn('检查结束 · 10-17 00:58:23 北京时间', text)
                self.assertNotIn('+00:00', text)

    def test_check_card_queues_work_and_later_replies_once(self):
        before = self.path.read_bytes()
        action = self.bot.action({'operation': 'check'}, self.settings.read()['revision'])
        self.bot.receive_card(self.callback(action))
        self.assertTrue(self.bot.process_one())
        row = self.store.db.execute('SELECT * FROM maintenance_requests').fetchone()
        self.assertEqual('pending', row['state'])
        self.assertEqual(self.settings.token()[1], row['target'])
        control = Control(self.settings, threading.Event())
        control.adopt()
        with patch('media_importer.preflight.run', return_value={'state': 'ok', 'checks': {}}):
            perform(self.store, control, threading.Event())
        self.bot.process_one()
        self.assertEqual(2, self.sender.call_count)
        self.assertFalse(self.bot.process_one())
        self.assertEqual(before, self.path.read_bytes())

    def test_changed_target_cannot_execute_old_check_request(self):
        control = self.request('check')
        self.settings.update_preferences({'limits': {'max_cache_gib': 51}})
        with patch('media_importer.preflight.run') as run:
            perform(self.store, control, threading.Event())
            run.assert_not_called()

    def request(self, kind, identifier='test-request'):
        with self.store.db:
            enqueue(self.store.db, identifier, kind, self.settings.token()[1])
        control = Control(self.settings, threading.Event())
        control.adopt()
        return control

    def test_check_result_is_delivered_once_without_enabling_sync(self):
        control = self.request('check')
        before = self.path.read_bytes()
        with patch('media_importer.preflight.run', return_value={'state': 'ok', 'checks': {}}):
            self.assertTrue(perform(self.store, control, threading.Event()))
        self.assertTrue(self.bot.process_one())
        self.assertFalse(self.bot.process_one())
        self.assertEqual(1, self.sender.call_count)
        self.assertIn('连接检查结果', str(self.sender.call_args))
        self.assertEqual(before, self.path.read_bytes())

    def test_check_result_retry_keeps_same_delivery_id(self):
        control = self.request('check')
        with patch('media_importer.preflight.run', return_value={'state': 'ok', 'checks': {}}):
            perform(self.store, control, threading.Event())
        self.sender.side_effect = [OSError('response lost'), 'message-id']
        self.assertFalse(self.bot.process_one())
        first_id = self.sender.call_args.kwargs['event_id']
        with self.bot.db() as db:
            db.execute("UPDATE bot_jobs SET retry_at=0 WHERE state='applied'")
        self.assertTrue(self.bot.process_one())
        self.assertEqual(first_id, self.sender.call_args.kwargs['event_id'])
        self.assertIn(self.bot.scope, first_id)
        self.assertFalse(self.bot.process_one())

    def test_verification_waits_for_transfer_lock_and_does_not_delete_or_upload(self):
        control = self.request('verify_cache')
        control.work_guard.acquire()
        try:
            self.assertFalse(perform(self.store, control, threading.Event()))
        finally:
            control.work_guard.release()
        with patch('media_importer.adapters.create_source', return_value=FakeSource([])), \
             patch('media_importer.adapters.create_destination', return_value=FakeDestination()), \
             patch('media_importer.service.Service.verify_caches', return_value={'verified': [], 'pending': []}) as verify:
            self.assertTrue(perform(self.store, control, threading.Event()))
            verify.assert_called_once()
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])

    def test_verification_rejects_enabled_sync_or_changed_destination(self):
        control = self.request('verify_cache')
        self.settings.set_enabled(True)
        with patch('media_importer.service.Service.verify_caches') as verify:
            perform(self.store, control, threading.Event())
            verify.assert_not_called()
        row = self.store.db.execute('SELECT result FROM maintenance_requests').fetchone()
        self.assertIn('先停止同步', row[0])

    def test_all_issue_pages_are_available_and_foreign_input_is_plain(self):
        from test_service import footage
        config = Config.load(self.path)
        self.store.batch(config.destination_id, [footage(i) for i in range(1, 13)])
        for row in self.store.files():
            self.store.update(row['id'], error='<at id=all>problem</at>')
        first = self.bot.panel('issues')
        last = self.bot.panel('issues', offset=10)
        text = json.dumps(last, ensure_ascii=False)
        self.assertIn('第 3 页', text)
        self.assertIn('GH010012.MP4', text)
        self.assertNotIn('GH010001.MP4', text)
        for element in first['elements']:
            if '<at' in str(element):
                self.assertEqual('plain_text', element['text']['tag'])

    def test_draining_card_keeps_progress_and_exposes_immediate_stop(self):
        self.settings.set_enabled(True)
        self.settings.update_preferences({'backup': {'stop_after_current': True}})
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now())
        result = self.bot.panel()
        text = json.dumps(result, ensure_ascii=False)
        self.assertIn('传完当前文件后停止', text)
        self.assertIn('立即停止', text)
        self.assertIn('继续同步', text)
