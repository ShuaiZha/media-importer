import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from media_importer.bot import Bot
from media_importer.config import Config, preferences
from media_importer.model import ConfigError
from media_importer.settings import Settings, Conflict
from media_importer.store import Store
from test_service import configuration

ENV = {'SOURCE_URL': 'http://172.21.123.51:8080', 'SOURCE_SERIAL': 'C0000000000123',
       'DESTINATION_HOST': '192.0.2.20', 'DESTINATION_SHARE': 'media',
       'DESTINATION_USERNAME': 'example_user', 'DESTINATION_DIRECTORY': 'media',
       'DESTINATION_PASSWORD': 'secret$with#quotes', 'FEISHU_APP_ID': 'cli_test12345678',
       'FEISHU_APP_SECRET': 'example-secret', 'FEISHU_OWNER_OPEN_ID': 'ou_owner123456789'}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, ENV, clear=True); self.env.start()
        self.path = self.root / 'settings.json'
        self.original = configuration(enabled=False, delete_source_after_import=False)
        self.path.write_text(json.dumps(preferences(self.original.values)))
        self.settings = Settings(self.path)
        self.store = Store(self.root / 'data')
        self.sender = Mock()
        self.updater = Mock()
        self.bot = Bot(self.settings, self.root / 'data', sender=self.sender, updater=self.updater)

    def tearDown(self):
        self.store.close(); self.env.stop(); self.tmp.cleanup()

    def message(self, command='状态', event_id='e1', owner=None, chat='p2p', seconds=0):
        return {'header': {'app_id': ENV['FEISHU_APP_ID'], 'event_id': event_id}, 'event': {
            'sender': {'sender_type':'user', 'sender_id': {'open_id': owner or ENV['FEISHU_OWNER_OPEN_ID']}},
            'message': {'chat_type':chat, 'message_type':'text', 'content':json.dumps({'text':command}),
                        'create_time': str(int((time.time()+seconds)*1000))}}}

    def callback(self, value, event_id='c1', owner=None, fields=None):
        return {'header': {'app_id':ENV['FEISHU_APP_ID'], 'event_id':event_id}, 'event': {
            'operator': {'open_id':owner or ENV['FEISHU_OWNER_OPEN_ID']},
            'action': {'value':value, 'form_value':fields or {}}}}

    def test_environment_migration_keeps_destination_identity_without_secrets_in_json(self):
        new = Config.load(self.path)
        self.assertEqual(self.original.destination_id, new.destination_id)
        self.settings.update_preferences({'limits':{'max_cache_gib':60}})
        saved = json.loads(self.path.read_text())
        self.assertEqual({'version','backup','limits','notifications','preview','livestream','archive','source_options'}, set(saved))
        self.assertNotIn('provider', saved['notifications'])
        for secret in ('example-secret','secret$with#quotes','FEISHU_APP_SECRET','destination'):
            self.assertNotIn(secret, self.path.read_text())
        self.assertEqual(60, saved['limits']['max_cache_gib'])
        self.assertEqual(ENV['FEISHU_OWNER_OPEN_ID'], self.settings.notification_credentials()['receive_id'])

    def test_owner_only_and_private_chat_only(self):
        self.assertFalse(self.bot.receive_message(self.message(owner='ou_other12345678')))
        self.assertFalse(self.bot.receive_message(self.message(chat='group')))
        self.assertFalse(self.bot.process_one())
        value = self.bot.action({'changes':{'backup':{'enabled':True}}})
        reply = self.bot.receive_card(self.callback(value, owner='ou_other12345678'))
        self.assertEqual('error', reply['toast']['type'])
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])

    def test_message_delivery_deduplicates_and_survives_restart(self):
        event = self.message()
        self.bot.receive_message(event); self.bot.receive_message(event)
        again = Bot(self.settings, self.root/'data', sender=self.sender)
        self.assertTrue(again.process_one())
        self.assertFalse(again.process_one())
        self.sender.assert_called_once()

    def test_stale_card_cannot_reenable_sync_but_can_stop(self):
        rev = self.settings.read()['revision']
        start = self.bot.action({'changes':{'backup':{'enabled':True}}}, rev)
        stop = self.bot.action({'changes':{'backup':{'enabled':False}}}, rev)
        self.settings.update_preferences({'limits':{'max_cache_gib':60}})
        self.bot.receive_card(self.callback(start)); self.bot.process_one()
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
        self.assertIn('设置已经变化', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.settings.set_enabled(True)
        self.bot.receive_card(self.callback(stop, event_id='stop'))
        with patch('media_importer.bot.time.monotonic', side_effect=[1,6]):self.bot.process_one()
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])

    def test_old_start_message_does_not_trigger_work(self):
        self.bot.receive_message(self.message('开启同步', seconds=-600))
        self.bot.process_one()
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
        self.assertIn('过期', json.dumps(self.sender.call_args.args[1],ensure_ascii=False))


    def test_replied_failure_does_not_apply_setting_twice(self):
        self.bot.receive_message(self.message('开启同步'))
        self.sender.side_effect = RuntimeError('not logged')
        with patch('media_importer.bot.time.monotonic', side_effect=[1,6]):self.bot.process_one()
        self.assertTrue(self.settings.read()['config']['backup']['enabled'])
        self.settings.set_enabled(False)
        with self.bot.db() as db:db.execute('UPDATE bot_jobs SET retry_at=0')
        self.sender.side_effect=None
        self.bot.process_one()
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])

    def test_delete_requires_separate_confirmation_card(self):
        before=self.settings.read()['revision']
        cleanup=self.bot.panel('cleanup')
        with self.bot.db() as db:
            actions=[json.loads(x[0]) for x in db.execute('SELECT payload FROM bot_actions')]
        self.assertEqual([{'panel':'confirm_delete'}], actions)
        self.bot.panel('confirm_delete')
        self.assertEqual(before,self.settings.read()['revision'])
        self.assertFalse(self.settings.read()['config']['backup']['delete_source_after_import'])
        with self.bot.db() as db:
            actions=[json.loads(x[0]) for x in db.execute('SELECT payload FROM bot_actions')]
        self.assertIn({'changes':{'backup':{'delete_source_after_import':True}},'page':'cleanup'},actions)

    def test_stopped_status_panels_offer_only_refresh_and_safe_checks(self):
        before = self.path.read_bytes()
        for page in ('status', 'devices', 'tasks'):
            response = self.bot.panel(page)
            self.assertTrue(response['config']['update_multi'])
            buttons = [button for e in response['elements'] if e['tag'] == 'action' for button in e['actions']]
            self.assertIn('刷新', [b['text']['content'] for b in buttons])
            self.assertTrue(all(b['text']['content'] in ('刷新', '查看详情') for b in buttons))
            refresh = next(b for b in buttons if b['text']['content'] == '刷新')
            with self.bot.db() as db:
                payload = json.loads(db.execute('SELECT payload FROM bot_actions WHERE token=?',
                                               (refresh['value']['token'],)).fetchone()[0])
            self.assertEqual({'panel': 'status', 'refresh': True}, payload)
        self.assertEqual(before, self.path.read_bytes())

    def test_old_status_commands_open_the_single_read_only_card(self):
        before = self.path.read_bytes()
        for label in ('运行总览', '设备与存储', '当前任务'):
            self.bot.receive_message(self.message(label, event_id=label))
            self.assertTrue(self.bot.process_one())
            self.assertIn('服务未响应', self.sender.call_args.args[1]['header']['title']['content'])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([], self.store.files())

    def test_refresh_replaces_the_clicked_message_and_retry_does_not_send_a_new_one(self):
        before = self.path.read_bytes()
        value = next(b['value'] for b in self.bot.panel('devices')['elements'][-1]['actions'] if b['text']['content'] == '刷新')
        event = self.callback(value)
        event['event']['context'] = {'open_message_id': 'om_status_test'}
        reply = self.bot.receive_card(event)
        self.assertIn('正在更新状态', reply['toast']['content'])
        self.bot.receive_card(event)
        self.updater.side_effect = [OSError('offline'), None]
        self.assertFalse(self.bot.process_one())
        with self.bot.db() as db:
            db.execute('UPDATE bot_jobs SET retry_at=0')
        again = Bot(self.settings, self.root/'data', sender=self.sender, updater=self.updater)
        self.assertTrue(again.process_one())
        self.assertFalse(again.process_one())
        self.sender.assert_not_called()
        self.assertEqual(2, self.updater.call_count)
        self.assertEqual('om_status_test', self.updater.call_args.args[1])
        self.assertIn('服务未响应', self.updater.call_args.args[2]['header']['title']['content'])
        self.assertEqual(before, self.path.read_bytes())

    def test_refresh_requires_a_valid_original_message_and_owner(self):
        value = next(b['value'] for b in self.bot.panel()['elements'][-1]['actions'] if b['text']['content'] == '刷新')
        for message_id in (None, 'https://example.invalid', 'om_bad/other'):
            event = self.callback(value)
            event['event']['context'] = {'open_message_id': message_id}
            self.assertEqual('error', self.bot.receive_card(event)['toast']['type'])
        event = self.callback(value, owner='ou_other')
        event['event']['context'] = {'open_message_id': 'om_status_test'}
        self.assertEqual('error', self.bot.receive_card(event)['toast']['type'])
        self.assertFalse(self.bot.process_one())



    def test_expired_and_forged_card_cannot_mutate_settings(self):
        self.assertEqual('error',self.bot.receive_card(self.callback({'token':'forged'}))['toast']['type'])
        value=self.bot.action({'changes':{'backup':{'enabled':True}}})
        with self.bot.db() as db:db.execute('UPDATE bot_actions SET created=0')
        self.assertEqual('error',self.bot.receive_card(self.callback(value))['toast']['type'])

    def test_ordinary_chat_body_not_stored_or_echoed(self):
        self.bot.receive_message(self.message('accidental-secret$123'))
        self.bot.process_one()
        with self.bot.db() as db:text=''.join(x[0] for x in db.execute('SELECT payload FROM bot_jobs'))
        self.assertNotIn('accidental-secret',text)
        self.assertNotIn('accidental-secret',str(self.sender.call_args))

    def test_configuration_cannot_leak_secret_in_status_card(self):
        for page in ('status', 'devices', 'tasks'):
            text=json.dumps(self.bot.panel(page), ensure_ascii=False)
            self.assertNotIn(ENV['DESTINATION_PASSWORD'],text)
            self.assertNotIn(ENV['FEISHU_APP_SECRET'],text)

    def test_settings_compare_and_swap(self):
        rev=self.settings.read()['revision'];self.settings.set_enabled(True)
        with self.assertRaises(Conflict):self.settings.update_preferences({'limits':{'max_cache_gib':60}},rev)

    def test_resuming_does_not_repeat_old_sync_off_message(self):
        from media_importer.model import now
        self.settings.set_enabled(True)
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(), stages={
            name: {'phase': 'paused', 'message': '同步关闭，任务与缓存保留'}
            for name in ('import', 'upload')})
        text = json.dumps(self.bot.panel(), ensure_ascii=False)
        self.assertIn('设置等待生效', text)
        self.assertNotIn('同步关闭，任务与缓存保留', text)

    def test_settings_apply_acknowledges_and_updates_original_card_without_resave(self):
        from media_importer.model import now
        value = self.bot.action({'changes': {'backup': {'delete_source_after_import': True}}, 'page': 'cleanup'}, self.settings.read()['revision'])
        event = self.callback(value)
        event['event']['context'] = {'open_message_id': 'om_original_settings'}
        self.bot.receive_card(event)
        with patch('media_importer.bot.time.monotonic', side_effect=[1, 6]):
            self.assertTrue(self.bot.process_one())
        self.sender.assert_not_called()
        self.assertIn('等待生效', json.dumps(self.updater.call_args.args[2], ensure_ascii=False))
        saved = self.path.read_bytes()
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(), configuration={'applied_revision': self.settings.read()['revision'], 'pending': False})
        with self.bot.db() as db:
            db.execute('UPDATE bot_jobs SET retry_at=0')
        self.assertTrue(self.bot.process_one())
        self.assertEqual(2, self.updater.call_count)
        self.assertEqual('om_original_settings', self.updater.call_args.args[1])
        self.assertNotIn('等待生效', json.dumps(self.updater.call_args.args[2], ensure_ascii=False))
        self.assertEqual(saved, self.path.read_bytes())
        self.assertTrue(self.updater.call_args.args[2]['config']['update_multi'])


    def test_start_uses_one_live_progress_delivery_when_notifications_are_enabled(self):
        from media_importer.model import now
        self.settings.update_preferences({'notifications': {'enabled': True}})
        self.store.set_meta('service', {'running': True})
        self.bot.receive_message(self.message('开启同步'))
        from media_importer.status import snapshot as actual_snapshot
        def applied(*args):
            self.store.status(worker_heartbeat_at=now(), configuration={'applied_revision': self.settings.read()['revision'], 'pending': False})
            return actual_snapshot(*args)
        with patch('media_importer.bot.snapshot', side_effect=applied):
            self.assertTrue(self.bot.process_one())
        self.sender.assert_not_called()
        with self.store.db:
            events = self.store.db.execute("SELECT kind FROM notification_events").fetchall()
        self.assertEqual(['requested_progress'], [r[0] for r in events])
