"""Tiny media exercises format policy before any delete intent or network write."""
import io
import types
import unittest
from unittest.mock import patch

from media_importer.adapters.gopro import GoProSource
from media_importer.adapters.gopro_models import compatibility
from media_importer.completion import cleanup_summary
from media_importer.model import SourceError
import test_service as fixtures
from test_service import FakeSource, configuration, footage, replace


class CleanupPolicyTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp
    tearDown = fixtures.ServiceTests.tearDown
    service = fixtures.ServiceTests.service

    def wireless(self, assets):
        self.config = configuration(delete_source_after_import=True)
        self.config.source['transport'] = 'wifi'
        self.source = FakeSource(assets)
        self.source.transport = 'wifi'
        self.source.deletion_support = types.MethodType(GoProSource.deletion_support, self.source)
        self.store.set_meta('source_selection', {
            'type': 'gopro', 'serial_number': self.config.source['serial'], 'transport': 'wifi',
            'compatibility': compatibility('HERO9 Black', 'HD9.01.01.72.00', 'wifi')})
        return self.service()

    def test_verified_single_jpg_is_deleted_only_after_complete_import(self):
        photo = replace(footage(), path='100GOPRO/GOPR0001.JPG')
        service = self.wireless([photo])
        def deleting(asset):
            cached = list(self.store.cache.glob('*.bin'))
            self.assertEqual(1, len(cached))
            self.assertEqual(FakeSource.payload(photo), cached[0].read_bytes())
        self.source.on_delete = deleting
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual([photo.path], self.source.deletes)
        self.assertEqual('uploaded', self.store.files()[0]['state'])
        self.assertEqual('deleted', self.store.files()[0]['cleanup'])

    def test_partial_photo_import_never_deletes_original(self):
        photo = replace(footage(), path='100GOPRO/GOPR0001.JPG')
        service = self.wireless([photo]); self.source.fail_download = True
        self.assertFalse(service.safe_cycle()['complete'])
        self.assertFalse(self.source.deletes)
        self.assertIn(photo.path, self.source.assets)
        self.assertFalse(list(self.store.cache.glob('*.download')))

    def test_unverified_format_never_sets_delete_intent_and_preserves_actual_reason(self):
        raw = replace(footage(), path='100GOPRO/GOPR0001.GPR')
        service = self.wireless([raw])
        self.assertTrue(service.cycle()['complete'])
        row = self.store.files()[0]
        self.assertEqual('unsupported', row['cleanup'])
        self.assertIsNone(row['source_cleanup_error'])
        self.assertFalse(self.source.deletes)
        calls = self.source.snapshot_calls
        with patch.object(self.store, 'update', wraps=self.store.update) as update:
            service.cleanup_groups(set())
        update.assert_not_called()
        self.assertEqual(calls, self.source.snapshot_calls)
        self.assertIn(raw.path, self.source.assets)

    def test_unsupported_companion_prevents_all_group_deletes(self):
        primary = footage()
        thm = replace(footage(primary=False), path='100GOPRO/GH010001.THM')
        service = self.wireless([primary, thm])
        self.config.backup['archive_previews'] = False
        self.assertTrue(service.cycle()['complete'])
        self.assertFalse(self.source.deletes)
        self.assertEqual(2, len(self.source.assets))
        self.assertEqual({'unsupported'}, {r['cleanup'] for r in self.store.files()})

    def test_unsupported_uncached_archive_still_preserves_complete_local_file(self):
        raw = replace(footage(), path='100GOPRO/GOPR0001.GPR')
        service = self.wireless([raw])
        self.destination.online = False
        self.assertFalse(service.safe_cycle()['complete'])
        row = self.store.files()[0]
        self.assertEqual('cached', row['state'])
        self.assertTrue(self.store.cached_path(row).is_file())
        self.assertIn(raw.path, self.source.assets)
        self.assertFalse(self.source.deletes)

    def test_retained_completed_group_retires_and_does_not_reappear_or_reupload(self):
        raw = replace(footage(), path='100GOPRO/GOPR0001.GPR')
        service = self.wireless([raw])
        service.settle()
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(1, service.settle())
        self.assertEqual([], self.store.files())
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM source_index').fetchone()[0])
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual([], self.store.files())
        self.assertEqual(1, len(self.destination.publishes))
        self.assertEqual(1, len(self.source.downloads))
        self.assertIn(raw.path, self.source.assets)

    def test_newly_supported_format_uses_existing_archive_without_reupload(self):
        raw = replace(footage(), path='100GOPRO/GOPR0001.GPR')
        service = self.wireless([raw])
        service.cycle(); service.settle()
        self.source.deletion_support = lambda asset: None
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual([raw.path], self.source.deletes)
        self.assertEqual(1, len(self.destination.publishes))
        self.assertEqual(1, len(self.source.downloads))

    def test_unknown_previous_delete_is_not_discarded_while_camera_is_offline(self):
        raw = replace(footage(), path='100GOPRO/GOPR0001.GPR')
        service = self.wireless([raw]); service.cycle()
        row = self.store.files()[0]
        self.store.update(row['id'], cleanup='deleting')
        self.source.online = False
        service.cleanup_groups(set())
        self.assertEqual('deleting', self.store.get(row['id'])['cleanup'])
        self.assertEqual(0, service.settle())
        self.source.online = True
        service.cleanup_groups(set())
        self.assertEqual('unsupported', self.store.get(row['id'])['cleanup'])
        self.assertFalse(self.source.deletes)

    def test_rejected_delete_keeps_original_error_instead_of_generic_pending_message(self):
        service = self.wireless([footage()]); self.source.fail_delete = True
        service.cycle()
        row = self.store.files()[0]
        self.assertEqual('pending', row['cleanup'])
        self.assertEqual('删除失败', row['source_cleanup_error'])

    def test_retained_files_are_not_counted_as_pending_or_deleted(self):
        rows = [{'cleanup': 'unsupported', 'state': 'uploaded', 'imported': True,
                 'cache_state': 'missing', 'group_id': 'group', 'source_cleanup_error': None},
                {'cleanup': 'unsupported', 'state': 'excluded', 'imported': False,
                 'group_id': 'group', 'source_cleanup_error': None}]
        summary = cleanup_summary(rows, True)
        self.assertEqual(0, summary['source_pending'])
        self.assertEqual(0, summary['preview_pending'])
        self.assertEqual(0, summary['source_deleted'])
        self.assertEqual(0, summary['preview_deleted'])
        self.assertEqual(1, summary['source_not_deleted'])


class WirelessPhotoAdapterTests(unittest.TestCase):
    def setUp(self):
        config = configuration(); config.source['transport'] = 'wifi'
        self.source = GoProSource(config)
        self.source.compatibility = compatibility('HERO9 Black', 'HD9.01.01.72.00', 'wifi')

    def test_single_jpg_delete_uses_exact_file_endpoint_and_identity_verification(self):
        photo = replace(footage(), path='100GOPRO/GOPR0001.JPG')
        response = io.BytesIO(); response.status = 200
        with patch.object(self.source, 'verify_primary') as verify, patch.object(self.source, '_open', return_value=response) as request:
            self.source.delete(photo)
        verify.assert_called_once_with(photo)
        request.assert_called_once_with('/gopro/media/delete/file?path=100GOPRO/GOPR0001.JPG')

    def test_burst_raw_unknown_and_unassociated_jpg_still_never_delete(self):
        for name in ('G0010001.JPG', 'GOPR0001.GPR', 'GOPR0001.THM', 'TEST.JPG'):
            asset = replace(footage(), path='100GOPRO/' + name)
            with self.subTest(name=name), patch.object(self.source, 'verify_primary'), patch.object(self.source, '_open') as request:
                with self.assertRaises(SourceError): self.source.delete(asset)
                request.assert_not_called()
        photo = replace(footage(primary=False), path='100GOPRO/GOPR0001.JPG')
        self.assertIsNotNone(self.source.deletion_support(photo))

    def test_identity_change_prevents_even_supported_jpg_delete(self):
        photo = replace(footage(), path='100GOPRO/GOPR0001.JPG')
        with patch.object(self.source, 'verify_primary', side_effect=SourceError('身份变化')), patch.object(self.source, '_open') as request:
            with self.assertRaisesRegex(SourceError, '身份变化'): self.source.delete(photo)
        request.assert_not_called()
