"""Selected cleanup never widens deletion, and recorded status does no remote IO."""
import asyncio,json,unittest
from unittest.mock import patch
from media_importer import camera_operations as ops, camera_library, camera_web
from media_importer.model import ConfigError
import test_camera_operations as cleanup_fixtures
from test_camera_workspace import Fixture
from test_service import footage, replace, FakeSource


class SelectedCleanupTests(unittest.TestCase):
    setUp = cleanup_fixtures.CameraOperationTests.setUp
    tearDown = cleanup_fixtures.CameraOperationTests.tearDown
    record = cleanup_fixtures.CameraOperationTests.record
    execute = cleanup_fixtures.CameraOperationTests.execute
    def review(self, selected, mode='archived'):
        library=camera_library.Library()
        library.session='session-a'; library.catalog='a'*32
        import time
        library.observed_at=time.monotonic()
        library.choices={g['members'][0].identity:g for g in self.source.selection_groups(list(self.source.assets.values()))}
        self.counter+=1
        return ops.review_library(self.store,self.settings,library,self.owner,'session-a',library.catalog,
                                  selected,str(self.counter),mode)

    def ready(self, op):
        self.assertTrue(ops.process_one(self.service,self.settings))
        return ops.get(self.store.db,op['id'])

    def test_selected_only_and_idempotent_confirmation(self):
        second=footage(2);self.source.assets[second.path]=second
        self.record(excluded=True)
        op=self.ready(self.review([footage().identity]))
        plan=json.loads(op['plan']);self.assertEqual(2,json.loads(op['result'])['eligible_files'])
        self.assertEqual('camera_workspace',plan['origin'])
        self.assertEqual({footage().path,footage(primary=False).path},{a['path'] for g in plan['groups'] for a in g['members']})
        result=self.execute(op)
        self.assertEqual(2,result['deleted']);self.assertIn(second.path,self.source.assets)
        self.assertEqual(self.before,self.path.read_bytes())
        ops.confirm(self.store,self.settings,op['id'],self.owner)
        self.assertFalse(ops.process_one(self.service,self.settings))
        self.assertEqual(1,len(self.source.deletes))

    def test_independent_backup_does_not_authorize_expanding_cleanup_to_other_mp4(self):
        second=replace(footage(1),path='100GOPRO/GH020001.MP4',gumi='chapter-2')
        self.source.assets[second.path]=second;self.record()
        op=self.ready(self.review([footage().identity]))
        result=json.loads(op['result']);self.assertEqual(0,result['eligible_files'])
        self.assertIn('未全部选择',result['retained'][0]['reason'])
        self.assertNotIn(second.path,result['retained'][0]['paths'])
        with self.assertRaises(ConfigError):self.execute(op)
        self.assertFalse(self.source.deletes)

    def test_missing_copy_and_new_sidecar_never_deleted(self):
        op=self.ready(self.review([footage().identity]))
        self.assertEqual(0,json.loads(op['result'])['eligible_files'])
        self.assertFalse(self.source.deletes)
        self.record();op=self.ready(self.review([footage().identity]))
        extra=replace(footage(primary=False),path='100GOPRO/GH010001.THM')
        self.source.assets[extra.path]=extra
        result=self.execute(op);self.assertEqual(0,result['deleted']);self.assertFalse(self.source.deletes)

    def test_changed_identity_between_selection_and_review_rejects(self):
        op=self.review([footage().identity]);changed=replace(footage(),gumi='replaced')
        self.source.assets[changed.path]=changed
        op=self.ready(op);self.assertEqual('done',op['state']);self.assertIn('变化',json.loads(op['result'])['error'])
        self.assertFalse(self.source.deletes)

    def test_recorded_states_survive_retirement_and_only_match_destination_and_identity(self):
        self.record(excluded=True)
        with self.store.db:
            for row in self.service.rows():
                asset=self.store.asset(row)
                self.store.db.execute('INSERT INTO source_index VALUES (?,?,?,?,?,?,?,?,?)',
                    (self.service.destination_id,asset.identity,asset.source_type,asset.serial,row['state'],row['role'],asset.size,row['remote_path'],row['remote_identity']))
            self.store.db.execute('DELETE FROM files')
        with patch.object(self.remote,'stat',side_effect=AssertionError('Status must not access SMB')):
            result=camera_library.backup_states(self.store,self.service.destination_id,[footage(),footage(2)])
            self.assertEqual('archived',result[footage().identity]['state'])
            self.assertEqual('pending',result[footage(2).identity]['state'])
            self.assertEqual('pending',camera_library.backup_states(self.store,'other',[footage()])[footage().identity]['state'])
            changed=replace(footage(),gumi='new')
            self.assertEqual('pending',camera_library.backup_states(self.store,self.service.destination_id,[changed])[changed.identity]['state'])

    def test_status_failure_preserves_archived_fact_and_imported_without_copy_claim(self):
        self.record(archived=False)
        row=self.service.rows()[0]
        asset=self.store.asset(row)
        view=lambda:camera_library.backup_states(self.store,self.service.destination_id,[asset])[asset.identity]
        self.assertEqual('imported',view()['state'])
        self.store.update(row['id'],error='failed');self.assertEqual('error',view()['state'])
        self.store.update(row['id'],state='uploaded',cache_cleanup_error='cache failed')
        self.assertEqual('archived',view()['state']);self.assertTrue(view()['error'])

    def test_importing_status_expires_without_marking_file_complete(self):
        self.record(archived=False)
        row=self.service.rows()[0];asset=self.store.asset(row)
        self.store.update(row['id'],state='discovered')
        from media_importer.model import now
        self.store.stage_status('import',phase='downloading',current_file_id=row['id'],progress_updated_at=now())
        view=lambda:camera_library.backup_states(self.store,self.service.destination_id,[asset])[asset.identity]
        self.assertEqual('importing',view()['state'])
        self.store.stage_status('import',progress_updated_at='2000-01-01T00:00:00+00:00')
        self.assertEqual('pending',view()['state'])


class WorkspaceCleanupTests(Fixture,unittest.IsolatedAsyncioTestCase):
    async def test_review_defaults_and_owner_scoping_release_browser_without_delete(self):
        listing=self.read('list')
        request={'action':'cleanup_review','session_id':self.connection['id'],'catalog':listing['catalog'],
                 'identifiers':[listing['files'][0]['id']],'nonce':'b'*32,'mode':'archived'}
        first=await self.workspace.dispatch(self.token,request)
        again=await self.workspace.dispatch(self.token,request)
        self.assertEqual(first,again);self.assertEqual('archived',first['mode'])
        self.assertFalse(camera_library.active(self.store));self.assertFalse(self.source.deletes)
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
        op=ops.get(self.store.db,first['id']);self.assertEqual(2,len(json.loads(op['plan'])['selection']))
        with self.store.db:self.store.db.execute('UPDATE camera_operations SET owner=? WHERE id=?',('other',first['id']))
        for action in ('cleanup_status','cleanup_confirm','cleanup_cancel','cleanup_verify'):
            with self.assertRaises(ConfigError):await self.workspace.dispatch(self.token,{'action':action,'id':first['id']})

    async def test_direct_delete_or_forged_path_not_accepted(self):
        for request in ({'action':'delete','path':'100GOPRO/any.MP4'},
                        {'action':'cleanup_confirm','id':'a'*32},
                        {'action':'cleanup_review','mode':'all'}):
            with self.assertRaises(ConfigError):await self.workspace.dispatch(self.token,request)
        self.assertFalse(self.source.deletes)

    async def test_nonce_cannot_be_reused_for_a_different_cleanup_scope(self):
        listing=self.read('list')
        request={'action':'cleanup_review','session_id':self.connection['id'],'catalog':listing['catalog'],
                 'identifiers':[listing['files'][0]['id']],'nonce':'d'*32,'mode':'archived'}
        await self.workspace.dispatch(self.token,request)
        for changed in ({'mode':'imported'},{'identifiers':[]},{'catalog':'f'*32}):
            with self.assertRaises(ConfigError):await self.workspace.dispatch(self.token,{**request,**changed})
