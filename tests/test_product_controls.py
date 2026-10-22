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
        def sending(*args, **kwargs):
            uploading.set()
            release.wait(3)
            return upload(*args, **kwargs)
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
        def sending(*args, **kwargs):
            upload_started.set()
            release_upload.wait(8)
            return upload(*args, **kwargs)
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


class DrainReceiptTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    message = test_bot.BotTests.message

    def request_stop(self):
        self.settings.set_enabled(True)
        self.sender.return_value = 'om_drain_result'
        self.bot.receive_message(self.message('当前文件完成后停止'))
        with patch('media_importer.bot.time.monotonic', side_effect=[1, 6]):
            self.assertTrue(self.bot.process_one())
        self.assertIn('等待生效', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        with self.bot.db() as db:
            db.execute('UPDATE bot_jobs SET retry_at=0')

    def applied(self):
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(),
                          configuration={'applied_revision': self.settings.read()['revision'], 'pending': False},
                          stages={name: {'phase': 'paused'} for name in ('import', 'upload')})
        with self.bot.db() as db:
            db.execute('UPDATE bot_jobs SET retry_at=0')

    def test_finished_drain_waits_for_actual_stop_then_updates_receipt_without_resave(self):
        self.request_stop()
        self.assertTrue(self.settings.finish_stop())
        saved = self.path.read_bytes()
        # Saved enabled=False is not proof that the worker has stopped yet.
        self.assertFalse(self.bot.process_one())
        self.updater.assert_not_called()
        self.applied()
        self.assertTrue(self.bot.process_one())
        self.assertEqual('om_drain_result', self.updater.call_args.args[1])
        text = json.dumps(self.updater.call_args.args[2], ensure_ascii=False)
        self.assertIn('同步已停止', text)
        self.assertNotIn('回执已失效', text)
        self.assertEqual(saved, self.path.read_bytes())

    def test_new_start_is_not_reported_as_finished_drain(self):
        self.request_stop()
        self.settings.set_enabled(True)
        self.assertFalse(self.settings.finish_stop())
        self.applied()
        self.assertTrue(self.bot.process_one())
        text = json.dumps(self.updater.call_args.args[2], ensure_ascii=False)
        self.assertNotIn('同步已停止', text)
        self.assertIn('设置已变更', text)
        self.assertTrue(self.settings.read()['config']['backup']['enabled'])

    def test_accepted_drain_receipt_keeps_waiting_until_current_files_finish(self):
        self.settings.set_enabled(True)
        self.sender.return_value = 'om_drain_result'
        self.bot.receive_message(self.message('当前文件完成后停止'))
        from media_importer.status import snapshot as actual_snapshot
        def adopt(*args):
            self.applied()
            return actual_snapshot(*args)
        with patch('media_importer.bot.snapshot', side_effect=adopt):
            self.assertTrue(self.bot.process_one())
        self.assertIn('当前文件完成后停止', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        with self.bot.db() as db:
            self.assertEqual('waiting', db.execute('SELECT state FROM bot_jobs').fetchone()[0])
            db.execute('UPDATE bot_jobs SET retry_at=0')
        self.assertFalse(self.bot.process_one())
        self.updater.assert_not_called()
        self.assertTrue(self.settings.finish_stop())
        self.applied()
        self.assertTrue(self.bot.process_one())
        self.assertEqual('om_drain_result', self.updater.call_args.args[1])
        self.assertIn('同步已停止', json.dumps(self.updater.call_args.args[2], ensure_ascii=False))
