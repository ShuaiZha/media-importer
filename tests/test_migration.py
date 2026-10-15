import contextlib,copy,importlib.util,io,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from media_importer.config import DEFAULTS
from media_importer.store import Store
from test_service import footage

spec=importlib.util.spec_from_file_location('migration',Path(__file__).resolve().parents[1]/'scripts/migrate_v010.py')
migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'data';self.data.mkdir();(self.data/'cache').mkdir()
        self.config=self.root/'settings.json';raw=copy.deepcopy(DEFAULTS);raw['version']=3
        raw['backup'].update(enabled=True,delete_source_after_upload=True,automatic=True,cleanup_cache_after_upload=True,import_paused=True)
        raw['limits']['max_cache_gib']=63;self.config.write_text(json.dumps(raw))
        self.env=patch.dict(os.environ,{'MEDIA_IMPORTER_REQUIRE_CACHE_MOUNT':'0'},clear=True);self.env.start()
        self.asset=footage();self.path=self.data/'cache'/'file.bin';self.path.write_bytes(b'x'*self.asset.size)
        (self.data/'cache'/'file.download').write_bytes(b'partial');(self.data/'cache'/'unrelated.txt').write_text('keep')
        with sqlite3.connect(self.data/'state.sqlite3') as db:
            db.executescript('''
            PRAGMA user_version=4;
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE files(id TEXT PRIMARY KEY,destination TEXT NOT NULL,asset_id TEXT NOT NULL,asset TEXT NOT NULL,
            state TEXT NOT NULL,remote_path TEXT,temp_path TEXT,cleanup TEXT NOT NULL,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
            upload_offset INTEGER NOT NULL DEFAULT 0,upload_identity TEXT,upload_source TEXT,error_stage TEXT,attempts INTEGER NOT NULL DEFAULT 0,cache_cleanup_error TEXT);
            CREATE TABLE batches(id TEXT PRIMARY KEY,destination TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE batch_files(batch_id TEXT NOT NULL,file_id TEXT NOT NULL,PRIMARY KEY(batch_id,file_id));
            CREATE TABLE requests(id TEXT);
            CREATE TABLE bot_actions(token TEXT);INSERT INTO bot_actions VALUES ('old');
            CREATE TABLE bot_jobs(id TEXT,state TEXT,error TEXT);INSERT INTO bot_jobs VALUES ('job','queued',NULL);
            ''')
            db.execute("INSERT INTO files (id,destination,asset_id,asset,state,remote_path,temp_path,cleanup,created_at,updated_at,upload_offset,upload_identity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ('file','dest',self.asset.identity,json.dumps(self.asset.to_dict()),'cached','device/original.mp4','device/.old.partial','pending','2026-10-15T01:00:00+00:00','2026-10-15T01:00:00+00:00',8,json.dumps({'inode':99})))

    def tearDown(self):self.env.stop();self.tmp.cleanup()

    def run_migrate(self):
        with contextlib.redirect_stdout(io.StringIO()):migration.migrate(self.config,self.data)

    def test_preserves_complete_data_names_and_settings_discards_offsets(self):
        self.run_migrate();store=Store(self.data)
        try:
            row=store.get('file');self.assertEqual('device/original.mp4',row['remote_path']);self.assertEqual('cached',row['state'])
            self.assertTrue(row['cache_identity']);self.assertNotIn('upload_offset',row)
            pending=store.temporary_items();self.assertEqual({'local','remote'},{x['kind'] for x in pending})
            self.assertEqual({'inode':99},json.loads(next(x for x in pending if x['kind']=='remote')['identity']))
            self.assertTrue(self.path.exists());self.assertTrue((self.data/'cache'/'unrelated.txt').exists())
            self.assertEqual(0,store.db.execute('select count(*) from bot_actions').fetchone()[0])
            self.assertEqual('error',store.db.execute('select state from bot_jobs').fetchone()[0])
        finally:store.close()
        raw=json.loads(self.config.read_text());self.assertEqual(4,raw['version']);self.assertEqual(63,raw['limits']['max_cache_gib'])
        self.assertFalse(raw['backup']['enabled']);self.assertFalse(raw['backup']['delete_source_after_import'])
        self.run_migrate()

    def test_complete_temporary_checkpoint_becomes_staged(self):
        with sqlite3.connect(self.data/'state.sqlite3') as db:db.execute('update files set upload_offset=?',(self.asset.size,))
        self.run_migrate();store=Store(self.data)
        try:
            self.assertEqual('staged',store.get('file')['state']);self.assertFalse([x for x in store.temporary_items() if x['kind']=='remote'])
        finally:store.close()

    def test_uncertain_rename_keeps_identity_and_is_not_deleted(self):
        with sqlite3.connect(self.data/'state.sqlite3') as db:db.execute("update files set state='publishing'")
        self.run_migrate();store=Store(self.data)
        try:
            row=store.get('file');self.assertEqual('publishing',row['state']);self.assertTrue(row['remote_identity'])
            self.assertEqual('device/.old.partial',row['temp_path']);self.assertFalse([x for x in store.temporary_items() if x['kind']=='remote'])
        finally:store.close()
