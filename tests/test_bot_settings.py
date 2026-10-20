"""Settings forms and archive changes with isolated, tiny media fixtures."""
import copy
import json
import os
import threading
from unittest import TestCase
from unittest.mock import patch

import test_bot
from test_service import FakeDestination, FakeSource, footage
from media_importer.archive_settings import check, connection_revision
from media_importer.bot_cards import settings_result
from media_importer.config import Config, ConfigError
from media_importer.destination_binding import bind
from media_importer.model import DestinationError, now
from media_importer.runtime import Control
from media_importer.service import Service
from media_importer.settings import Conflict


class SettingsFormsTests(TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    callback = test_bot.BotTests.callback
    message = test_bot.BotTests.message

    def submit(self, page, fields, event='form'):
        panel = self.bot.panel(page)
        form = next(e for e in panel['elements'] if e['tag'] == 'form')
        return self.bot.receive_card(self.callback(form['elements'][-1]['value'], event_id=event, fields=fields))

    def process(self):
        with patch('media_importer.bot.time.monotonic', side_effect=[1, 6]):
            return self.bot.process_one()

    def test_settings_entry_exposes_four_sections_without_secrets(self):
        self.bot.receive_message(self.message('设置'))
        self.process()
        result = json.dumps(self.sender.call_args.args[1], ensure_ascii=False)
        for title in ('素材与清理', '传输与缓存', '消息通知', '归档位置'):
            self.assertIn(title, result)
        for page in ('transfer_settings', 'notification_settings', 'destination_settings'):
            result += json.dumps(self.bot.panel(page), ensure_ascii=False)
        for secret in (test_bot.ENV['DESTINATION_PASSWORD'], test_bot.ENV['FEISHU_APP_SECRET']):
            self.assertNotIn(secret, result)

    def test_transfer_save_preserves_sync_delete_tasks_and_complete_cache(self):
        self.store.batch(self.original.destination_id, [footage()])
        row = self.store.files()[0]
        path = self.store.cached_path(row); path.write_bytes(b'complete-test-cache')
        before = self.store.files()
        fields = {'upload_mib_per_second': '0.5', 'max_cache_gib': '20', 'min_free_disk_gib': '5'}
        self.assertEqual('info', self.submit('transfer_settings', fields)['toast']['type'])
        self.process()
        values = self.settings.read()['config']
        self.assertEqual({'upload_mib_per_second': .5, 'max_cache_gib': 20, 'min_free_disk_gib': 5}, values['limits'])
        self.assertFalse(values['backup']['enabled'])
        self.assertFalse(values['backup']['delete_source_after_import'])
        self.assertEqual(before, self.store.files())
        self.assertEqual(b'complete-test-cache', path.read_bytes())
        self.assertIn('等待生效', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        control = Control(self.settings, threading.Event()); control.adopt()
        self.assertEqual(self.settings.read()['revision'], control.state()['applied_revision'])

    def test_all_settings_inputs_fit_feishu_card_limits(self):
        # Feishu rejects the entire card when any input exceeds its 1–1000 limit.
        for page in ('transfer_settings', 'notification_settings', 'destination_settings'):
            panel = self.bot.panel(page)
            fields = next(e for e in panel['elements'] if e['tag'] == 'form')['elements']
            for field in fields:
                if field['tag'] == 'input':
                    with self.subTest(page=page, field=field['name']):
                        self.assertGreaterEqual(field['max_length'], 1)
                        self.assertLessEqual(field['max_length'], 1000)

    def test_invalid_or_unknown_fields_are_rejected_before_persistence(self):
        before = self.path.read_bytes()
        fields = {'upload_mib_per_second': '1', 'max_cache_gib': '50', 'min_free_disk_gib': '10'}
        for bad in ('nan', '-1', 'inf', '', 'not-a-number'):
            self.assertEqual('error', self.submit('transfer_settings', {**fields, 'max_cache_gib': bad}, bad)['toast']['type'])
        self.assertEqual('error', self.submit('transfer_settings', {**fields, 'password': 'never-store-this'})['toast']['type'])
        self.assertEqual(before, self.path.read_bytes())
        with self.bot.db() as db:
            self.assertFalse(db.execute('SELECT 1 FROM bot_jobs').fetchone())

    def test_notification_form_validates_interval_and_preserves_other_values(self):
        fields = {'enabled': 'on', 'connection_events': 'off', 'progress_interval_seconds': '600', 'keyword': '导入助手'}
        before = copy.deepcopy(self.settings.read()['config'])
        self.assertEqual('info', self.submit('notification_settings', fields)['toast']['type'])
        self.process()
        after = self.settings.read()['config']
        self.assertEqual(before['backup'], after['backup'])
        self.assertEqual(before['limits'], after['limits'])
        self.assertEqual({'enabled': True, 'connection_events': False, 'progress_interval_seconds': 600, 'keyword': '导入助手'}, after['notifications'])
        for value in ('0', '59', '86401', '1.5'):
            self.assertEqual('error', self.submit('notification_settings', {**fields, 'progress_interval_seconds': value}, value)['toast']['type'])
        self.assertIn('通知设置', settings_result({'notifications': after['notifications']}, True, False)['title'])

    def test_old_form_cannot_overwrite_new_settings_and_duplicate_delivery_applies_once(self):
        panel = self.bot.panel('transfer_settings')
        token = next(e for e in panel['elements'] if e['tag'] == 'form')['elements'][-1]['value']
        fields = {'upload_mib_per_second': '1', 'max_cache_gib': '40', 'min_free_disk_gib': '5'}
        self.settings.update_preferences({'limits': {'max_cache_gib': 60}})
        self.bot.receive_card(self.callback(token, fields=fields)); self.process()
        self.assertEqual(60, self.settings.read()['config']['limits']['max_cache_gib'])
        self.assertIn('设置已经变化', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.submit('transfer_settings', fields, 'new')
        self.process()
        self.settings.update_preferences({'limits': {'max_cache_gib': 70}})
        self.submit('transfer_settings', fields, 'new')  # duplicate event id
        self.assertFalse(self.process())
        self.assertEqual(70, self.settings.read()['config']['limits']['max_cache_gib'])


class ArchiveSettingsTests(SettingsFormsTests):
    def setUp(self):
        super().setUp()
        self.remote = FakeDestination()
        self.remote.check_write = lambda: self.remote.check()
        self.factory = patch('media_importer.archive_settings.create_destination', return_value=self.remote)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.fields = {'host': 'archive.example.test', 'port': 445, 'share': 'media', 'directory': 'new-imports'}

    def checked(self, mode='new', save=False, change_id='a' * 32, fields=None):
        item = self.settings.read()
        return check(self.settings, fields or self.fields, mode, item['revision'], connection_revision(item['config']),
                     save=save, change_id=change_id)

    def test_new_archive_preview_does_not_save_and_confirmation_survives_restart(self):
        before = self.path.read_bytes()
        self.checked()
        self.assertEqual(before, self.path.read_bytes())
        previous = bind(self.store, Config.load(self.path)).destination_id
        saved = self.checked(save=True)
        config = Config.load(self.path)
        self.assertEqual(self.fields['host'], config.destination['host'])
        self.assertEqual('archive:' + 'a' * 32, config.destination_id)
        self.assertNotEqual(previous, config.destination_id)
        self.assertEqual(config.destination_id, bind(self.store, config).destination_id)
        self.assertEqual(saved['revision'], self.settings.read()['revision'])
        self.settings.update_preferences({'limits': {'max_cache_gib': 80}})
        self.assertEqual(self.fields['directory'], Config.load(self.path).destination['directory'])
        self.assertEqual(test_bot.ENV['DESTINATION_HOST'], os.environ['DESTINATION_HOST'])
        self.assertNotIn(test_bot.ENV['DESTINATION_PASSWORD'], self.path.read_text())

    def test_active_sync_unfinished_tasks_unknown_cache_and_offline_cache_block_switch(self):
        before = self.path.read_bytes()
        self.settings.set_enabled(True)
        with self.assertRaises(ConfigError): self.checked(save=True)
        self.settings.set_enabled(False)
        self.store.batch(self.original.destination_id, [footage()])
        with self.assertRaises(ConfigError): self.checked(save=True)
        self.assertEqual(1, len(self.store.files()))
        with self.store.db: self.store.db.execute('DELETE FROM files')
        path = self.store.cache / 'orphan.bin'; path.write_bytes(b'keep')
        with self.assertRaises(ConfigError): self.checked(save=True)
        self.assertEqual(b'keep', path.read_bytes())
        path.unlink()
        self.store.cache.rmdir()
        with self.assertRaises(ConfigError): self.checked(save=True)
        self.assertNotIn('archive', json.loads(self.path.read_text()))

    def test_readonly_or_missing_destination_never_changes_config(self):
        before = self.path.read_bytes()
        self.remote.online = False
        with self.assertRaises(ConfigError): self.checked(save=True)
        self.remote.online = True
        def denied(): raise DestinationError('SMB 没有写入权限')
        self.remote.check_write = denied
        with self.assertRaisesRegex(ConfigError, '写入权限'): self.checked(save=True)
        self.assertEqual(before, self.path.read_bytes())

    def test_connection_alias_proves_identity_and_preserves_pending_task_keys(self):
        values = copy.deepcopy(self.original.values); values['backup']['enabled'] = True
        config = Config.from_values(values)
        service = Service(config, self.store, FakeSource([footage()]), self.remote, threading.Event())
        self.assertTrue(service.cycle()['complete'])
        self.store.batch(service.destination_id, [footage(2)])
        rows = self.store.files()
        fields = {k: config.destination[k] for k in self.fields}; fields['host'] = 'alias.example.test'
        self.checked('connection', save=True, fields=fields)
        changed = bind(self.store, Config.load(self.path))
        self.assertEqual(service.destination_id, changed.destination_id)
        self.assertEqual(rows, self.store.files())
        self.remote.identities.clear()
        fields['host'] = 'different.example.test'
        before = self.path.read_bytes()
        with self.assertRaises(ConfigError): self.checked('connection', save=True, fields=fields)
        self.assertEqual(before, self.path.read_bytes())

    def test_destination_confirmation_is_explicit_and_checks_for_new_work(self):
        fields = {**{k: str(v) for k, v in self.fields.items()}, 'mode': 'new'}
        self.submit('destination_settings', fields); self.process()
        response = self.sender.call_args.args[1]
        self.assertIn('尚未保存', json.dumps(response, ensure_ascii=False))
        self.assertNotIn('archive', json.loads(self.path.read_text()))
        token = response['elements'][-1]['actions'][-1]['value']
        self.store.batch(self.original.destination_id, [footage()])
        self.bot.receive_card(self.callback(token, event_id='confirm')); self.process()
        self.assertNotIn('archive', json.loads(self.path.read_text()))
        self.assertIn('未保存', self.sender.call_args.args[1]['header']['title']['content'])

    def test_save_then_reply_crash_does_not_allocate_another_archive(self):
        item = self.settings.read()
        saved = self.checked(save=True)
        restored = check(self.settings, self.fields, 'new', item['revision'], connection_revision(item['config']),
                         save=True, change_id='a' * 32)
        self.assertEqual(saved['revision'], restored['revision'])

    def test_local_env_edit_invalidates_confirmation(self):
        item = self.settings.read()
        os.environ['DESTINATION_HOST'] = 'changed.example.test'
        with self.assertRaises(Conflict):
            check(self.settings, self.fields, 'new', item['revision'], connection_revision(item['config']),
                  save=True, change_id='a' * 32)

    def test_new_target_import_upload_cleanup_keeps_previous_archive_and_source(self):
        source = FakeSource([footage()])
        values = copy.deepcopy(self.original.values); values['backup']['enabled'] = True
        service = Service(Config.from_values(values), self.store, source, self.remote, threading.Event())
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(1, service.settle())
        original_objects = copy.deepcopy(self.remote.objects)
        new_remote = FakeDestination(); new_remote.check_write = lambda: new_remote.check()
        with patch('media_importer.archive_settings.create_destination', return_value=new_remote):
            self.checked(save=True)
        self.settings.set_enabled(True)
        service = Service(Config.load(self.path), self.store, source, new_remote, threading.Event())
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(1, service.settle())
        self.assertEqual(1, len(new_remote.uploads))
        self.assertEqual(original_objects, self.remote.objects)
        self.assertEqual([], source.deletes)
        self.assertFalse(list(self.store.cache.glob('*.bin')))
        self.assertFalse(self.store.files())
