"""Reader-facing truth and consistency across query, event and control cards."""
import copy
import json
import unittest

from media_importer.bot_cards import event_card, progress_card, settings_result, status_content
from test_bot_cards import view


def text(value):
    return json.dumps(value, ensure_ascii=False)


class MessageContractTests(unittest.TestCase):
    def test_parallel_work_has_one_consistent_status_in_query_and_progress(self):
        state = view()
        state['stages']['import']['phase'] = 'downloading'
        state['stages']['upload']['phase'] = 'uploading'
        title, color, _ = status_content(state)
        self.assertEqual('导入与归档进行中', title)
        self.assertEqual('blue', color)
        self.assertEqual(title, progress_card(state, 300)['header']['title']['content'])

    def test_completed_import_count_overrides_leftover_download_phase(self):
        state = view()
        state['batch']['imported_files'] = 2
        state['stages']['import']['phase'] = 'downloading'
        title, _, content = status_content(state)
        self.assertNotIn('导入进行中', title)
        self.assertIn('2 / 2', text(content))
        self.assertNotIn('可断开', text(content))

    def test_stale_disconnect_permission_cannot_be_presented_as_current(self):
        for running, stale, online in ((False, False, True), (True, True, True), (True, False, False)):
            state = view()
            state['running'] = running
            state['health']['source'].update(stale=stale, online=online)
            state['unplug'] = {'ready': True, 'can_disconnect': True}
            for message in (status_content(state), progress_card(state, 300), event_card('imported', '', '', state)):
                self.assertNotIn('可断开', text(message))

    def test_byte_rounding_never_claims_one_hundred_percent_early(self):
        state = view()
        state['batch']['percent'] = 99.99
        state['stages']['upload']['phase'] = 'uploading'
        rendered = text(progress_card(state, 300))
        self.assertIn('99.9%', rendered)
        self.assertNotIn('100%', rendered)

    def test_disabled_notifications_do_not_claim_sync_has_stopped(self):
        state = view()
        state['stages']['upload']['phase'] = 'uploading'
        card = progress_card(state, 300, notifications_enabled=False)
        self.assertEqual('远端归档进行中', card['header']['title']['content'])
        self.assertNotIn('自动进度更新', text(card))
        self.assertNotIn('备份已停止', text(card))

    def test_stopping_has_priority_over_complete_but_only_ack_confirms_stop(self):
        state = view()
        state['configuration']['sync_enabled'] = False
        state['batch'].update(completed_files=2, imported_files=2)
        state['controls']['import']['state'] = 'pausing'
        self.assertEqual('正在停止备份', status_content(state)[0])
        response = settings_result({'backup': {'enabled': False}}, True, True)
        self.assertNotIn('均已停止', response['detail'])
        response = settings_result({'backup': {'enabled': False}}, True, False)
        self.assertEqual('备份已停止', response['title'])

    def test_empty_batch_with_durable_work_is_not_reported_as_empty(self):
        state = view()
        state['batch'].update(total_files=0, scanned=True)
        state['work']['pending_files'] = 2
        content = text(status_content(state))
        self.assertNotIn('无待处理任务', content)
        self.assertIn('已登记任务', content)

    def test_result_facts_survive_a_new_empty_or_unrelated_round(self):
        state = view()
        state['batch'].update(total_files=99, total_bytes=999, completed_files=0)
        result = {'file_count': 2, 'byte_count': 1024, 'ignored_preview_files': 2,
                  'cleanup': {'source_deleted': 2, 'cache_cleared': 2, 'preview_deleted': 2}}
        before = copy.deepcopy(result)
        card = event_card('completed', 'old title', 'old text', state, result=result,
                          locations=['smb://storage.example.com/media/Camera'])
        rendered = text(card)
        self.assertIn('**2 个文件**', rendered)
        self.assertNotIn('99', rendered)
        self.assertNotIn('预览文件：', rendered)
        self.assertIn('原片已删除 4 个', rendered)
        self.assertNotIn('smb://', rendered)
        self.assertEqual(before, result)

    def test_archive_complete_with_cleanup_failure_keeps_archive_success(self):
        result = {'file_count': 2, 'byte_count': 1024, 'cleanup': {'cache_cleared': 1, 'cache_pending': 1}}
        card = event_card('completed', '', '', view(), result=result)
        self.assertEqual('归档完成', card['header']['title']['content'])
        self.assertEqual('orange', card['header']['template'])
        self.assertIn('缓存待清理 1 个', text(card))
        self.assertIn('**2 个文件**', text(card))

    def test_import_completion_is_specific_and_does_not_claim_archive_completion(self):
        state = view()
        state['batch'].update(imported_files=2, import_percent=100)
        state['unplug'].update(ready=True)
        card = event_card('imported', '', '', state)
        self.assertEqual('导入完成', card['header']['title']['content'])
        self.assertEqual('blue', card['header']['template'])
        self.assertIn('待归档 2 个', text(card))
        self.assertIn('相机读取已结束', text(card))

    def test_disconnection_does_not_assert_physical_unplug_or_active_upload(self):
        state = view()
        card = event_card('disconnected', '', '', state)
        self.assertEqual('相机已断开', card['header']['title']['content'])
        self.assertNotIn('上传中', text(card))
        self.assertNotIn('已拔出', text(card))

    def test_titles_are_formal_and_complete_without_emoji_or_colloquial_instructions(self):
        state = view()
        messages = [progress_card(state, 300)]
        messages += [event_card(kind, title, '说明', state) for kind, title in (
            ('connected', '相机已连接'), ('disconnected', '相机已断开'),
            ('completed', '归档完成'), ('imported', '本地导入完成'),
            ('failed', '存储认证失败'), ('recovered', '存储连接已恢复'),
            ('backlog', '归档等待恢复'), ('test', '通知连接正常'))]
        for card in messages:
            title = card['header']['title']['content']
            self.assertFalse(any(ord(c) > 0xffff for c in title))
            for wording in ('拔走', '拔线', '收齐', '导完', '下面'):
                self.assertNotIn(wording, text(card))
