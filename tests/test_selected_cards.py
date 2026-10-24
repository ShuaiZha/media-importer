"""Small, isolated fixtures for selection forms and progress receipts."""
import copy
import json
import threading
import time
import unittest

from media_importer import selected_cards as cards
from media_importer import selected_imports as operations
from media_importer.bot_cards import plain
from media_importer.config import Config
from media_importer.model import Asset, ConfigError
from media_importer.service import Service
import test_bot
from test_service import FakeSource, FakeDestination, footage
from test_source_connection import device


class Bot:
    def button(self, label, payload, revision=None, danger=False):
        return {'tag': 'button', 'text': plain(label), 'type': 'danger' if danger else 'default',
                'value': payload}

    def buttons(self, *items):
        return {'tag': 'action', 'actions': list(items)}


def operation(groups=13, state='choosing', selected=None):
    items = []
    for index in range(groups):
        items.append({'id': str(index), 'safe': True, 'reason': '', 'preview_ids': [f'p{index}'],
                      'members': [
                          member(f'DCIM/100CAM/clip-{index}.MP4', f'm{index}', 1024),
                          member(f'DCIM/100CAM/clip-{index}.LRV', f'p{index}', 128)]})
    return {'id': 'a' * 32, 'owner': 'example-owner', 'state': state, 'cancelled': 0,
            'revision': 'configuration-a', 'choice_revision': 7, 'updated': 1,
            'plan': json.dumps({'groups': items, 'selected': selected or [], 'archive_previews': False}),
            'result': '{}'}


def member(path, identity, size):
    return Asset('test-device', path, size, '', '', identity).to_dict()


def text(result):
    return json.dumps(result, ensure_ascii=False)


def buttons(result):
    found = []
    def visit(item):
        if isinstance(item, dict):
            if item.get('tag') == 'button':
                found.append(item)
            for value in item.values():
                visit(value)
        elif isinstance(item, list):
            for value in item:
                visit(value)
    visit(result)
    return found


class SelectionCardTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot()

    def test_browse_keeps_saved_page_selection_and_never_selects_other_pages(self):
        op = operation(selected=[0, 11, 12])
        before = copy.deepcopy(op)
        result = cards.render(self.bot, op, 0)
        form = next(element for element in result['elements'] if element['tag'] == 'form')
        select = form['elements'][0]
        self.assertEqual('multi_select_static', select['tag'])
        self.assertEqual('groups', select['name'])
        self.assertEqual(['0', '11'], select['selected_values'])
        self.assertEqual(12, len(select['options']))
        self.assertFalse(select['required'])
        self.assertNotIn('clip-12.MP4', text(result))
        self.assertIn('已选 3 组', text(result))
        next_page = next(b for b in buttons(result) if b['text']['content'] == '保存并到下一页')
        self.assertEqual('form_submit', next_page['action_type'])
        self.assertEqual({'version': 1, 'action': 'choose', 'id': op['id'], 'page': 0,
                          'next_page': 1, 'choice_revision': 7, 'review': False},
                         next_page['value']['selected_import'])
        self.assertEqual(before, op)

    def test_last_page_can_clear_only_its_own_selection(self):
        op = operation(selected=[0, 12])
        result = cards.render(self.bot, op, 1)
        form = next(element for element in result['elements'] if element['tag'] == 'form')
        self.assertEqual(['12'], form['elements'][0]['selected_values'])
        self.assertEqual(['12'], [option['value'] for option in form['elements'][0]['options']])
        self.assertEqual([], cards.parse_choices({'groups': []}, 1))
        self.assertEqual([], cards.parse_choices({}, 1))
        self.assertEqual([], cards.parse_choices(None, 1))
        previous = next(b for b in buttons(result) if b['text']['content'] == '保存并到上一页')
        self.assertEqual('form_submit', previous['action_type'])
        self.assertEqual(1, previous['value']['selected_import']['page'])
        self.assertEqual(0, previous['value']['selected_import']['next_page'])
        self.assertFalse(any(b['text']['content'] == '保存并到下一页' for b in buttons(result)))

    def test_preview_submission_saves_visible_choices_before_review(self):
        result = cards.render(self.bot, operation())
        submit = next(b for b in buttons(result) if b['text']['content'] == '保存并预览')
        self.assertEqual('form_submit', submit['action_type'])
        self.assertEqual('primary', submit['type'])
        self.assertTrue(submit['value']['selected_import']['review'])
        self.assertEqual([1, 5], cards.parse_choices({'groups': ['5', '1']}, 0))
        self.assertNotIn('confirm', [b['value']['selected_import']['action'] for b in buttons(result)])

    def test_chooser_lists_files_only_in_dropdown_with_archive_scope_totals(self):
        op = operation(groups=1, selected=[0])
        plan = json.loads(op['plan'])
        plan['groups'][0]['members'] = [
            member('DCIM/GH021414.MP4', 'second', 2048),
            member('DCIM/GL021414.LRV', 'preview2', 4096),
            member('DCIM/GH011414.MP4', 'first', 1024),
            member('DCIM/GL011414.LRV', 'preview1', 8192),
        ]
        plan['groups'][0]['preview_ids'] = ['preview1', 'preview2']
        op['plan'] = json.dumps(plan)
        before = copy.deepcopy(op)
        result = cards.render(self.bot, op)
        form = next(element for element in result['elements'] if element['tag'] == 'form')
        label = form['elements'][0]['options'][0]['text']['content']
        self.assertEqual('1. GH011414.MP4 · 2 段视频 · 3.0 KiB', label)
        body = [element for element in result['elements'] if element['tag'] != 'form']
        self.assertNotIn('.MP4', text(body))
        self.assertEqual(1, text(result).count('GH011414.MP4'))
        self.assertEqual(2, operations._summary(plan)['total_files'])
        self.assertEqual(3072, operations._summary(plan)['total_bytes'])
        self.assertEqual(before, op)  # Presentation cannot change the adapter's processing order.

    def test_preview_policy_adds_only_confirmed_previews_to_label_and_file_list(self):
        op = operation(groups=1, selected=[0], state='ready')
        plan = json.loads(op['plan'])
        plan['archive_previews'] = True
        op['plan'] = json.dumps(plan)
        op['result'] = json.dumps(operations._summary(plan))
        chooser = cards.render(self.bot, op, 0)
        self.assertIn('1 个视频 + 1 个辅助文件 · 1.1 KiB', text(chooser))
        review = cards.render(self.bot, op)
        self.assertIn('归档范围 2 个文件', text(review))
        self.assertIn('clip-0.MP4', text(review))
        self.assertIn('clip-0.LRV', text(review))
        self.assertNotIn('不归档辅助文件', text(review))

    def test_unknown_companions_remain_in_archive_totals_even_when_previews_excluded(self):
        op = operation(groups=1, selected=[0], state='ready')
        plan = json.loads(op['plan'])
        plan['groups'][0]['members'] += [
            member('DCIM/audio.WAV', 'audio', 1024),
            member('DCIM/unlinked.LRV', 'unlinked', 1024),
        ]
        op['plan'] = json.dumps(plan)
        op['result'] = json.dumps(operations._summary(plan))
        self.assertIn('3 个文件 · 3.0 KiB', text(cards.render(self.bot, op, 0)))
        review = cards.render(self.bot, op)
        self.assertIn('归档范围 3 个文件', text(review))
        self.assertIn('audio.WAV', text(review))
        self.assertIn('unlinked.LRV', text(review))
        self.assertNotIn('clip-0.LRV', text(review))

    def test_review_shows_specific_archive_files_in_first_chapter_order(self):
        op = operation(groups=1, state='ready', selected=[0])
        plan = json.loads(op['plan'])
        plan['groups'][0]['members'] = [
            member('DCIM/GH021414.MP4', 'm2', 2048),
            member('DCIM/GL021414.LRV', 'p2', 128),
            member('DCIM/GH011414.MP4', 'm1', 1024),
            member('DCIM/GL011414.LRV', 'p1', 128),
        ]
        plan['groups'][0]['preview_ids'] = ['p1', 'p2']
        op['plan'] = json.dumps(plan)
        op['result'] = json.dumps(operations._summary(plan))
        rendered = text(cards.render(self.bot, op))
        self.assertIn('归档范围 2 个文件 · 3.0 KiB', rendered)
        self.assertLess(rendered.index('GH011414.MP4'), rendered.index('GH021414.MP4'))
        self.assertNotIn('.LRV', rendered)
        self.assertNotIn('查看完整清单', rendered)
        self.assertIn('不归档辅助文件 2 个', rendered)

    def test_large_review_is_bounded_and_exposes_full_archive_list(self):
        op = operation(state='ready', selected=list(range(13)))
        op['result'] = json.dumps(operations._summary(json.loads(op['plan'])))
        result = cards.render(self.bot, op)
        self.assertIn('归档范围 13 个文件', text(result))
        self.assertIn('已展示前 12 个文件，完整清单共 13 个', text(result))
        self.assertIn('clip-11.MP4', text(result))
        self.assertNotIn('clip-12.MP4', text(result))
        control = next(b for b in buttons(result) if b['text']['content'] == '查看完整清单')
        self.assertEqual('details', control['value']['selected_import']['action'])
        self.assertNotIn('.LRV', text(cards.render(self.bot, op, 1, details=True)))

    def test_invalid_form_values_cannot_select_hidden_or_duplicate_groups(self):
        bad = [[], '', False, {'groups': None}, {'groups': '1,2'}, {'groups': [True]}, {'groups': [1]},
               {'groups': ['1', '1']}, {'groups': ['12']}, {'groups': ['-1']}, {'groups': ['00']},
               {'groups': ['1e3']}, {'groups': ['1'], 'password': 'must-not-be-stored'}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ConfigError):
                cards.parse_choices(value, 0)
        with self.assertRaises(ConfigError):
            cards.parse_choices({'groups': []}, True)

    def test_all_rendered_actions_are_supported_and_do_not_mutate_global_settings(self):
        for state in ('queued', 'scanning', 'choosing', 'ready', 'queued_transfer', 'running',
                      'waiting', 'paused', 'done'):
            op = operation(state=state, selected=[0])
            for b in buttons(cards.render(self.bot, op)):
                with self.subTest(state=state, action=b):
                    self.assertEqual({'selected_import'}, set(b['value']))
                    self.assertTrue(cards.supported(b['value']['selected_import']))
            self.assertNotIn('确认清理原片', text(cards.render(self.bot, op)))
        self.assertTrue(cards.supported({'version': 1, 'action': 'drain', 'id': 'a' * 32}))

    def test_action_schema_rejects_forgery_or_unscoped_commands(self):
        good = {'version': 1, 'action': 'choose', 'id': 'a' * 32, 'page': 0,
                'choice_revision': 3, 'next_page': 1, 'review': False}
        for key, value in [('version', True), ('page', -1), ('page', True), ('next_page', 100001),
                           ('review', 'true'), ('choice_revision', 1.5), ('id', '../../path')]:
            with self.subTest(key=key):
                self.assertFalse(cards.supported({**good, key: value}))
        for value in ({**good, 'enabled': True}, {**good, 'action': 'delete'}, None,
                      {'version': 1, 'action': 'confirm'}, {'version': 1, 'action': 'browse', 'id': 'a' * 32}):
            self.assertFalse(cards.supported(value))

    def test_preview_separates_archive_scope_already_saved_and_excluded_previews(self):
        op = operation(groups=2, state='ready', selected=[0, 1])
        op['result'] = json.dumps({'total_files': 2, 'total_bytes': 2048, 'already_archived': 1,
                                   'excluded_preview_files': 2})
        result = cards.render(self.bot, op)
        self.assertIn('归档范围 2 个文件', text(result))
        self.assertIn('已有归档 1 个', text(result))
        self.assertIn('不归档辅助文件 2 个', text(result))
        self.assertNotIn('首个文件', text(result))
        self.assertIn('原片策略：保留', text(result))
        self.assertIn('clip-0.MP4', text(result))
        self.assertIn('clip-1.MP4', text(result))
        self.assertNotIn('.LRV', text(result))
        confirm = next(b for b in buttons(result) if b['text']['content'] == '确认备份')
        self.assertEqual('primary', confirm['type'])
        self.assertEqual(op['choice_revision'], confirm['value']['selected_import']['choice_revision'])
        self.assertNotIn('delete_source', text(result))

    def test_details_show_paths_as_plain_text_and_paginate_actual_archive_files(self):
        op = operation(selected=list(range(13)))
        plan = json.loads(op['plan'])
        plan['groups'][0]['members'][0]['path'] = 'DCIM/**fake**<at id=all>.MP4'
        op['plan'] = json.dumps(plan)
        result = cards.render(self.bot, op, 0, details=True)
        self.assertEqual('plain_text', next(e['text']['tag'] for e in result['elements'] if 'clip-' in text(e)))
        path = next(e['text'] for e in result['elements'] if 'DCIM/**fake**' in text(e))
        self.assertEqual('plain_text', path['tag'])
        self.assertIn('**fake**<at id=all>', path['content'])
        self.assertNotIn('clip-12.MP4', text(result))
        self.assertNotIn('.LRV', text(result))
        last = cards.render(self.bot, op, 1, details=True)
        self.assertIn('clip-12.MP4', text(last))
        self.assertNotIn('clip-11.MP4', text(last))

    def test_transfer_progress_uses_this_request_and_does_not_claim_published_at_100_bytes(self):
        op = operation(state='running', selected=[0, 1])
        op['result'] = json.dumps({'total_files': 2, 'total_bytes': 2048, 'imported_files': 2,
            'uploaded_files': 1, 'import_bytes': 2048, 'upload_bytes': 2048,
            'stages': {'upload': {'current_file': 'DCIM/clip-1.MP4', 'speed_bytes_per_second': 512,
                                  'eta_seconds': 2}}})
        rendered = text(cards.render(self.bot, op))
        self.assertIn('**导入**', rendered)
        self.assertIn('**99.9%**', rendered)
        self.assertIn('1 / 2', rendered)
        self.assertNotIn('clip-1.MP4', rendered)
        detail = text(cards.render(self.bot, op, details=True))
        self.assertIn('clip-1.MP4', detail)
        self.assertIn('预计剩余 2 秒', detail)

    def test_paused_and_failure_are_not_displayed_as_success(self):
        op = operation(state='paused', selected=[0])
        op['plan'] = json.dumps({**json.loads(op['plan']), 'confirmed': True})
        op['result'] = json.dumps({'total_files': 1, 'total_bytes': 1024, 'imported_files': 1,
                                  'uploaded_files': 0, 'reason': '存储暂不可用'})
        result = cards.render(self.bot, op)
        self.assertEqual('所选文件备份已停止', result['header']['title']['content'])
        self.assertIn('继续本次备份', text(result))
        op['state'] = 'done'
        result = cards.render(self.bot, op)
        self.assertEqual('本次所选文件备份已结束', result['header']['title']['content'])
        self.assertEqual('orange', result['header']['template'])
        self.assertNotIn('所选文件备份已完成', text(result))

    def test_cancelled_draft_cannot_offer_transfer_resume(self):
        op = operation(state='paused', selected=[0])
        op['cancelled'] = 1
        rendered = text(cards.render(self.bot, op))
        self.assertNotIn('继续本次备份', rendered)
        self.assertIn('重新选择文件', rendered)

    def test_completed_archive_reports_cleanup_separately(self):
        op = operation(state='done', selected=[0])
        op['result'] = json.dumps({'total_files': 1, 'total_bytes': 1024, 'imported_files': 1,
                                  'uploaded_files': 1, 'cache_cleanup_pending': 1})
        result = cards.render(self.bot, op)
        self.assertEqual('所选文件备份已完成', result['header']['title']['content'])
        self.assertIn('缓存待清理 1 个', text(result))
        self.assertIn('**1 个文件**', text(result))

    def test_waiting_archive_verification_shows_quantity_and_first_plain_text_failure(self):
        op = operation(state='waiting', selected=[0, 1])
        op['result'] = json.dumps({'total_files': 2, 'total_bytes': 2048,
            'imported_files': 2, 'uploaded_files': 0, 'import_bytes': 2048, 'upload_bytes': 0,
            'archive_verification_pending': 2, 'reason': 'archive_verification',
            'archive_errors': [{'file': 'archive/**clip**.MP4', 'reason': '文件身份与记录不一致'},
                               {'file': 'archive/another.MP4', 'reason': '存储连接中断'}]})
        result = cards.render(self.bot, op)
        self.assertIn('归档待核验：2 个文件', text(result))
        self.assertIn('暂不计为完成', text(result))
        self.assertNotIn('another.MP4', text(result))
        self.assertNotIn('文件身份与记录不一致', text(result))
        details = cards.render(self.bot, op, details=True)
        detail = next(e for e in details['elements'] if '文件身份与记录不一致' in text(e))
        self.assertEqual('plain_text', detail['text']['tag'])
        self.assertEqual('orange', result['header']['template'])
        self.assertNotIn('远端归档　100%', text(result))

    def test_sparse_queued_or_empty_scan_have_no_false_zero_progress(self):
        op = operation(state='queued')
        result = cards.render(self.bot, op)
        self.assertNotIn('0 / 0', text(result))
        self.assertNotIn('已完成', text(result))
        result = cards.render(self.bot, operation(groups=0))
        self.assertEqual('没有可选择的素材', result['header']['title']['content'])
        self.assertFalse(any(item['tag'] == 'form' for item in result['elements']))


class SelectionCallbackTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    callback = test_bot.BotTests.callback
    message = test_bot.BotTests.message

    def browse(self, assets=None):
        source = FakeSource(assets if assets is not None else [footage(index) for index in range(1, 14)])
        service = Service(Config.from_values(self.settings.read()['config']), self.store,
                          source, FakeDestination(), threading.Event())
        self.store.set_meta('source_connection', {'id': 'session-a', 'state': 'approved', 'online': True,
                                                 'last_seen': time.time(), 'device': device()})
        self.sender.return_value = 'om_selected_test'
        self.bot.receive_message(self.message('选择素材'))
        self.assertTrue(self.bot.process_one())
        operations.process_one(service, self.settings)
        with self.bot.db() as db:
            db.execute('UPDATE bot_jobs SET retry_at=0')
        self.assertTrue(self.bot.process_one())
        return service, self.updater.call_args.args[2]

    def test_file_selection_callback_confirms_only_one_chapter(self):
        from test_service import replace
        first, first_preview = footage(), footage(primary=False)
        second = replace(footage(2), path='100GOPRO/GH020001.MP4')
        second_preview = replace(footage(2, primary=False), path='100GOPRO/GL020001.LRV')
        self.settings.update_preferences({'backup': {'archive_previews': False}})
        before = self.path.read_bytes()
        service, card = self.browse([first, first_preview, second, second_preview])
        field = next(e for e in card['elements'] if e['tag'] == 'form')['elements'][0]
        self.assertEqual(2, len(field['options']))
        submit = next(b for b in buttons(card) if b['text']['content'] == '保存并预览')
        event = self.callback(submit['value'], event_id='one-chapter', fields={'groups': ['1']})
        event['event']['context'] = {'open_message_id': 'om_selected_test'}
        self.bot.receive_card(event)
        self.assertTrue(self.bot.process_one())
        preview = self.updater.call_args.args[2]
        self.assertIn('归档范围 1 个文件 · 32.0 B', text(preview))
        self.assertIn(second.path, text(preview))
        self.assertNotIn(first.path, text(preview))
        confirm = next(b for b in buttons(preview) if b['text']['content'] == '确认备份')
        event = self.callback(confirm['value'], event_id='confirm-one-chapter')
        event['event']['context'] = {'open_message_id': 'om_selected_test'}
        self.bot.receive_card(event)
        self.assertTrue(self.bot.process_one())
        self.assertTrue(operations.process_one(service, self.settings))
        with self.bot.db() as db:
            row = dict(db.execute('SELECT * FROM selected_imports').fetchone())
            db.execute('UPDATE bot_jobs SET retry_at=0')
        self.assertEqual('done', row['state'])
        self.assertEqual([second.path], service.source.downloads)
        self.assertFalse(service.source.deletes)
        self.assertEqual(before, self.path.read_bytes())
        self.assertTrue(self.bot.process_one())
        finished = self.updater.call_args.args[2]
        self.assertIn('所选文件备份已完成', text(finished))
        self.assertIn('**1 个文件**', text(finished))

    def test_real_callback_saves_pages_previews_and_deduplicates_delivery(self):
        before = self.path.read_bytes()
        service, first = self.browse()
        next_button = next(b for b in buttons(first) if b['text']['content'] == '保存并到下一页')
        event = self.callback(next_button['value'], event_id='page', fields={'groups': ['0', '1']})
        event['event']['context'] = {'open_message_id': 'om_selected_test'}
        self.assertEqual('info', self.bot.receive_card(event)['toast']['type'])
        self.bot.receive_card(event)
        self.assertTrue(self.bot.process_one())
        self.assertFalse(self.bot.process_one())
        second = self.updater.call_args.args[2]
        self.assertIn('选择文件 · 2/2', text(second))
        preview_button = next(b for b in buttons(second) if b['text']['content'] == '保存并预览')
        event = self.callback(preview_button['value'], event_id='review', fields={'groups': ['12']})
        event['event']['context'] = {'open_message_id': 'om_selected_test'}
        self.bot.receive_card(event)
        self.assertTrue(self.bot.process_one())
        result = self.updater.call_args.args[2]
        self.assertEqual('确认文件备份', result['header']['title']['content'])
        with self.bot.db() as db:
            row = dict(db.execute('SELECT * FROM selected_imports').fetchone())
        self.assertEqual([0, 1, 12], json.loads(row['plan'])['selected'])
        self.assertEqual(2, row['choice_revision'])
        self.assertEqual('ready', row['state'])
        self.assertFalse(service.source.downloads or service.source.deletes)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(self.store.files())

    def test_rejected_form_is_not_persisted_and_other_account_cannot_choose(self):
        _, result = self.browse()
        submit = next(b for b in buttons(result) if b['text']['content'] == '保存并预览')
        for event in (self.callback(submit['value'], fields={'groups': ['12']}),
                      self.callback(submit['value'], fields={'groups': ['0']}, owner='ou_other')):
            self.assertEqual('error', self.bot.receive_card(event)['toast']['type'])
        with self.bot.db() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM bot_jobs').fetchone()[0])
            row = dict(db.execute('SELECT * FROM selected_imports').fetchone())
        self.assertEqual([], json.loads(row['plan'])['selected'])
        self.assertEqual('choosing', row['state'])

    def test_empty_client_form_clears_visible_page_and_keeps_other_saved_pages(self):
        self.clear_visible_page({})

    def test_omitted_client_form_clears_visible_page_and_keeps_other_saved_pages(self):
        self.clear_visible_page(None)

    def clear_visible_page(self, fields):
        service, first = self.browse()
        next_button = next(b for b in buttons(first) if b['text']['content'] == '保存并到下一页')
        self.bot.receive_card(self.callback(next_button['value'], event_id='select-page-1', fields={'groups': ['0']}))
        self.assertTrue(self.bot.process_one())
        second = self.sender.call_args.args[1]
        previous = next(b for b in buttons(second) if b['text']['content'] == '保存并到上一页')
        self.bot.receive_card(self.callback(previous['value'], event_id='select-page-2', fields={'groups': ['12']}))
        self.assertTrue(self.bot.process_one())
        first_again = self.sender.call_args.args[1]
        next_button = next(b for b in buttons(first_again) if b['text']['content'] == '保存并到下一页')
        # The real client omits this optional field after every choice is cleared.
        event = self.callback(next_button['value'], event_id='clear-page-1', fields=fields)
        if fields is None:
            event['event']['action'].pop('form_value')
        response = self.bot.receive_card(event)
        self.assertEqual('info', response['toast']['type'])
        self.assertTrue(self.bot.process_one())
        with self.bot.db() as db:
            row = dict(db.execute('SELECT * FROM selected_imports').fetchone())
        self.assertEqual([12], json.loads(row['plan'])['selected'])
        self.assertEqual('choosing', row['state'])
        self.assertFalse(service.source.downloads or service.source.deletes or self.store.files())

    def test_initial_empty_preview_remains_unconfirmed_and_reports_select_at_least_one(self):
        self.empty_preview({})

    def test_initial_none_preview_remains_unconfirmed_and_reports_select_at_least_one(self):
        self.empty_preview(None)

    def empty_preview(self, fields):
        service, result = self.browse()
        submit = next(b for b in buttons(result) if b['text']['content'] == '保存并预览')
        event = self.callback(submit['value'], event_id='empty-preview', fields=fields)
        event['event']['action']['form_value'] = fields
        response = self.bot.receive_card(event)
        self.assertEqual('info', response['toast']['type'])
        self.assertTrue(self.bot.process_one())
        self.assertIn('请至少选择一个', text(self.sender.call_args.args[1]))
        with self.bot.db() as db:
            row = dict(db.execute('SELECT * FROM selected_imports').fetchone())
        self.assertEqual([], json.loads(row['plan'])['selected'])
        self.assertEqual('choosing', row['state'])
        self.assertFalse(service.source.downloads or service.source.deletes or self.store.files())

    def test_failed_card_update_retries_same_saved_selection_without_reapplying(self):
        _, result = self.browse()
        submit = next(b for b in buttons(result) if b['text']['content'] == '保存并预览')
        event = self.callback(submit['value'], event_id='retry-form', fields={'groups': ['0']})
        event['event']['context'] = {'open_message_id': 'om_selected_test'}
        self.bot.receive_card(event)
        self.updater.side_effect = [OSError('isolated network failure'), None]
        self.assertFalse(self.bot.process_one())
        with self.bot.db() as db:
            row = dict(db.execute('SELECT * FROM selected_imports').fetchone())
            self.assertEqual(1, row['choice_revision'])
            self.assertEqual('ready', row['state'])
            db.execute("UPDATE bot_jobs SET retry_at=0 WHERE id='card:retry-form'")
        self.assertTrue(self.bot.process_one())
        with self.bot.db() as db:
            row = dict(db.execute('SELECT * FROM selected_imports').fetchone())
            self.assertEqual(1, row['choice_revision'])
        self.assertEqual('确认文件备份', self.updater.call_args.args[2]['header']['title']['content'])

    def test_old_preview_confirmation_cannot_start_a_newer_selection(self):
        service, first = self.browse()
        submit = next(b for b in buttons(first) if b['text']['content'] == '保存并预览')
        self.bot.receive_card(self.callback(submit['value'], event_id='first-choice', fields={'groups': ['0']}))
        self.assertTrue(self.bot.process_one())
        old_preview = self.sender.call_args.args[1]
        old_confirm = next(b for b in buttons(old_preview) if b['text']['content'] == '确认备份')
        with self.bot.db() as db:
            op = dict(db.execute('SELECT * FROM selected_imports').fetchone())
        edit = cards.render(self.bot, op, page=0)
        submit = next(b for b in buttons(edit) if b['text']['content'] == '保存并预览')
        self.bot.receive_card(self.callback(submit['value'], event_id='changed-choice', fields={'groups': ['1']}))
        self.assertTrue(self.bot.process_one())
        current_preview = self.sender.call_args.args[1]
        current_confirm = next(b for b in buttons(current_preview) if b['text']['content'] == '确认备份')
        self.bot.receive_card(self.callback(old_confirm['value'], event_id='stale-confirm'))
        self.assertTrue(self.bot.process_one())
        with self.bot.db() as db:
            op = dict(db.execute('SELECT * FROM selected_imports').fetchone())
        self.assertEqual('ready', op['state'])
        self.assertEqual([1], json.loads(op['plan'])['selected'])
        self.assertIn('选择', text(self.sender.call_args.args[1]))
        self.assertFalse(service.source.downloads or service.source.deletes)
        self.bot.receive_card(self.callback(current_confirm['value'], event_id='current-confirm'))
        self.assertTrue(self.bot.process_one())
        with self.bot.db() as db:
            op = dict(db.execute('SELECT * FROM selected_imports').fetchone())
        self.assertEqual('queued_transfer', op['state'])
        self.assertEqual([1], json.loads(op['plan'])['selected'])


if __name__ == '__main__':
    unittest.main()
