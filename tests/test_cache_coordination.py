"""Small-file concurrency: deletion protects only its own cache group."""
import threading
import unittest

from media_importer.service import Service
from media_importer.store import Store
from media_importer.storage import CacheGuard
from test_service import configuration
import test_reliability


class CacheCoordinationTests(unittest.TestCase):
    setUp = test_reliability.ReliabilityTests.setUp
    tearDown = test_reliability.ReliabilityTests.tearDown
    service = test_reliability.ReliabilityTests.service
    cached = test_reliability.ReliabilityTests.cached

    def test_slow_camera_delete_preserves_group_and_allows_unrelated_upload_to_finish(self):
        self.config = configuration(delete_source_after_import=True, archive_previews=False)
        service = self.service()
        rows = self.cached()
        group = next(g for g in self.store.groups(service.destination_id) if rows[0]['id'] in g['members'])
        deleting, release = threading.Event(), threading.Event()
        errors = []

        def slow_delete(asset):
            deleting.set()
            if not release.wait(5):
                raise AssertionError('fixture timed out')

        def clean_source():
            db = Store.create(self.store.directory)
            child = Service(self.config, db, self.source, self.remote, self.stop)
            child.cache_guard = service.cache_guard
            try:
                child.cleanup_groups(set(), groups=[group])
            except BaseException as exc:
                errors.append(exc)
            finally:
                db.close()

        self.source.on_delete = slow_delete
        thread = threading.Thread(target=clean_source)
        thread.start()
        try:
            self.assertTrue(deleting.wait(2))
            service.upload_cached(rows[0])
            self.assertTrue(self.store.cached_path(rows[0]).exists(), 'the protected group lost its local copy')
            service.upload_cached(rows[1])
            self.assertEqual('uploaded', self.store.get(rows[1]['id'])['state'])
            self.assertFalse(self.store.cached_path(rows[1]).exists(), 'an unrelated cache was blocked by camera IO')
            self.assertFalse(release.is_set())
        finally:
            release.set()
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        service.clean_cache(self.store.get(rows[0]['id']))
        self.assertFalse(self.store.cached_path(rows[0]).exists())
        self.assertEqual('deleted', self.store.get(rows[0]['id'])['cleanup'])

    def test_nested_protection_is_released_after_failure(self):
        guard = CacheGuard()
        with guard.protect(['one']):
            with self.assertRaises(RuntimeError):
                with guard.protect(['one', 'two']):
                    raise RuntimeError('fixture')
            self.assertTrue(guard.protected('one'))
            self.assertFalse(guard.protected('two'))
        self.assertFalse(guard.protected('one'))


if __name__ == '__main__':
    unittest.main()
