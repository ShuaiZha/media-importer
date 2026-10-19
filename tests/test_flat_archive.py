import copy
import json
import unittest
from pathlib import PurePosixPath

from media_importer.model import Cancelled, DestinationError
from media_importer.store import Store
import test_service as fixtures
from test_service import FakeSource, configuration, footage, replace


class FlatArchiveTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp
    tearDown = fixtures.ServiceTests.tearDown
    service = fixtures.ServiceTests.service

    def cached(self, asset=None):
        asset = asset or footage()
        self.store.batch(self.config.destination_id, [asset])
        row = self.store.get(self.store.file_id(self.config.destination_id, asset))
        self.store.cached_path(row).write_bytes(FakeSource.payload(asset))
        self.store.update(row['id'], state='cached', cache_identity=json.dumps(__import__('media_importer.storage',fromlist=['file_identity']).file_identity(self.store.cached_path(row))))
        return self.store.get(row['id'])

    def test_dates_dcim_directories_and_sidecars_share_one_flat_directory(self):
        assets = [footage(), replace(footage(), path='101GOPRO/GH010001.MP4', created='1792000000', gumi='another'),
                  footage(primary=False), replace(footage(2), path='102GOPRO/GOPR0002.JPG'),
                  replace(footage(3), path='103GOPRO/GOPR0003.GPR', created='')]
        self.source = FakeSource(assets)
        service = self.service(configuration(delete_source_after_import=False))
        self.assertTrue(service.safe_cycle()['complete'])
        parent = 'HERO9-' + assets[0].serial
        self.assertEqual({'GH010001.MP4', 'GH010001_1.MP4', 'GL010001.LRV', 'GOPR0002.JPG', 'GOPR0003.GPR'},
                         {PurePosixPath(path).name for path in self.destination.objects})
        self.assertEqual({parent}, {str(PurePosixPath(path).parent) for path in self.destination.objects})
        self.assertTrue(service.safe_cycle()['complete'])
        self.assertEqual(5, len(self.destination.uploads))
        self.assertEqual([], self.source.deletes)

    def test_allocation_combines_casefolded_remote_and_database_names(self):
        first = self.cached()
        parent = 'HERO9-' + footage().serial
        self.store.update(first['id'], remote_path=parent + '/gh010001_1.mp4')
        self.destination.objects[parent + '/gh010001.mp4'] = b'foreign'
        second = self.cached(replace(footage(), gumi='new-recording'))
        self.assertEqual(parent + '/GH010001_2.MP4', self.service().allocate(second))
        self.assertTrue(self.store.reserved(self.config.destination_id, parent + '/GH010001_1.MP4', second['id']))

    def test_more_than_100_collisions_keep_flat_suffixes(self):
        row = self.cached()
        parent = 'HERO9-' + footage().serial
        for index in range(121):
            self.destination.objects[parent + '/GH010001' + (f'_{index}' if index else '') + '.MP4'] = b'other'
        self.assertEqual(parent + '/GH010001_121.MP4', self.service().allocate(row))

    def test_conflict_appearing_during_upload_does_not_repeat_upload(self):
        row = self.cached()
        parent = 'HERO9-' + footage().serial
        original = self.destination.upload
        def upload(*args, **kwargs):
            original(*args, **kwargs)
            self.destination.objects[parent + '/gh010001.mp4'] = b'peer'
        self.destination.upload = upload
        self.service().upload_cached(row)
        self.assertEqual(1, len(self.destination.uploads))
        self.assertEqual(b'peer', self.destination.objects[parent + '/gh010001.mp4'])
        self.assertEqual(parent + '/GH010001_1.MP4', self.store.get(row['id'])['remote_path'])

    def test_atomic_rename_collision_reuses_temp_and_suffix_survives_restart(self):
        row = self.cached()
        original = self.destination.publish
        calls = []
        def publish(temporary, final):
            calls.append(final)
            if len(calls) == 1:
                self.destination.objects[final] = b'peer'
            elif len(calls) == 2:
                self.stop.set()
                raise Cancelled('paused before renamed publication')
            return original(temporary, final)
        self.destination.publish = publish
        with self.assertRaises(Cancelled):
            self.service().upload_cached(row)
        pending = self.store.get(row['id'])
        self.assertEqual('publishing', pending['state'])
        self.assertTrue(pending['remote_path'].endswith('_1.MP4'))
        self.assertEqual(FakeSource.payload(footage()), self.destination.objects[pending['temp_path']])
        self.store.close()
        self.store = Store(self.temporary.name)
        self.stop.clear()
        self.service().upload_cached(self.store.get(row['id']))
        result = self.store.get(row['id'])
        self.assertEqual('uploaded', result['state'])
        self.assertEqual(pending['remote_path'], result['remote_path'])
        self.assertEqual(1, len(self.destination.uploads))
        self.assertEqual(b'peer', self.destination.objects[calls[0]])
        self.assertFalse(self.store.cached_path(row).exists())

    def test_equal_size_foreign_final_cannot_recover_a_lost_publish(self):
        row = self.cached()
        final = self.service().allocate(row)
        self.store.update(row['id'], state='publishing', remote_path=final,
                          temp_path='HERO9-' + footage().serial + '/.lost.partial',
                          remote_identity=json.dumps({'inode': 1}))
        self.destination.objects[final] = b'foreign'.ljust(footage().size, b'x')
        self.destination.file_identity = lambda _: {'inode': 2}
        self.destination.fail_upload = True
        before = copy.deepcopy(self.destination.objects)
        with self.assertRaises(DestinationError):
            self.service().upload_cached(self.store.get(row['id']))
        self.assertNotEqual('uploaded', self.store.get(row['id'])['state'])
        self.assertTrue(self.store.cached_path(row).exists())
        self.assertEqual(before[final], self.destination.objects[final])


if __name__ == '__main__':
    unittest.main()
