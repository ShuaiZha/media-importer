import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from media_importer.config import Config, ConfigError, DEFAULTS, preferences
from media_importer.adapters import source_defaults, destination_defaults
from media_importer.storage import file_identity
from media_importer.model import Cancelled, CacheUnavailable
from media_importer.adapters.gopro import make_asset as Asset, GoProSource
from media_importer.model import SourceError, DestinationError, NameConflict, LocalCacheChanged
from media_importer.service import Service
from media_importer.store import Store, worker_lock


def configuration(**backup):
    raw = copy.deepcopy(DEFAULTS)
    raw["source"] = source_defaults()
    raw["destination"] = destination_defaults()
    raw["source"].update(url="http://172.21.123.51:8080", serial="C0000000000123")
    raw["destination"].update(host="192.0.2.20", share="media", username="example_user")
    # Legacy fixtures exercise the explicit full-archive option. Preview-policy
    # tests separately assert the production default and exclusion path.
    raw["backup"].update(enabled=True, delete_source_after_import=False, archive_previews=True)
    raw["backup"].update(backup)
    raw["backup"]["stability_seconds"] = 0
    raw["limits"]["min_free_disk_gib"] = 0
    return Config.from_values(raw)


def footage(index=1, primary=True, gumi=None):
    return Asset("C0000000000123", f"100GOPRO/{'GH' if primary else 'GL'}01{index:04d}.{'MP4' if primary else 'LRV'}",
                 32 if primary else 12, "1791919163", "Tue, 13 Oct 2026 19:19:28 GMT",
                 (gumi if gumi is not None else f"gumi-{index}") if primary else "", primary,
                 "" if primary else f"gumi-{index}", device_name='HERO9')


def replace(asset, **changes):
    data = asset.to_dict()
    metadata = dict(data["metadata"])
    for key in ("gumi", "primary", "parent_gumi"):
        if key in changes:
            metadata[key] = changes.pop(key)
    data.update(changes)
    return Asset(data["serial"], data["path"], data["size"], data["created"], data["modified"],
                 metadata["gumi"], metadata["primary"], metadata["parent_gumi"], data["device_name"])


class FakeSource:
    deletion_candidates = staticmethod(GoProSource.deletion_candidates)
    deletion_effects = staticmethod(GoProSource.deletion_effects)
    keep_alive_interval_seconds = GoProSource.keep_alive_interval_seconds
    groups = staticmethod(GoProSource.groups)
    reconcile = staticmethod(GoProSource.reconcile)
    idle_reason = staticmethod(GoProSource.idle_reason)
    def __init__(self, assets):
        self.assets = {a.path: a for a in assets}
        self.online = True
        self.busy = False
        self.downloads = []
        self.deletes = []
        self.snapshot_calls = 0
        self.on_snapshot = None
        self.on_delete = None
        self.fail_download = False
        self.fail_delete = False
        self.keep_sidecar = False
        self.lose_delete_ack = False
        self.keep_alive_calls = 0

    def identify(self):
        if not self.online:
            raise SourceError("相机不可访问")
        return {"serial_number": "C0000000000123", "model_name": "HERO9 Black"}

    def ensure_idle(self):
        if self.busy:
            raise SourceError("相机正在录制")

    def keep_alive(self):
        self.keep_alive_calls += 1

    def snapshot(self):
        self.identify()
        self.ensure_idle()
        self.snapshot_calls += 1
        if self.on_snapshot:
            self.on_snapshot(self.snapshot_calls)
        return sorted(self.assets.values(), key=lambda a: a.path)

    def paths(self):
        self.identify()
        return set(self.assets)

    @staticmethod
    def payload(asset):
        return (asset.path + asset.metadata["gumi"]).encode()[:asset.size].ljust(asset.size, b"x")

    def download(self, asset, path, progress):
        self.identify()
        self.ensure_idle()
        if self.assets.get(asset.path) != asset:
            raise SourceError("素材已变化")
        self.downloads.append(asset.path)
        data = self.payload(asset)
        Path(path).write_bytes(data)
        if self.fail_download:
            Path(path).write_bytes(b"partial")
            raise SourceError("下载中断")
        progress(asset.size, asset.size)
        return __import__("hashlib").sha256(data).hexdigest()

    def delete(self, asset):
        self.identify()
        if self.assets.get(asset.path) != asset or not asset.metadata["gumi"]:
            raise SourceError("素材身份变化")
        if self.on_delete:
            self.on_delete(asset)
        if self.fail_delete:
            raise SourceError("删除失败")
        self.deletes.append(asset.path)
        del self.assets[asset.path]
        if not self.keep_sidecar:
            for path, other in list(self.assets.items()):
                if other.metadata["parent_gumi"] == asset.metadata["gumi"]:
                    del self.assets[path]
        if self.lose_delete_ack:
            raise SourceError("删除响应丢失")


class FakeDestination:
    def __init__(self):
        self.objects = {}
        self.identities = {}
        self.sequence = 0
        self.deleted = []
        self.online = True
        self.uploads = []
        self.publishes = []
        self.fail_upload = False
        self.lose_publish_ack = False
        self.corrupt_size = False

    def check(self):
        if not self.online:
            raise DestinationError("目的地离线")
        return {"target": "destination:media/media"}

    def stat(self, path):
        self.check()
        if path not in self.objects:
            return None
        return {"is_directory": False, "size": len(self.objects[path])}

    def list_names(self, parent):
        self.check()
        return [PurePosixPath(path).name for path in self.objects if str(PurePosixPath(path).parent) == parent]

    def upload(self, local, temporary, progress, created, staged, content_sha256=None):
        self.check()
        self.uploads.append(temporary)
        self.sequence += 1
        self.identities[temporary] = {"inode": self.sequence}
        self.objects[temporary] = b""
        created(self.identities[temporary])
        if self.fail_upload:
            self.objects[temporary] = b"partial"
            raise DestinationError("上传失败")
        data = Path(local).read_bytes()
        self.objects[temporary] = data[:-1] if self.corrupt_size else data
        progress(len(data), len(data))
        if self.corrupt_size:
            raise DestinationError("上传大小不一致")
        if content_sha256 is not None and __import__("hashlib").sha256(data).hexdigest() != content_sha256:
            raise LocalCacheChanged("上传读取的缓存与导入校验不一致，已停止并保留缓存")
        staged(self.identities[temporary])

    def file_identity(self, path):
        self.check()
        return self.identities.get(path) if path in self.objects else None

    def delete_owned(self, path, expected):
        self.check()
        if path not in self.objects:
            return
        if not expected or self.identities.get(path) != expected:
            raise DestinationError("临时文件归属不明", code="temporary_identity")
        del self.objects[path]
        self.identities.pop(path, None)
        self.deleted.append(path)

    def redact(self, message):
        return message

    def close(self):
        pass

    def publish(self, temporary, final):
        self.check()
        if any(path.casefold() == final.casefold() for path in self.objects):
            raise NameConflict("不能覆盖已有文件")
        self.publishes.append(final)
        self.objects[final] = self.objects.pop(temporary)
        self.identities[final] = self.identities.pop(temporary)
        if self.lose_publish_ack:
            self.lose_publish_ack = False
            raise DestinationError("改名响应丢失")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(self.temporary.name)
        self.config = configuration()
        self.source = FakeSource([footage(), footage(primary=False)])
        self.destination = FakeDestination()
        self.stop = threading.Event()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def service(self, config=None):
        return Service(config or self.config, self.store, self.source, self.destination, self.stop)

    def cached(self, asset=None):
        asset = asset or footage()
        self.store.batch(self.config.destination_id, [asset])
        row = next(r for r in self.store.files() if r['asset_id']==asset.identity)
        path = self.store.cached_path(row)
        path.write_bytes(FakeSource.payload(asset))
        from media_importer.storage import cache_evidence
        self.store.update(row['id'], state='cached', cache_identity=json.dumps(cache_evidence(path, row['asset_id'], self.store.get_meta('cache_identity'))))
        return self.store.get(row['id'])

    def test_upload_stops_when_cache_differs_from_import_hash(self):
        old = self.source.download
        def download(asset, path, progress):
            digest = old(asset, path, progress)
            file = Path(path)
            data = bytearray(file.read_bytes())
            data[0] ^= 0xff
            file.write_bytes(data)
            return digest
        self.source.download = download
        result = self.service().safe_cycle()
        self.assertFalse(result["complete"])
        self.assertEqual([], self.destination.publishes)
        self.assertTrue(list(self.store.cache.glob("*.bin")))

    def test_full_archive_cleans_cache_and_retains_originals(self):
        result=self.service().safe_cycle()
        self.assertTrue(result['complete']); self.assertEqual(2,len(self.destination.publishes))
        self.assertEqual([],self.source.deletes);self.assertFalse(list(self.store.cache.glob('*.bin')))
        self.service().safe_cycle();self.assertEqual(2,len(self.destination.uploads))

    def test_group_deleted_after_local_import_while_destination_offline(self):
        self.destination.online=False
        result=self.service(configuration(delete_source_after_import=True)).safe_cycle()
        self.assertFalse(result['complete']);self.assertEqual(1,len(self.source.deletes))
        self.assertEqual(2,len(list(self.store.cache.glob('*.bin'))));self.assertFalse(self.source.assets)

    def test_no_delete_until_sidecar_download_is_complete(self):
        old=self.source.download
        def download(asset,*args):
            self.assertEqual([],self.source.deletes)
            return old(asset,*args)
        self.source.download=download;self.destination.online=False
        self.service(configuration(delete_source_after_import=True)).safe_cycle()
        self.assertEqual(1,len(self.source.deletes))

    def test_all_video_chapters_import_before_deleting_last_chapter_first(self):
        first, first_proxy = footage(), footage(primary=False)
        last = replace(footage(2), path='100GOPRO/GH020001.MP4')
        last_proxy = replace(footage(2, primary=False), path='100GOPRO/GL020001.LRV')
        self.source = FakeSource([first, first_proxy, last, last_proxy])
        self.destination.online = False
        def deleting(asset):
            self.assertEqual(4, len(self.source.downloads))
        self.source.on_delete = deleting
        self.service(configuration(delete_source_after_import=True)).safe_cycle()
        self.assertEqual([last.path, first.path], self.source.deletes)
        self.assertFalse(self.source.assets)

    def test_old_individual_groups_cannot_delete_incomplete_recording(self):
        first, proxy = footage(), footage(primary=False)
        last = replace(footage(2), path='100GOPRO/GH020001.MP4')
        last_proxy = replace(footage(2, primary=False), path='100GOPRO/GL020001.LRV')
        self.source = FakeSource([first, proxy, last, last_proxy])
        for asset in (first, proxy):
            self.cached(asset)
        self.store.batch(self.config.destination_id, [last, last_proxy])
        # Existing installations recorded each chapter as a separate group.
        for pair in ((first, proxy), (last, last_proxy)):
            self.store.save_groups(self.config.destination_id, self.source.groups(list(pair)))
        self.store.save_groups(self.config.destination_id, self.source.groups(list(self.source.assets.values())))
        self.service(configuration(delete_source_after_import=True)).cleanup_groups(set())
        self.assertFalse(self.source.deletes)
        self.assertEqual(4, len(self.source.assets))

    def test_missing_member_or_identity_prevents_delete(self):
        self.source.fail_download=True
        self.service(configuration(delete_source_after_import=True)).safe_cycle()
        self.assertEqual([],self.source.deletes);self.assertFalse(list(self.store.cache.glob('*.download')))
        self.source.fail_download=False
        self.source.assets={a.path:a for a in [footage(gumi=''),footage(primary=False)]}
        self.service(configuration(delete_source_after_import=True)).safe_cycle()
        self.assertEqual([],self.source.deletes)

    def test_delete_failure_does_not_block_upload(self):
        self.source.fail_delete=True
        result=self.service(configuration(delete_source_after_import=True)).safe_cycle()
        self.assertTrue(result['complete']);self.assertTrue(any(r['source_cleanup_error'] for r in self.store.files()))

    def test_lost_delete_ack_and_remaining_sidecar_are_not_reimported(self):
        self.source.lose_delete_ack=True;self.source.keep_sidecar=True
        service=self.service(configuration(delete_source_after_import=True))
        self.assertTrue(service.safe_cycle()['complete']);count=len(self.source.downloads)
        # Simulate the camera's orphan sidecar losing its parent metadata after deletion.
        for path,asset in list(self.source.assets.items()):
            self.source.assets[path]=replace(asset,parent_gumi='',created='')
        service.safe_cycle();self.assertEqual(count,len(self.source.downloads))
        self.assertEqual(2,len(self.store.files()))

    def test_upload_failure_removes_only_remote_partial(self):
        row=self.cached();self.destination.fail_upload=True
        with self.assertRaises(DestinationError):self.service().upload_cached(row)
        self.assertTrue(self.store.cached_path(row).exists());self.assertFalse(self.destination.objects)
        self.assertEqual(0,len(self.store.temporary_items()))
        self.destination.fail_upload=False;self.service().upload_cached(self.store.get(row['id']))
        self.assertEqual(2,len(self.destination.uploads));self.assertNotEqual(*self.destination.uploads)

    def test_foreign_partial_is_not_deleted(self):
        row=self.cached();self.destination.objects['foreign']=b'x';self.destination.identities['foreign']={'inode':9}
        self.store.queue_temporary(row,'remote','foreign',{'inode':2})
        self.service().clean_temporaries();self.assertIn('foreign',self.destination.objects)
        self.assertEqual('blocked',self.store.temporary_items()[0]['state'])

    def test_offline_cleanup_waits_and_runs_with_sync_disabled(self):
        row=self.cached();self.destination.objects['own']=b'partial';self.destination.identities['own']={'inode':1}
        self.store.queue_temporary(row,'remote','own',{'inode':1});self.destination.online=False
        service=self.service(configuration(enabled=False));service.clean_temporaries()
        self.assertEqual('pending',self.store.temporary_items()[0]['state'])
        self.destination.online=True;service.clean_temporaries()
        self.assertFalse(self.destination.objects);self.assertTrue(self.store.cached_path(row).exists())

    def test_repeated_interruption_cleans_each_new_local_attempt(self):
        asset = footage()
        self.store.batch(self.config.destination_id, [asset])
        row = self.store.files()[0]
        service = self.service()
        self.source.fail_download = True
        for attempt in range(2):
            with self.assertRaises(SourceError):
                service.import_asset(self.store.get(row['id']))
            self.assertFalse(self.store.cached_path(row).with_suffix('.download').exists())
            self.assertFalse(self.store.temporary_items())
        self.source.fail_download = False
        service.import_asset(self.store.get(row['id']))
        self.assertEqual('cached', self.store.get(row['id'])['state'])
        self.assertEqual(3, len(self.source.downloads))
        self.assertFalse(self.source.deletes)

    def test_import_flushes_closed_download_before_recording_cache_identity(self):
        from media_importer.storage import cache_evidence
        self.store.batch(self.config.destination_id, [footage()])
        row = self.store.files()[0]
        with patch('media_importer.service.os.fsync', wraps=os.fsync) as flush:
            def checked(*args):
                self.assertGreater(flush.call_count, 0)
                return cache_evidence(*args)
            with patch('media_importer.service.cache_evidence', side_effect=checked):
                self.service().import_asset(row)
        self.assertTrue(self.service().cache_valid(self.store.get(row['id'])))

    def test_import_records_final_cache_timestamp_after_rename(self):
        self.store.batch(self.config.destination_id, [footage()])
        row = self.store.files()[0]
        replace = os.replace
        def rename(source, destination):
            replace(source, destination)
            previous = Path(destination).stat()
            os.utime(destination, ns=(previous.st_atime_ns, previous.st_mtime_ns + 15_000_000_000))
        with patch('media_importer.service.os.replace', side_effect=rename):
            self.service().import_asset(row)
        self.assertTrue(self.service().cache_valid(self.store.get(row['id'])))

    def test_changed_content_after_rename_is_preserved_without_import_commit(self):
        self.store.batch(self.config.destination_id, [footage()])
        row = self.store.files()[0]
        replace = os.replace
        def rename(source, destination):
            replace(source, destination)
            Path(destination).write_bytes(b'z' * footage().size)
        with patch('media_importer.service.os.replace', side_effect=rename):
            with self.assertRaises(CacheUnavailable):
                self.service().import_asset(row)
        self.assertEqual('pending', self.store.get(row['id'])['state'])
        self.assertTrue(self.store.cached_path(row).exists())
        self.assertFalse(self.source.deletes)

    def test_requeued_foreign_temporary_stays_blocked(self):
        row = self.cached()
        self.destination.objects['foreign'] = b'x'
        self.destination.identities['foreign'] = {'inode': 9}
        self.store.queue_temporary(row, 'remote', 'foreign', {'inode': 2})
        self.service().clean_temporaries()
        self.store.queue_temporary(row, 'remote', 'foreign', {'inode': 2})
        self.assertEqual('blocked', self.store.temporary_items()[0]['state'])
        self.assertIn('foreign', self.destination.objects)

    def test_recover_half_upload_and_local_commit_after_restart(self):
        row=self.cached();self.destination.objects['partial']=b'x';self.destination.identities['partial']={'inode':1}
        self.store.update(row['id'],temp_path='partial',remote_identity=json.dumps({'inode':1}))
        service=self.service(configuration(enabled=False));service.recover_interrupted();service.clean_temporaries()
        self.assertFalse(self.destination.objects);self.assertTrue(self.store.cached_path(row).exists())
        self.store.update(row['id'],state='pending')
        self.store.cached_path(row).rename(self.store.cached_path(row).with_suffix('.download'))
        service.recover_interrupted();self.assertEqual('cached',self.store.get(row['id'])['state'])
        self.assertTrue(self.store.cached_path(row).exists())

    def test_completed_remote_survives_stop_and_publish_ack_loss(self):
        row=self.cached();self.destination.lose_publish_ack=True
        with self.assertRaises(DestinationError):self.service().upload_cached(row)
        self.service().recover_interrupted();self.service().upload_cached(self.store.get(row['id']))
        self.assertEqual(1,len(self.destination.uploads));self.assertEqual('uploaded',self.store.get(row['id'])['state'])

    def test_same_name_new_material_gets_suffix(self):
        self.source=FakeSource([footage()]);self.service().safe_cycle()
        self.source.assets={footage(gumi='new').path:footage(gumi='new')}
        self.service().safe_cycle();self.assertEqual(2,len(self.destination.publishes))
        self.assertTrue(any(x.endswith('_1.MP4') for x in self.destination.publishes))

    def test_master_off_preserves_complete_cache_and_source(self):
        row=self.cached()
        result=self.service(configuration(enabled=False,delete_source_after_import=True)).safe_cycle()
        self.assertEqual('paused',result['reason']);self.assertEqual([],self.destination.uploads)
        self.assertEqual([],self.source.deletes);self.assertTrue(self.store.cached_path(row).exists())

    def test_source_unplugged_uploads_existing_complete_cache(self):
        row=self.cached();self.source.online=False
        self.service().safe_cycle();self.assertEqual('uploaded',self.store.get(row['id'])['state'])

    def test_no_fallback_when_cache_volume_unavailable(self):
        row=self.cached()
        with patch('media_importer.service.require_cache',side_effect=CacheUnavailable('missing T7')):
            with self.assertRaises(CacheUnavailable):self.service().upload_cached(row)
        self.assertEqual('cached',self.store.get(row['id'])['state']);self.assertFalse(self.destination.uploads)

    def test_cache_failure_after_archive_never_reuploads(self):
        row=self.cached();path=self.store.cached_path(row);original=Path.unlink
        def unlink(target,*args,**kwargs):
            if target==path:raise OSError('disk unavailable')
            return original(target,*args,**kwargs)
        with patch.object(Path,'unlink',unlink):self.service().upload_cached(row)
        saved=self.store.get(row['id']);self.assertEqual('uploaded',saved['state']);self.assertTrue(saved['cache_cleanup_error'])
        self.service().clean_cache(saved);self.assertFalse(path.exists());self.assertEqual(1,len(self.destination.uploads))

    def test_low_disk_and_bad_source_state_preserve_originals(self):
        with patch('media_importer.service.shutil.disk_usage',return_value=type('D',(),{'free':0})()):
            self.service().safe_cycle()
        self.assertFalse(self.source.downloads);self.assertFalse(self.source.deletes)
        self.source.busy=True;self.service().safe_cycle();self.assertFalse(self.source.downloads)

    def test_one_failed_import_does_not_block_other_files(self):
        original=self.source.download
        def download(asset,*args):
            if asset.metadata['primary']:raise SourceError('file failure')
            return original(asset,*args)
        self.source.download=download
        self.assertFalse(self.service().safe_cycle()['complete'])
        self.assertEqual(1,len(self.destination.publishes))

    def test_parallel_import_and_upload(self):
        self.source=FakeSource([footage(1),footage(2)])
        upload_started=threading.Event();second_started=threading.Event()
        upload=self.destination.upload;download=self.source.download
        def sending(*args, **kwargs):
            upload_started.set();self.assertTrue(second_started.wait(3));return upload(*args, **kwargs)
        def importing(asset,*args):
            if asset.identity==footage(2).identity:
                self.assertTrue(upload_started.wait(3));second_started.set()
            return download(asset,*args)
        self.destination.upload=sending;self.source.download=importing
        self.assertTrue(self.service().safe_cycle()['complete'])

    def test_cache_cleanup_waits_for_source_delete_verification(self):
        row=self.cached();self.store.update(row['id'],state='uploaded')
        service=self.service();attempted=threading.Event();finished=threading.Event()
        def clean():
            db=Store(self.store.directory)
            child=Service(self.config,db,self.source,self.destination,self.stop);child.cache_guard=service.cache_guard
            attempted.set();child.clean_cache(db.get(row['id']));finished.set();db.close()
        with service.cache_guard:
            worker=threading.Thread(target=clean);worker.start();self.assertTrue(attempted.wait(1))
            self.assertFalse(finished.wait(.05));self.assertTrue(self.store.cached_path(row).exists())
        worker.join(2);self.assertTrue(finished.is_set())

    def test_deleted_source_and_missing_cache_report_loss(self):
        row=self.cached();self.store.update(row['id'],cleanup='deleted');self.store.cached_path(row).unlink()
        with self.assertRaises(CacheUnavailable):self.service().upload_cached(self.store.get(row['id']))
        self.assertIn('人工恢复',self.store.get(row['id'])['error']);self.assertFalse(self.destination.uploads)

    def test_retry_clears_previous_error_before_reporting_progress(self):
        row=self.cached()
        self.store.update(row['id'],error='previous failure',error_stage='upload')
        service=self.service();service.stage='upload'
        self.store.stage_status('upload',phase='failed',issue={'code':'permission'})
        upload=self.destination.upload
        def sending(*args, **kwargs):
            self.assertIsNone(self.store.get(row['id'])['error'])
            self.assertIsNone(self.store.get_meta('status')['stages']['upload']['issue'])
            return upload(*args, **kwargs)
        self.destination.upload=sending
        service.upload_cached(self.store.get(row['id']))

    def test_cleanup_removes_only_associated_appledouble_metadata(self):
        row=self.cached();partial=self.store.cached_path(row).with_suffix('.download')
        partial.write_bytes(b'partial')
        metadata=partial.with_name('._'+partial.name)
        metadata.write_bytes(bytes.fromhex('00051607')+b'file metadata')
        unrelated=self.store.cache/'._unrelated';unrelated.write_bytes(b'keep')
        self.store.queue_temporary(row,'local',partial.name)
        self.service(configuration(enabled=False)).clean_temporaries()
        self.assertFalse(partial.exists());self.assertFalse(metadata.exists())
        self.assertTrue(unrelated.exists());self.assertTrue(self.store.cached_path(row).exists())

    def test_new_sidecar_between_cleanup_checks_prevents_delete(self):
        for asset in self.source.assets.values(): self.cached(asset)
        self.store.save_groups(self.config.destination_id,self.source.groups(list(self.source.assets.values())))
        def appeared(call):
            if call==2:
                extra=replace(footage(primary=False),path='100GOPRO/GL010001.THM')
                self.source.assets[extra.path]=extra
        self.source.on_snapshot=appeared
        self.service(configuration(delete_source_after_import=True)).cleanup_groups(set())
        self.assertFalse(self.source.deletes)
        self.assertTrue(any(r['source_cleanup_error'] for r in self.store.files()))

    def test_source_identity_changed_after_import_prevents_delete(self):
        for asset in self.source.assets.values(): self.cached(asset)
        self.store.save_groups(self.config.destination_id,self.source.groups(list(self.source.assets.values())))
        changed=footage(gumi='replacement')
        self.source.assets[changed.path]=changed
        self.service(configuration(delete_source_after_import=True)).cleanup_groups(set())
        self.assertFalse(self.source.deletes)
        self.assertTrue(any(r['source_cleanup_error'] for r in self.store.files()))

    def test_equal_size_changed_cache_does_not_report_import_complete(self):
        self.source=FakeSource([footage()]);row=self.cached()
        path=self.store.cached_path(row);path.unlink();path.write_bytes(b'z'*footage().size)
        self.assertFalse(self.service().verify_imports({'assets':[footage()],'batch_id':self.store.get_meta('last_batch')}))

    def test_macos_removing_metadata_concurrently_is_successful_cleanup(self):
        row=self.cached();partial=self.store.cached_path(row).with_suffix('.download')
        partial.write_bytes(b'partial');metadata=partial.with_name('._'+partial.name)
        metadata.write_bytes(bytes.fromhex('00051607'))
        original=Path.open
        def metadata_disappeared(path,*args,**kwargs):
            if path==metadata:path.unlink(missing_ok=True)
            return original(path,*args,**kwargs)
        self.store.queue_temporary(row,'local',partial.name)
        with patch.object(Path,'open',metadata_disappeared):
            self.service(configuration(enabled=False)).clean_temporaries()
        self.assertFalse(self.store.temporary_items());self.assertTrue(self.store.cached_path(row).exists())
