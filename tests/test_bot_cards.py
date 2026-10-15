import copy
import json
import unittest

from media_importer.bot_cards import settings_result, status_content, progress_bar, progress_card, event_card


def view():
    return {'running': True, 'configuration': {'sync_enabled': True},
            'health': {'source': {'online': True}, 'destination': {'online': True}, 'cache': {'available': True}},
            'disk': {'free_bytes': 100 * 1024**3, 'cache_used_bytes': 1024**3, 'quota_bytes': 50 * 1024**3},
            'controls': {name: {'state': 'running'} for name in ('import', 'upload')},
            'stages': {name: {'phase': 'idle'} for name in ('import', 'upload')},
            'queues': {name: {'pending_files': 2, 'remaining_bytes': 512} for name in ('import', 'upload')},
            'batch': {'scanned': True, 'total_files': 2, 'total_bytes': 1024, 'completed_files': 0,
                      'imported_files': 1, 'import_percent': 50., 'percent': 25.},
            'totals': {'pending_cleanup': 0, 'cache_cleanup_pending': 0},
            'unplug': {'ready': False, 'message': '导入尚未完成'}, 'policy': {'delete_source': False},
            'issues': [], 'temporary_cleanup': [], 'observed_at': '2026-10-15T12:00:00+00:00'}


class CommandCardTests(unittest.TestCase):
    def test_paused_card_keeps_completed_counts_but_hides_old_live_speed(self):
        state=view(); state['configuration']['sync_enabled']=False
        for name in state['controls']:
            state['controls'][name]['state']='paused'
            state['stages'][name]={'phase':'paused', 'current_file':'old.mp4', 'speed_bytes_per_second':99999}
        before=copy.deepcopy(state)
        title,color,elements=status_content(state)
        text=json.dumps(elements,ensure_ascii=False)
        self.assertEqual('grey',color)
        self.assertIn('同步已关闭',title)
        self.assertIn('1 / 2',text)
        self.assertNotIn('old.mp4',text)
        self.assertNotIn('/s',text)
        self.assertEqual(before,state)

    def test_stale_device_and_unknown_space_are_not_shown_as_healthy_or_zero(self):
        state=view(); state['health']['source'].update(stale=True)
        state['health']['destination'].update(online=None)
        state['disk']['free_bytes']=None
        text=json.dumps(status_content(state),ensure_ascii=False)
        self.assertIn('相机 待更新',text)
        self.assertIn('目的地 待更新',text)
        self.assertNotIn('磁盘可用 0',text)

    def test_upload_bytes_are_not_mistaken_for_committed_archive(self):
        state=view(); state['batch']['percent']=100.
        state['stages']['upload']={'phase':'publishing','current_file':'100MEDIA/a.mp4','current_bytes':512,'current_total_bytes':512}
        title,_,elements=status_content(state)
        text=json.dumps(elements,ensure_ascii=False)
        self.assertNotIn('已归档',title)
        self.assertIn('0 / 2',text)
        self.assertIn('确认归档',text)

    def test_unscanned_card_does_not_show_zero_of_zero(self):
        state=view(); state['batch'].update(total_files=0,scanned=False)
        text=json.dumps(status_content(state),ensure_ascii=False)
        self.assertIn('尚未扫描',text)
        self.assertNotIn('0 / 0',text)

    def test_file_names_and_errors_never_become_markdown(self):
        state=view()
        payload='<at id=all>click</at> [fake](https://example.invalid)'
        state['stages']['upload']={'phase':'uploading','current_file':payload,'current_bytes':128,'current_total_bytes':512,'speed_bytes_per_second':64,'eta_seconds':6}
        state['issues']=[{'path':'a.mp4','error':payload}]
        _,_,elements=status_content(state)
        text=json.dumps(elements,ensure_ascii=False)
        self.assertIn('本文件剩余',text)
        for element in elements:
            if payload in json.dumps(element,ensure_ascii=False):
                self.assertEqual('plain_text',element['text']['tag'])

    def test_save_is_not_reported_as_applied_before_acknowledgment(self):
        result=settings_result({'backup':{'enabled':False}},False,False)
        self.assertEqual('warning',result['level'])
        self.assertIn('等待生效',result['title'])
        result=settings_result({'backup':{'enabled':False}},True,True)
        self.assertEqual('正在停止同步',result['title'])
        result=settings_result({'backup':{'enabled':False}},True,False)
        self.assertEqual('同步已关闭',result['title'])

    def test_two_byte_bars_share_the_same_rendering_in_status_and_notifications(self):
        state = view()
        for content in (status_content(state), progress_card(state, 300)):
            text = json.dumps(content, ensure_ascii=False)
            self.assertIn('█████░░░░░', text)
            self.assertIn('██░░░░░░░░', text)
            self.assertIn('**💾 导入到缓存 · 50.0%**', text)
            self.assertIn('**📁 归档到存储 · 25.0%**', text)
            self.assertIn('**0 / 2**', text)
        self.assertTrue(progress_card(state, 300)['config']['update_multi'])

    def test_bar_clamps_values_and_does_not_fill_early(self):
        self.assertEqual('░' * 10, progress_bar(-10)[0])
        self.assertEqual('█' * 9 + '░', progress_bar(99.99)[0])
        self.assertEqual('█' * 10, progress_bar(150)[0])
        self.assertEqual(0, progress_bar(float('nan'))[1])

    def test_progress_card_stop_and_completion_are_distinct(self):
        state = view(); state['configuration']['sync_enabled'] = False
        stopped = progress_card(state, 300)
        self.assertEqual('grey', stopped['header']['template'])
        self.assertIn('自动进度更新已停止', json.dumps(stopped, ensure_ascii=False))
        state['configuration']['sync_enabled'] = True
        state['batch'].update(percent=100, completed_files=2)
        self.assertEqual('green', progress_card(state, 300)['header']['template'])

    def test_completion_location_and_failure_text_are_plain_not_executable_markup(self):
        payload = '<at id=all>unsafe</at> [link](https://example.invalid)'
        for message in (event_card('failed', payload, payload, view()),
                        event_card('completed', '归档完成', '已正式归档 2 个文件', view(), locations=[payload])):
            for element in message['elements']:
                if payload in json.dumps(element):
                    self.assertEqual('plain_text', element['text']['tag'])
