"""Camera library selection reuses bounded transfers and their copy protection."""
import json
import time
import unittest
from unittest.mock import patch

from media_importer import camera_library, selected_imports
from media_importer.camera_web import backup_snapshot
from media_importer.model import ConfigError
from test_camera_workspace import Fixture
from test_service import footage


class WorkspaceBackupTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def review(self, indices=(0,), key='a', listing=None):
        listing = listing or self.read('list')
        request = {'action': 'backup_review', 'session_id': self.connection['id'],
                   'catalog': listing['catalog'], 'identifiers': [listing['files'][i]['id'] for i in indices],
                   'nonce': key * 32}
        return await self.workspace.dispatch(self.token, request), request

    async def confirm(self, op):
        return await self.workspace.dispatch(self.token, {'action': 'backup_confirm', 'id': op['id'],
                                                         'choice_revision': op['choice_revision']})

    def process(self):
        self.service.config = self.control.adopt()
        self.assertTrue(selected_imports.process_one(self.service, self.settings))

    async def test_single_file_review_uses_library_snapshot_without_transfer_or_preference_change(self):
        self.settings.update_preferences({'backup': {'archive_previews': False, 'delete_source_after_import': True}})
        before = self.path.read_bytes()
        result, _ = await self.review()
        self.assertEqual('ready', result['state'])
        self.assertEqual(1, result['total_files'])
        self.assertEqual(footage().size, result['total_bytes'])
        self.assertEqual([footage().path], [f['path'] for f in result['files']])
        self.assertEqual(1, result['excluded_preview_files'])
        self.assertEqual(1, self.source.snapshot_calls)
        self.assertFalse(self.source.downloads or self.source.deletes or self.store.files())
        self.assertEqual(before, self.path.read_bytes())
        self.assertTrue(camera_library.active(self.store))

    async def test_actual_archive_scope_includes_enabled_companions_but_never_other_mp4(self):
        self.settings.update_preferences({'backup': {'archive_previews': True}})
        result, _ = await self.review()
        self.assertEqual(2, result['total_files'])
        self.assertEqual(footage().size + footage(primary=False).size, result['total_bytes'])
        self.assertEqual({footage().path, footage(primary=False).path}, {f['path'] for f in result['files']})

    async def test_duplicate_review_and_confirm_run_exactly_once_and_keep_originals(self):
        self.settings.update_preferences({'backup': {'archive_previews': False, 'delete_source_after_import': True}})
        before = self.path.read_bytes()
        op, request = await self.review()
        self.assertEqual(op, await self.workspace.dispatch(self.token, request))
        first = await self.confirm(op)
        self.assertEqual(first['id'], (await self.confirm(op))['id'])
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM selected_imports').fetchone()[0])
        self.assertFalse(camera_library.active(self.store))
        self.process()
        result = await self.workspace.dispatch(self.token, {'action': 'backup_status', 'id': op['id']})
        self.assertEqual('done', result['state'])
        self.assertTrue(result['complete'])
        self.assertEqual([100, 100], [p['percent'] for p in result['progress']])
        self.assertEqual(1, len(self.source.downloads))
        self.assertFalse(self.source.deletes)
        self.assertEqual(3, len(self.source.assets))
        self.assertFalse(self.store.files())
        self.assertEqual([], list(self.store.cache.glob('*.bin')))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual('done', (await self.confirm(op))['state'])
        self.assertFalse(selected_imports.pending(self.store.db))

    async def test_stale_catalog_unknown_identity_and_reused_nonce_cannot_change_scope(self):
        old = self.read('list')
        self.read('list')
        with self.assertRaisesRegex(ConfigError, '清单已失效'):
            await self.review(listing=old)
        op, request = await self.review()
        changed = {**request, 'identifiers': [footage(2).identity]}
        with self.assertRaisesRegex(ConfigError, '选择已变化'):
            await self.workspace.dispatch(self.token, changed)
        for identifiers in ([], ['unknown'], [footage().identity] * 2, [True], 'wrong'):
            with self.assertRaises(ConfigError):
                await self.workspace.dispatch(self.token, {**request, 'identifiers': identifiers, 'nonce': 'b' * 32})
        self.assertEqual(1, self.store.db.execute('SELECT count(*) FROM selected_imports').fetchone()[0])
        self.assertFalse(self.store.files() or self.source.deletes)

    async def test_changed_settings_or_connection_prevents_confirmation_without_releasing_reader(self):
        op, _ = await self.review()
        self.settings.update_preferences({'limits': {'max_cache_gib': 52}})
        with self.assertRaises(ConfigError):
            await self.confirm(op)
        self.assertTrue(camera_library.active(self.store))
        second, _ = await self.review(key='b')
        connection = self.store.get_meta('source_connection')
        connection['id'] = 'another-session'
        self.store.set_meta('source_connection', connection)
        with self.assertRaises(ConfigError):
            await self.confirm(second)
        self.assertFalse(self.store.files() or self.source.downloads)

    async def test_full_backup_is_not_implicitly_stopped_and_other_selection_not_overwritten(self):
        self.settings.set_enabled(True)
        op, _ = await self.review()
        with self.assertRaisesRegex(ConfigError, '全量备份'):
            await self.confirm(op)
        self.assertTrue(self.settings.read()['config']['backup']['enabled'])
        self.settings.set_enabled(False)
        other = selected_imports.enqueue(self.store, self.settings, 'feishu-selection', self.bot.scope)
        with self.assertRaisesRegex(ConfigError, '已有选定任务'):
            await self.review(key='b')
        self.assertEqual(other, selected_imports.get(self.store.db, other['id']))

    async def test_cancel_offline_transfer_preserves_cache_and_resume_uses_same_task(self):
        self.settings.update_preferences({'backup': {'archive_previews': False}})
        self.service.destination.online = False
        op, _ = await self.review()
        await self.confirm(op)
        self.process()
        cached = {p.name: p.read_bytes() for p in self.store.cache.glob('*.bin')}
        self.assertTrue(cached)
        await self.workspace.dispatch(self.token, {'action': 'backup_cancel', 'id': op['id']})
        self.process()
        self.assertEqual('paused', selected_imports.get(self.store.db, op['id'])['state'])
        self.assertEqual(cached, {p.name: p.read_bytes() for p in self.store.cache.glob('*.bin')})
        self.service.destination.online = True
        result = await self.workspace.dispatch(self.token, {'action': 'backup_resume', 'id': op['id']})
        self.assertEqual(op['id'], result['id'])
        self.process()
        self.assertEqual('done', selected_imports.get(self.store.db, op['id'])['state'])
        self.assertEqual(1, len(self.source.downloads))
        self.assertFalse(self.source.deletes)

    async def test_cancelled_or_expired_draft_cannot_start(self):
        op, _ = await self.review()
        await self.workspace.dispatch(self.token, {'action': 'backup_cancel', 'id': op['id']})
        with self.assertRaises(ConfigError):
            await self.confirm(op)
        op, _ = await self.review(key='b')
        with self.store.db:
            self.store.db.execute('UPDATE selected_imports SET updated=? WHERE id=?', (time.time() - 301, op['id']))
        with self.assertRaisesRegex(ConfigError, '5 分钟'):
            await self.confirm(op)
        self.assertFalse(self.store.files() or self.source.downloads)

    async def test_status_read_and_owner_checks_do_not_create_or_control_other_tasks(self):
        op, request = await self.review()
        for action in ('backup_confirm', 'backup_cancel', 'backup_resume', 'backup_status'):
            value = {'action': action, 'id': op['id']}
            if action == 'backup_confirm': value['choice_revision'] = op['choice_revision']
            with self.assertRaises(ConfigError):
                await self.workspace.dispatch('f' * 64, value)
            with patch('media_importer.camera_web.authorize', return_value={'owner': 'different-owner'}):
                with self.assertRaises(ConfigError):
                    await self.workspace.dispatch(self.token, value)
        before = self.store.db.total_changes
        self.assertEqual(op['id'], (await self.workspace.dispatch(self.token, {'action': 'status'}))['backup']['id'])
        self.assertEqual(before, self.store.db.total_changes)
        self.assertFalse(self.source.downloads or self.source.deletes)

    async def test_byte_completion_does_not_claim_archive_completion(self):
        op, _ = await self.review()
        row = selected_imports.get(self.store.db, op['id'])
        plan = json.loads(row['plan']); plan['confirmed'] = True
        result = json.loads(row['result'])
        result.update(import_bytes=result['total_bytes'], upload_bytes=result['total_bytes'], imported_files=result['total_files'], uploaded_files=0)
        row.update(state='running', plan=json.dumps(plan), result=json.dumps(result))
        value = backup_snapshot(self.store, row)
        self.assertFalse(value['complete'])
        self.assertEqual(99.9, value['progress'][1]['percent'])
        self.assertEqual('归档确认中', value['progress'][1]['phase'])
