"""Daily controls stay small without losing the persistent transfer contract."""
import json
import os
import unittest
from unittest.mock import patch
from pathlib import Path

import test_bot
from media_importer.bot import Bot
from media_importer.config import Config
from media_importer.model import ConfigError
from media_importer.adapters.gopro import GoProSource
from media_importer.bot_cards import status_content
from test_bot_cards import view


class SimpleControlsTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    callback = test_bot.BotTests.callback
    message = test_bot.BotTests.message

    def test_usb_only_preserves_connection_identity_and_rejects_wifi_without_saving(self):
        before = self.path.read_bytes()
        config = Config.load(self.path)
        self.assertEqual(config.source['url'], GoProSource(config).base)
        with self.assertRaises(ConfigError):
            self.settings.update_preferences({'backup': {'source_transport': 'wifi'}})
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(config.destination_id, Config.load(self.path).destination_id)
        self.assertFalse(any((Path('media_importer') / name).exists() for name in ('wireless_control.py','wireless_host.py')))

    def test_retired_card_forms_controls_and_checks_are_rejected_without_changes(self):
        before = self.path.read_bytes()
        payloads = [
            {'form': 'transfer'}, {'operation': 'verify_cache'},
            {'changes': {'limits': {'min_free_disk_gib': 0}}},
            {'changes': {'notifications': {'enabled': False}}},
            {'changes': {'backup': {'media_scope': 'photo'}}},
            {'changes': {'backup': {'archive_previews': True}}},
            {'changes': {'backup': {'source_transport': 'wifi'}}},
            {'changes': {'backup': {'stop_after_current': True}}},
            {'changes': {'backup': {'enabled': 'true'}}}]
        for payload in payloads:
            token = self.bot.action(payload, self.settings.read()['revision'])
            self.assertEqual('error', self.bot.receive_card(self.callback(token))['toast']['type'])
        self.assertFalse(self.bot.process_one())
        self.assertEqual(before, self.path.read_bytes())

    def test_old_queued_or_applied_control_never_executes_after_upgrade(self):
        before = self.path.read_bytes()
        for state in ('queued', 'applied', 'waiting'):
            self.bot.enqueue('old-'+state, {'changes': {'backup': {'archive_previews': True}}, 'revision': self.settings.read()['revision']})
            with self.bot.db() as db:
                db.execute('UPDATE bot_jobs SET state=?,response=? WHERE id=?', (state, '{}', 'old-'+state))
            self.assertTrue(self.bot.process_one())
            self.assertIn('此操作已停用', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.assertEqual(before, self.path.read_bytes())

    def test_file_settings_still_validate_and_survive_daily_controls(self):
        self.settings.update_preferences({'limits': {'upload_mib_per_second': 0.125, 'max_cache_gib': 60},
            'backup': {'media_scope': 'photo', 'archive_previews': True},
            'notifications': {'progress_interval_seconds': 600}})
        expected = self.settings.read()['config']
        for text, enabled in (('开启同步',True), ('停止同步',False)):
            self.bot.receive_message(self.message(text, event_id=text))
            with patch('media_importer.bot.time.monotonic',side_effect=[1,6]):self.assertTrue(self.bot.process_one())
            current = self.settings.read()['config']
            self.assertEqual(enabled, current['backup']['enabled'])
            self.assertEqual(expected['limits'], current['limits'])
            self.assertEqual(expected['notifications'], current['notifications'])
            self.assertEqual('photo',current['backup']['media_scope'])
            self.assertTrue(current['backup']['archive_previews'])
        with self.assertRaises(ConfigError):self.settings.update_preferences({'limits': {'max_cache_gib': float('nan')}})

    def test_original_delete_toggle_requires_confirmation_then_can_disable(self):
        self.bot.panel('cleanup')
        with self.bot.db() as db:
            actions = [json.loads(r[0]) for r in db.execute('SELECT payload FROM bot_actions')]
        self.assertEqual([{'panel': 'confirm_delete'}], actions)
        confirmation = self.bot.panel('confirm_delete')
        self.assertIn('唯一副本',json.dumps(confirmation,ensure_ascii=False))
        button = confirmation['elements'][-1]['actions'][-1]
        self.bot.receive_card(self.callback(button['value'],event_id='delete-on'))
        with patch('media_importer.bot.time.monotonic',side_effect=[1,6]):self.assertTrue(self.bot.process_one())
        self.assertTrue(self.settings.read()['config']['backup']['delete_source_after_import'])
        button = self.bot.panel('cleanup')['elements'][-1]['actions'][0]
        self.bot.receive_card(self.callback(button['value'],event_id='delete-off'))
        with patch('media_importer.bot.time.monotonic',side_effect=[1,6]):self.assertTrue(self.bot.process_one())
        self.assertFalse(self.settings.read()['config']['backup']['delete_source_after_import'])
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])

    def test_unknown_commands_explain_files_without_echoing_arbitrary_input(self):
        self.bot.receive_message(self.message('private example value'))
        self.bot.process_one()
        response = json.dumps(self.sender.call_args.args[1],ensure_ascii=False)
        self.assertIn('配置文件',response)
        self.assertIn('原片处理',response)
        self.assertNotIn('private example value',response)

    def test_one_status_card_has_both_lanes_storage_path_and_cleanup_problem(self):
        current=view()
        current['archive']={'locations':['smb://example.test/share/Camera']}
        current['issues']=[{'path':'clip.mp4','error':'稍后重试'}]
        text=json.dumps(status_content(current),ensure_ascii=False)
        for value in ('相机：在线','本地缓存：可用','SMB：在线','归档位置','smb://example.test/share/Camera','稍后重试','clip.mp4'):
            self.assertIn(value,text)
        self.assertNotIn('历史',text)
        self.assertNotIn('设备与存储',text)

    def test_idle_status_does_not_ask_to_keep_devices_online_without_work(self):
        current=view()
        current['batch'].update(total_files=0,completed_files=0,scanned=True)
        current['work']['pending_files']=0
        current['unplug']={'ready':True,'can_disconnect':True}
        text=json.dumps(status_content(current),ensure_ascii=False)
        self.assertIn('同步待机',text)
        self.assertIn('当前无待处理任务',text)
        self.assertNotIn('需保持可用',text)
