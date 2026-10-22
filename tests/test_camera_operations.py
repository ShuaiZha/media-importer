"""Disposable byte-sized fixtures only: no physical camera or real storage."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from media_importer import camera_operations as operations
from media_importer.config import Config, preferences
from media_importer.model import ConfigError
from media_importer.service import Service
from media_importer.settings import Settings
from media_importer.storage import cache_evidence
from media_importer.store import Store
import test_bot
from test_service import configuration, footage, FakeSource, FakeDestination, replace
from test_source_connection import device


class CameraOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, test_bot.ENV, clear=True)
        self.env.start()
        self.path = self.root / 'settings.json'
        self.path.write_text(json.dumps(preferences(configuration(enabled=False).values)))
        self.store = Store(self.root / 'data')
        self.settings = Settings(self.path, self.store.directory)
        self.config = Config.from_values(self.settings.read()['config'])
        self.source = FakeSource([footage(), footage(primary=False)])
        self.remote = FakeDestination()
        self.service = Service(self.config, self.store, self.source, self.remote, threading.Event())
        self.store.set_meta('source_connection', {
            'id': 'session-a', 'state': 'approved', 'online': True,
            'last_seen': time.time(), 'device': device()})
        self.owner = 'app:owner'
        self.counter = 0
        self.before = self.path.read_bytes()

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def record(self, archived=True, excluded=False):
        assets = list(self.source.assets.values())
        groups = self.source.groups(assets)
        previews = {key for group in groups for key in group['preview_ids']}
        self.store.batch(self.service.destination_id, assets,
                         preview_ids=previews, archive_previews=not excluded)
        self.store.save_groups(self.service.destination_id, groups)
        for row in self.service.rows():
            if row['state'] == 'excluded':
                continue
            asset = self.store.asset(row)
            if archived:
                path = 'device/' + asset.path.split('/')[-1]
                self.remote.objects[path] = self.source.payload(asset)
                self.remote.identities[path] = {'inode': len(self.remote.objects)}
                self.store.update(row['id'], state='uploaded', remote_path=path,
                    remote_identity=json.dumps(self.remote.identities[path]))
            else:
                path = self.store.cached_path(row)
                path.write_bytes(self.source.payload(asset))
                self.store.update(row['id'], state='cached', cache_identity=json.dumps(
                    cache_evidence(path, row['asset_id'], self.store.get_meta('cache_identity'))))

    def request(self, mode='archived', kind='preview'):
        self.counter += 1
        op = operations.enqueue(self.store, self.settings, str(self.counter), self.owner, kind, mode)
        self.assertTrue(operations.process_one(self.service, self.settings))
        return operations.get(self.store.db, op['id'])

    def execute(self, op):
        operations.confirm(self.store, self.settings, op['id'], self.owner)
        self.assertTrue(operations.process_one(self.service, self.settings))
        return json.loads(operations.get(self.store.db, op['id'])['result'])

    def test_default_archived_requires_preview_and_confirmation_with_sync_off(self):
        self.record()
        op = self.request()
        self.assertEqual('ready', op['state'])
        self.assertEqual('archived', op['mode'])
        self.assertEqual(2, json.loads(op['result'])['eligible_files'])
        self.assertFalse(self.source.deletes)
        result = self.execute(op)
        self.assertEqual((2, 0, 0), (result['deleted'], result['retained'], result['unknown']))
        self.assertEqual(1, len(self.source.deletes))  # Adapter accounts for linked LRV.
        self.assertEqual(self.before, self.path.read_bytes())
        self.assertEqual(2, len(self.remote.objects))
        self.assertFalse(self.remote.deleted)
        operations.confirm(self.store, self.settings, op['id'], self.owner)
        self.assertFalse(operations.process_one(self.service, self.settings))
        self.assertEqual(1, len(self.source.deletes))

    def test_imported_mode_keeps_complete_cache_and_works_with_smb_offline(self):
        self.record(archived=False)
        self.remote.online = False
        original = {p.name: p.read_bytes() for p in self.store.cache.glob('*.bin')}
        archived = self.request()
        self.assertEqual(0, json.loads(archived['result'])['eligible_files'])
        op = self.request('imported')
        self.assertEqual(2, json.loads(op['result'])['pending_archive'])
        self.assertEqual(2, self.execute(op)['deleted'])
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.store.cache.glob('*.bin')})
        self.assertEqual(self.before, self.path.read_bytes())
        self.assertEqual({'cached'}, {r['state'] for r in self.service.rows()})
        self.assertEqual({'deleted'}, {r['cleanup'] for r in self.service.rows()})

    def test_camera_status_does_not_change_tasks_settings_or_media(self):
        self.record(archived=False)
        rows = self.store.files()
        op = self.request(kind='inspect')
        data = json.loads(op['result'])
        self.assertEqual(2, data['files'])
        self.assertEqual(1, data['counts']['video']['files'])
        self.assertEqual(1, data['counts']['preview']['files'])
        self.assertEqual(rows, self.store.files())
        self.assertEqual(self.before, self.path.read_bytes())
        self.assertFalse(self.source.deletes or self.source.downloads or self.remote.uploads)

    def test_busy_camera_still_shows_identity_without_claiming_empty_inventory(self):
        self.source.busy = True
        self.source.telemetry = lambda: {'available': True, 'recording': True, 'battery_percent': 80}
        op = self.request(kind='inspect')
        result = json.loads(op['result'])
        self.assertFalse(result['scanned'])
        self.assertTrue(result['identity']['telemetry']['recording'])
        self.assertIn('录制', result['error'])
        op = self.request()
        self.assertEqual('done', op['state'])
        self.assertFalse(self.source.deletes)

    def test_camera_approval_revoked_before_preview_does_not_create_confirmable_plan(self):
        op = operations.enqueue(self.store, self.settings, 'revoked', self.owner, 'preview')
        connection = self.store.get_meta('source_connection')
        connection['state'] = 'ignored'
        self.store.set_meta('source_connection', connection)
        operations.process_one(self.service, self.settings)
        op = operations.get(self.store.db, op['id'])
        self.assertEqual('done', op['state'])
        self.assertIsNone(op['plan'])
        self.assertFalse(self.source.deletes)

    def test_archived_file_identity_changed_after_preview_is_not_deleted(self):
        self.record()
        op = self.request()
        row = self.service.rows()[0]
        self.remote.identities[row['remote_path']] = {'inode': 999}
        result = self.execute(op)
        self.assertEqual(2, result['retained'])
        self.assertFalse(self.source.deletes)

    def test_cache_changed_after_preview_preserves_entire_group(self):
        self.record(archived=False)
        op = self.request('imported')
        path = self.store.cached_path(self.service.rows()[0])
        path.write_bytes(b'x' * path.stat().st_size)
        result = self.execute(op)
        self.assertEqual(2, result['retained'])
        self.assertFalse(self.source.deletes)

    def test_incomplete_associated_file_blocks_primary_deletion(self):
        self.record(archived=False)
        row = next(r for r in self.service.rows() if self.store.asset(r).path.endswith('.LRV'))
        self.store.cached_path(row).unlink()
        self.store.update(row['id'], state='pending')
        op = self.request('imported')
        self.assertEqual(0, json.loads(op['result'])['eligible_files'])
        with self.assertRaises(ConfigError):
            self.execute(op)
        self.assertFalse(self.source.deletes)

    def test_confirmed_excluded_preview_can_be_deleted_with_archived_primary(self):
        self.record(excluded=True)
        self.assertEqual(2, self.execute(self.request())['deleted'])
        self.assertEqual(1, len(self.remote.objects))

    def test_new_files_are_not_added_to_frozen_plan(self):
        self.record()
        op = self.request()
        added = footage(2)
        self.source.assets[added.path] = added
        result = self.execute(op)
        self.assertEqual(2, result['deleted'])
        self.assertEqual([added.path], list(self.source.assets))

    def test_new_associated_file_invalidates_group(self):
        self.source = FakeSource([footage()])
        self.service.source = self.source
        self.record()
        op = self.request()
        added = footage(primary=False)
        self.source.assets[added.path] = added
        self.assertEqual(1, self.execute(op)['retained'])
        self.assertFalse(self.source.deletes)

    def test_replaced_source_identity_blocks_group(self):
        self.record()
        op = self.request()
        changed = replace(footage(), gumi='new-recording')
        self.source.assets[changed.path] = changed
        self.assertEqual(2, self.execute(op)['retained'])
        self.assertFalse(self.source.deletes)

    def test_unknown_preview_association_and_unverified_model_are_retained(self):
        self.source.assets[footage(primary=False).path] = replace(footage(primary=False), parent_gumi='other')
        self.record()
        op = self.request()
        self.assertEqual(0, json.loads(op['result'])['eligible_files'])
        connection = self.store.get_meta('source_connection')
        connection['device']['compatibility']['delete_verified'] = False
        self.store.set_meta('source_connection', connection)
        op = self.request()
        self.assertEqual(0, json.loads(op['result'])['eligible_files'])
        self.assertFalse(self.source.deletes)

    def test_preview_expires_on_config_session_age_or_cancellation(self):
        self.record()
        op = self.request()
        with self.assertRaises(ConfigError):
            operations.confirm(self.store, self.settings, op['id'], 'other-owner')
        with self.store.db:
            self.store.db.execute('UPDATE camera_operations SET updated=? WHERE id=?', (time.time() - 301, op['id']))
        with self.assertRaises(ConfigError):
            self.execute(op)
        op = self.request()
        self.settings.update_preferences({'limits': {'max_cache_gib': 51}})
        with self.assertRaises(ConfigError):
            self.execute(op)
        op = self.request()
        connection = self.store.get_meta('source_connection')
        connection['id'] = 'session-b'
        self.store.set_meta('source_connection', connection)
        with self.assertRaises(ConfigError):
            self.execute(op)
        op = self.request()
        with self.store.db:
            operations.cancel(self.store.db, op['id'], self.owner)
        with self.assertRaises(ConfigError):
            self.execute(op)
        self.assertFalse(self.source.deletes)

    def test_queued_deletion_can_be_cancelled_without_changing_settings(self):
        self.record()
        op = self.request()
        operations.confirm(self.store, self.settings, op['id'], self.owner)
        with self.store.db:
            operations.cancel(self.store.db, owner=self.owner)
        operations.process_one(self.service, self.settings)
        result = json.loads(operations.get(self.store.db, op['id'])['result'])
        self.assertEqual(2, result['retained'])
        self.assertFalse(self.source.deletes)
        self.assertEqual(self.before, self.path.read_bytes())

    def test_cancel_during_deletion_stops_before_next_group(self):
        for a in (footage(2), footage(2, primary=False)):
            self.source.assets[a.path] = a
        self.record()
        op = self.request()
        def cancel_after_first(_):
            with self.store.db:
                operations.cancel(self.store.db, op['id'])
        self.source.on_delete = cancel_after_first
        result = self.execute(op)
        self.assertEqual(2, result['deleted'])
        self.assertEqual(2, result['retained'])
        self.assertEqual(1, len(self.source.deletes))

    def test_cancel_during_final_source_scan_prevents_delete_request(self):
        self.record()
        op = self.request()
        last_scan = self.source.snapshot_calls + 3
        def stop_on_scan(count):
            if count == last_scan:
                with self.store.db:
                    operations.cancel(self.store.db, op['id'])
        self.source.on_snapshot = stop_on_scan
        result = self.execute(op)
        self.assertEqual(2, result['retained'])
        self.assertFalse(self.source.deletes)

    def test_lost_delete_response_is_resolved_by_inventory_not_resent(self):
        self.record()
        self.source.lose_delete_ack = True
        self.assertEqual(2, self.execute(self.request())['deleted'])
        self.assertEqual(1, len(self.source.deletes))

    def test_unknown_result_keeps_intent_and_readonly_recheck(self):
        self.record()
        op = self.request()
        self.source.on_delete = lambda _: setattr(self.source, 'online', False)
        result = self.execute(op)
        self.assertEqual(2, result['unknown'])
        self.assertEqual(1, len(self.source.deletes))
        self.source.online = True
        operations.verify(self.store, op['id'], self.owner)
        operations.process_one(self.service, self.settings)
        result = json.loads(operations.get(self.store.db, op['id'])['result'])
        self.assertEqual(2, result['deleted'])
        self.assertEqual(1, len(self.source.deletes))

    def test_restart_does_not_resume_partially_executed_plan(self):
        self.record()
        op = self.request()
        with self.store.db:
            self.store.db.execute("UPDATE camera_operations SET state='deleting',intents=? WHERE id=?",
                (json.dumps([footage().identity, footage(primary=False).identity]), op['id']))
        operations.process_one(self.service, self.settings)
        result = json.loads(operations.get(self.store.db, op['id'])['result'])
        self.assertEqual(2, result['retained'])
        self.assertTrue(result['recovered'])
        self.assertFalse(self.source.deletes)

    def test_once_cleared_archive_proof_can_be_used_without_reimport(self):
        self.record(excluded=True)
        with self.store.db:
            for row in self.service.rows():
                asset = self.store.asset(row)
                self.store.db.execute('INSERT INTO source_index VALUES (?,?,?,?,?,?,?,?,?)',
                    (self.service.destination_id, asset.identity, asset.source_type, asset.serial,
                     row['state'], row['role'], asset.size, row['remote_path'], row['remote_identity']))
            self.store.db.execute('DELETE FROM files')
        self.assertEqual(2, self.execute(self.request())['deleted'])
        self.assertFalse(self.source.downloads or self.remote.uploads)

    def test_source_already_removed_is_reported_absent_not_deleted(self):
        self.record()
        op = self.request()
        self.source.assets.clear()
        result = self.execute(op)
        self.assertEqual(2, result['absent'])
        self.assertEqual(0, result['deleted'])
        self.assertFalse(self.source.deletes)


class CameraCardTests(unittest.TestCase):
    tearDown = test_bot.BotTests.tearDown
    message = test_bot.BotTests.message
    callback = test_bot.BotTests.callback

    def setUp(self):
        test_bot.BotTests.setUp(self)
        self.sender.return_value = 'om_camera_result'
        self.store.set_meta('source_connection', {'id': 'session-a', 'state': 'approved',
            'online': True, 'last_seen': time.time(), 'device': device()})
        self.source = FakeSource([footage(), footage(primary=False)])
        self.service = Service(Config.from_values(self.settings.read()['config']), self.store,
            self.source, FakeDestination(), threading.Event())

    def poll(self):
        with self.bot.db() as db:
            db.execute('UPDATE bot_jobs SET retry_at=0')
        return self.bot.process_one()

    def test_menu_defaults_to_archived_and_does_not_queue_deletion(self):
        self.bot.receive_message(self.message('清理原片'))
        self.bot.process_one()
        body = json.dumps(self.sender.call_args.args[1], ensure_ascii=False)
        self.assertIn('已归档素材', body)
        self.assertIn('预览清理范围', body)
        self.assertFalse(operations.pending(self.store.db))
        self.assertFalse(self.source.deletes)

    def test_status_updates_original_card_only_when_backend_changes(self):
        self.bot.receive_message(self.message('相机状态'))
        self.assertTrue(self.bot.process_one())
        self.sender.assert_called_once()
        self.assertFalse(self.poll())
        self.updater.assert_not_called()
        operations.process_one(self.service, self.settings)
        self.assertTrue(self.poll())
        self.updater.assert_called_once()
        body = json.dumps(self.updater.call_args.args[2], ensure_ascii=False)
        self.assertIn('设备详情', body)
        self.assertNotIn('相机文件：', body)
        self.assertIn('未知', body)
        self.assertIn('更新 ', body)
        self.assertNotIn('北京时间', body)
        self.assertFalse(self.source.deletes)

    def test_failed_card_update_retries_without_repeating_camera_operation(self):
        self.bot.receive_message(self.message('相机状态'))
        self.bot.process_one()
        operations.process_one(self.service, self.settings)
        scans = self.source.snapshot_calls
        self.updater.side_effect = RuntimeError('send failed')
        self.assertFalse(self.poll())
        self.updater.side_effect = None
        self.assertTrue(self.poll())
        self.assertEqual(scans, self.source.snapshot_calls)

    def test_stop_sync_also_cancels_manual_operation_while_sync_is_off(self):
        op = operations.enqueue(self.store, self.settings, 'cleanup', self.bot.scope, 'preview')
        self.bot.receive_message(self.message('立即停止'))
        with patch('media_importer.bot.time.monotonic', side_effect=[1, 6]):
            self.bot.process_one()
        self.assertTrue(operations.get(self.store.db, op['id'])['cancelled'])
        operations.process_one(self.service, self.settings)
        self.assertFalse(self.source.deletes)

    def test_forged_camera_payload_cannot_bypass_confirmation(self):
        for action in ({'version': 1, 'action': 'delete', 'id': 'a' * 32},
                       {'version': 1, 'action': 'preview', 'mode': 'all'}):
            self.assertFalse(self.bot.supported({'camera': action}))
        value = self.bot.action({'camera': {'version': 1, 'action': 'preview', 'mode': 'archived'}})
        response = self.bot.receive_card(self.callback(value, owner='ou_other12345678'))
        self.assertEqual('error', response['toast']['type'])
        self.assertFalse(operations.pending(self.store.db))
