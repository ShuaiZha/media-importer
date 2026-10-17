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
