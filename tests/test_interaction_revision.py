"""Current facts and form transactions, using isolated state and tiny cache files."""
import copy
import json
import unittest
from unittest.mock import patch

import test_bot
from test_service import footage, FakeDestination
from media_importer.bot_cards import status_content, progress_card, cleanup_summary
from media_importer.bot_routes import COMMANDS, TEXT_ACTIONS
from media_importer.bot_settings import parse
from media_importer.camera_web import cleanup_snapshot
from media_importer.config import ConfigError
from media_importer.message_facts import cleanup_scope


class FactsTests(unittest.TestCase):
    def test_finished_selection_is_a_recent_result_not_active_automatic_backup(self):
        view = {'running': True, 'configuration': {'sync_enabled': False},
                'selected_import': {'state': 'done'},
                'batch': {'total_files': 2, 'total_bytes': 2048, 'completed_files': 2, 'imported_files': 2},
                'cleanup': {'cache_pending': 1, 'source_not_deleted': 2}}
        title, _, elements = status_content(view)
        rendered = str(elements)
        self.assertEqual('备份已停止', title)
        self.assertIn('最近结果：已归档 2 个文件', rendered)
        self.assertIn('缓存待清理 1 个', rendered)
        self.assertNotIn('100%', rendered)
        receipt = progress_card(view, 300)
        self.assertIn('归档完成', receipt['header']['title']['content'])

    def test_active_selected_backup_does_not_imply_automatic_backup_enabled(self):
        view = {'running': True, 'configuration': {'sync_enabled': False},
                'selected_import': {'state': 'running'},
                'batch': {'total_files': 2, 'completed_files': 0, 'imported_files': 1},
                'stages': {'upload': {'phase': 'publishing'}}}
        title, _, elements = status_content(view)
        self.assertIn('进行中', title)
        self.assertIn('自动备份：关闭', str(elements))
        self.assertNotIn('已完成', str(elements))

    def test_cleanup_counts_adapter_confirmed_members_not_extensions(self):
        primary = footage().to_dict()
        auxiliary = {**primary, 'identity_key': 'proxy', 'path': 'DCIM/preview.LRV'}
        unassociated = {**primary, 'identity_key': 'unknown', 'path': 'DCIM/standalone.LRV'}
        plan = {'groups': [{'members': [primary, auxiliary, unassociated], 'preview_ids': ['proxy']}]}
        counts = cleanup_scope(plan)
        self.assertEqual((2, 1), (counts['primary_files'], counts['auxiliary_files']))
        op = {'id': 'test', 'state': 'done', 'cancelled': False, 'mode': 'archived', 'plan': json.dumps(plan),
              'result': json.dumps({'deleted': 1, 'unknown': 1, 'details': [
                  {'path': auxiliary['path'], 'state': 'deleted'}, {'path': primary['path'], 'state': 'unknown'}]})}
        result = cleanup_snapshot(op)
        self.assertEqual(1, result['deleted_auxiliary'])
        self.assertNotIn('deleted_primary', result)
        self.assertEqual(1, result['unknown_primary'])
        self.assertIn('原片已删除 1 个', cleanup_summary({'source_deleted': 1, 'preview_deleted': 1}))
        self.assertIn('辅助文件已删除 1 个', cleanup_summary({'source_deleted': 1, 'preview_deleted': 1}))

    def test_new_menu_uses_workspace_and_old_selection_remains_available(self):
        self.assertEqual('library', COMMANDS['sync.select'][1]['capture']['action'])
        self.assertEqual('library', COMMANDS['camera.library'][1]['capture']['action'])
        self.assertIn('selected_import', TEXT_ACTIONS['选择文件'])
        self.assertIn('自动', COMMANDS['sync.start'][0])

    def test_validation_errors_name_only_the_relevant_field(self):
        with self.assertRaisesRegex(ConfigError, '缓存额度') as error:
            parse('transfer_settings', {'upload_mib_per_second': '1', 'max_cache_gib': 'nan', 'min_free_disk_gib': '10'}, 5)
        self.assertNotIn('端口', str(error.exception))
        self.assertEqual(90, parse('notification_settings', {'enabled': 'on', 'progress_interval_minutes': '1.5'}, 5)['notifications']['progress_interval_seconds'])
        self.assertEqual(90, parse('notification_settings', {'enabled': 'on', 'progress_interval_seconds': '90'}, 4)['notifications']['progress_interval_seconds'])


class FormTransactions(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    callback = test_bot.BotTests.callback

    def submit(self, page, fields, event='form'):
        form = next(e for e in self.bot.panel(page)['elements'] if e['tag'] == 'form')
        token = form['elements'][-1]['value']
        return self.bot.receive_card(self.callback(token, fields=fields, event_id=event))

    def process(self):
        with patch('media_importer.bot.time.monotonic', side_effect=[1, 6]):
            self.bot.process_one()

    def test_combined_media_form_never_enables_deletion_without_confirmation(self):
        self.store.batch(self.original.destination_id, [footage()])
        rows = self.store.files(); cache = self.store.cached_path(rows[0]); cache.write_bytes(b'keep')
        before = self.path.read_bytes()
        fields = {'media_scope': 'video', 'archive_previews': 'on', 'delete_source_after_import': 'on'}
        self.assertEqual('info', self.submit('media_settings', fields)['toast']['type']); self.process()
        self.assertEqual(before, self.path.read_bytes())
        card = self.sender.call_args.args[1]
        self.assertIn('唯一副本', str(card))
        token = card['elements'][-1]['actions'][-1]['value']
        event = self.callback(token, event_id='confirm')
        self.bot.receive_card(event); self.process()
        config = self.settings.read()['config']
        self.assertTrue(config['backup']['delete_source_after_import'])
        self.assertTrue(config['backup']['archive_previews'])
        self.assertEqual('video', config['backup']['media_scope'])
        self.assertFalse(config['backup']['enabled'])
        self.assertEqual(rows, self.store.files()); self.assertEqual(b'keep', cache.read_bytes())
        revision = self.settings.read()['revision']
        self.bot.receive_card(event); self.process()
        self.assertEqual(revision, self.settings.read()['revision'])

    def test_combined_source_form_saves_transport_and_hints_atomically(self):
        before = copy.deepcopy(self.settings.read()['config'])
        fields = {'source_transport': 'usb', 'url': 'http://192.0.2.50:8080', 'serial': ''}
        self.assertEqual('info', self.submit('source_settings', fields)['toast']['type']); self.process()
        value = self.settings.read()['config']
        self.assertEqual('usb', value['backup']['source_transport'])
        self.assertEqual(fields['url'], value['source']['url'])
        self.assertEqual(before['source']['timeout_seconds'], value['source']['timeout_seconds'])
        self.assertFalse(value['backup']['enabled'])
        self.assertEqual('info', self.submit('source_advanced', {'timeout_seconds': '25', 'request_attempts': '2'}, event='advanced')['toast']['type']); self.process()
        value = self.settings.read()['config']
        self.assertEqual(fields['url'], value['source']['url']); self.assertEqual(25, value['source']['timeout_seconds'])

    def test_destination_core_preserves_advanced_values_and_uses_explicit_commit(self):
        target = self.settings.read()['config']['destination']
        fields = {k: target[k] for k in ('host', 'share', 'directory', 'name', 'username')}
        fields.update(mode='connection', password='')
        remote = FakeDestination(); remote.check_write = lambda: remote.check()
        before = self.path.read_bytes()
        with patch('media_importer.archive_settings.create_destination', return_value=remote):
            self.assertEqual('info', self.submit('destination_settings', fields)['toast']['type']); self.process()
            self.assertEqual(before, self.path.read_bytes())
            result = self.sender.call_args.args[1]
            self.assertIn('尚未保存', str(result))
            token = result['elements'][-1]['actions'][-1]['value']
            self.bot.receive_card(self.callback(token, event_id='confirm')); self.process()
        saved = self.settings.read()['config']['destination']
        self.assertEqual(target['port'], saved['port']); self.assertEqual(target['domain'], saved['domain'])
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
