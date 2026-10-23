import copy,json,os,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from media_importer.config import Config,ConfigError,DEFAULTS,preferences
from media_importer.settings import Settings,Conflict
from media_importer.store import Store
from media_importer.runtime import Control
from media_importer.service import Service
from media_importer.status import snapshot
from media_importer.model import now, Cancelled
from test_bot import ENV
from test_service import configuration,footage,FakeSource,FakeDestination

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'data'
        self.env=patch.dict(os.environ,ENV,clear=True);self.env.start()
        self.path=self.root/'settings.json';self.path.write_text(json.dumps(preferences(configuration().values)))
        self.settings=Settings(self.path);self.store=Store.create(self.data);self.stop=threading.Event()

    def tearDown(self):self.store.close();self.env.stop();self.tmp.cleanup()

    def test_single_schema_rejects_old_switches_and_embedded_connections(self):
        raw=preferences(configuration(enabled=False).values)
        self.assertFalse(Config.from_dict(raw).backup['enabled'])
        self.assertFalse(Config.from_dict(raw).backup['delete_source_after_import'])
        for key in ('automatic','import_paused','cleanup_cache_after_upload','delete_source_after_upload'):
            bad=copy.deepcopy(raw);bad['backup'][key]=True
            with self.assertRaises(ConfigError):Config.from_dict(bad)
        raw['source']={}
        with self.assertRaises(ConfigError):Config.from_dict(raw)

    def test_notifications_and_delete_switch_do_not_recreate_transfer_clients(self):
        before=self.settings.token()[1]
        self.settings.update_preferences({'notifications':{'enabled':True},'backup':{'delete_source_after_import':True}})
        self.assertEqual(before,self.settings.token()[1])

    def test_saved_switch_survives_a_new_runtime_with_default_sync_off(self):
        self.assertFalse(Config.from_dict(preferences(configuration(enabled=False).values)).backup['enabled'])
        for enabled in (True, False):
            self.settings.set_enabled(enabled)
            restarted = Control(Settings(self.path), threading.Event())
            self.assertEqual(enabled, restarted.adopt().backup['enabled'])
            self.assertEqual(enabled, Config.load(self.path).backup['enabled'])
        self.assertFalse(Config.load(self.path).backup['delete_source_after_import'])

    def test_master_stop_interrupts_both_parallel_workers(self):
        control=Control(self.settings,self.stop);config=control.adopt()
        source=FakeSource([footage(1),footage(2)]);destination=FakeDestination()
        upload_started=threading.Event();second_started=threading.Event();release=threading.Event();outcome={}
        download=source.download;upload=destination.upload
        def sending(*args, **kwargs):
            upload_started.set();release.wait(3);return upload(*args, **kwargs)
        def importing(asset,*args):
            if asset.identity==footage(2).identity:
                upload_started.wait(3);second_started.set();release.wait(3)
            return download(asset,*args)
        source.download=importing;destination.upload=sending
        def run():
            db=Store.create(self.data)
            try:outcome['result']=Service(config,db,source,destination,self.stop,control).safe_cycle()
            except Cancelled:outcome['result']={'reason':'paused'}
            finally:db.close()
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(second_started.wait(3));self.settings.set_enabled(False);control.refresh();release.set();thread.join(4)
            self.assertFalse(thread.is_alive());self.assertEqual('paused',outcome['result']['reason'])
            self.assertFalse(list(self.store.cache.glob('*.download')));self.assertEqual([],source.deletes)
            self.assertTrue(list(self.store.cache.glob('*.bin')));self.assertFalse(destination.objects)
        finally:release.set();thread.join(4)

    def test_paused_status_resets_inflight_and_preserves_archive_count(self):
        batch=self.store.batch(Config.load(self.path).destination_id,[footage()]);row=self.store.files()[0]
        self.store.set_meta('service',{'running':True});self.store.status(worker_heartbeat_at=now(),batch_id=batch)
        self.store.stage_status('upload',phase='paused',current_file_id=row['id'],current_bytes=16)
        self.settings.set_enabled(False);view=snapshot(self.data,self.settings)
        self.assertEqual(0,view['stages']['upload']['current_bytes']);self.assertEqual(0,view['batch']['in_flight_bytes'])
        self.store.update(row['id'],state='uploaded');self.assertEqual(1,snapshot(self.data,self.settings)['batch']['completed_files'])

    def test_stale_health_and_not_scanned_are_unknown(self):
        view=snapshot(self.data,self.settings)
        self.assertFalse(view['batch']['scanned']);self.assertIsNone(view['health']['source']['online'])
        self.assertFalse(view['unplug']['ready'])
