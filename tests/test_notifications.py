"""No external sends: exercise durable notification decisions independently of the delivery protocol."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from media_importer.adapters import feishu
from media_importer.model import BackupError, ConfigError
from media_importer.notifications import NotificationWorker, send_test, _digest
from media_importer.store import Store
from test_service import configuration, footage


CREDENTIALS = {"app_id": "cli_test12345678", "app_secret": "only-test-secret",
               "receive_id_type": "email", "receive_id": "recipient@example.invalid"}


class WorkerTests(unittest.TestCase):
    def test_source_network_failure_is_delayed_and_recovers_as_a_connection_issue(self):
        from media_importer.issues import describe
        from media_importer.model import SourceConnectionError
        issue = describe(SourceConnectionError('timeout'), 'import')
        self.view['stages'] = {'import': {'phase': 'waiting_source', 'issue': issue},
                               'upload': {'phase': 'idle'}}
        self.view['health']['source'] = {'online': False, 'issue': issue}
        self.tick()
        self.tick(119)
        self.sender.assert_not_called()
        self.tick(1)
        self.assertEqual(1, self.sender.call_count)
        message = json.dumps(self.sender.call_args.args[1], ensure_ascii=False)
        self.assertIn('相机连接暂时不可用', message)
        self.assertIn('完整缓存继续上传', message)
        self.assertNotIn('文件传输失败', message)
        self.tick(30)
        self.assertEqual(1, self.sender.call_count)
        self.view['health']['source'] = {'online': True}
        self.view['stages']['import'] = {'phase': 'idle'}
        self.tick(1)
        self.assertTrue(any('已恢复' in str(call) for call in self.updater.call_args_list))

    def test_missing_or_unverified_complete_cache_is_actionable(self):
        self.view['files'] = [{'id': self.rows[0]['id'], 'state': 'cached', 'cache_state': 'unverified'}]
        self.tick()
        self.assertIn('完整缓存需要核验', str(self.sender.call_args))
        self.view['files'][0].update(cache_state='verified', cache_available=True)
        self.tick(1)
        self.assertTrue(any('缓存核验已通过' in str(call) for call in self.updater.call_args_list))

    def test_recovery_never_patches_a_different_recipients_message(self):
        self.view['health']['destination'] = {'online': False, 'issue': {'code': 'network'}}
        self.tick()
        self.tick(121)
        self.assertEqual(1, self.sender.call_count)
        credentials = copy.deepcopy(CREDENTIALS)
        credentials['receive_id'] = 'another@example.invalid'
        self.settings.notification_credentials.return_value = credentials
        self.view['health']['destination'] = {'online': True}
        self.tick(1)
        self.assertFalse(any('已恢复' in str(call) for call in self.updater.call_args_list))

    def test_backlog_alert_once_per_stall_and_recovery_updates_original_card(self):
        self.view['backlog'] = {'files': 2, 'bytes': 32, 'source_removed_files': 1, 'verified_files': 2}
        self.view['files'] = [{'id': r['id'], 'state': 'cached'} for r in self.rows]
        self.view['stages'] = {'import': {'phase': 'idle'}, 'upload': {'phase': 'idle'}}
        self.tick()
        self.tick(6 * 3600)
        self.assertEqual(1, self.sender.call_count)
        self.assertIn('缓存长时间未归档', str(self.sender.call_args))
        self.tick(3600)
        self.assertEqual(1, self.sender.call_count)
        self.worker = self.make_worker()
        self.tick(3600)
        self.assertEqual(1, self.sender.call_count)
        self.view['stages']['upload'].update(phase='uploading', current_file_id=self.rows[0]['id'], current_bytes=8)
        self.tick(1)
        self.tick(1)
        self.assertTrue(any('已恢复' in str(call) for call in self.updater.call_args_list))

    def test_old_bytes_retried_are_not_progress_and_stop_does_not_alert(self):
        self.view['backlog'] = {'files': 1, 'bytes': 16, 'verified_files': 1}
        self.view['files'] = [{'id': self.rows[0]['id'], 'state': 'cached'}]
        self.view['stages'] = {'upload': {'phase': 'uploading', 'current_file_id': self.rows[0]['id'], 'current_bytes': 8}}
        self.tick()
        self.view['stages']['upload']['current_bytes'] = 0
        self.tick(3 * 3600)
        self.view['stages']['upload']['current_bytes'] = 8
        self.tick(3 * 3600)
        self.tick(1)
        self.assertTrue(any('缓存长时间未归档' in str(call) for call in self.sender.call_args_list))
        self.values['backup']['enabled'] = False
        self.sender.reset_mock()
        self.tick(24 * 3600)
        self.sender.assert_not_called()

    def test_retrying_a_failed_file_does_not_claim_recovery_before_archive(self):
        item = {'id': self.rows[0]['id'], 'path': 'clip.mp4', 'state': 'cached', 'error': 'failed', 'error_stage': 'upload'}
        self.view['files'] = [item]
        self.view['stages']['upload'] = {'phase': 'failed', 'issue': {'code': 'file', 'scope': 'file'}}
        self.tick()
        item['error'] = None
        self.view['stages']['upload'] = {'phase': 'uploading', 'current_file_id': item['id'], 'current_bytes': 5}
        self.tick(1)
        self.assertFalse(any('已恢复' in str(call) for call in self.updater.call_args_list))
        item['state'] = 'uploaded'
        self.tick(1)
        self.assertTrue(any('相关文件已处理成功' in str(call) for call in self.updater.call_args_list))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.store = Store(self.directory)
        self.config = configuration(delete_source_after_import=False)
        self.values = copy.deepcopy(self.config.values)
        self.values["notifications"].update(enabled=True)
        self.settings = Mock()
        self.settings.read.side_effect = lambda: {"config": copy.deepcopy(self.values), "error": None}
        self.settings.notification_credentials.return_value = CREDENTIALS
        self.stop, self.sender = threading.Event(), Mock(return_value="om_testprogress")
        self.updater = Mock(return_value="om_testprogress")
        self.time = 1000.0
        self.batch = self.store.batch(self.config.destination_id, [footage(1), footage(2)])
        self.rows = self.store.files(batch_id=self.batch)
        self.view = {
            "running": True, "configuration": {"pending": False}, "status": {"phase": "uploading"},
            "batch": {"id": self.batch, "total_files": 2, "completed_files": 0, "imported_files": 1,
                      "percent": 0, "import_percent": 50},
            "controls": {"import": {"requested_stop": False}, "upload": {"requested_stop": False}},
            "stages": {"import": {"phase": "downloading", "current_bytes": 0},
                       "upload": {"phase": "uploading", "current_bytes": 0}},
            "queues": {"import": {"pending_files": 1}, "upload": {"pending_files": 2}},
            "health": {"source": {"online": True}, "destination": {"online": True}, "cache": {"available": True}},
            "policy": {"delete_source": False, "cleanup_cache": True},
            "files": [], "totals": {"pending_files": 2},
        }
        self.worker = self.make_worker()
        # These cases exercise an already armed worker. Startup is covered below.
        self.store.set_meta('notification_state', {'scope': _digest([self.config.destination_id,
            self.values['source']['type'], self.values['source']['serial']]), 'armed_at': self.time,
            'progress_at': self.time, 'progress_signature': self.worker._progress(self.view)[0], 'problems': {}})

    def make_worker(self):
        return NotificationWorker(self.directory, self.settings, self.stop, sender=self.sender, updater=self.updater, clock=lambda: self.time)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def tick(self, seconds=0):
        self.time += seconds
        self.worker.tick(copy.deepcopy(self.view))

    def archive(self, rows=None, cleanup="pending"):
        observations = {f['id']: f for f in self.view['files']}
        for row in rows or self.rows:
            self.store.update(row["id"], state="uploaded", cleanup=cleanup)
            observations[row['id']] = {'id': row['id'], 'cache_state': 'missing', 'state': 'uploaded', 'cleanup': cleanup}
        self.view['files'] = list(observations.values())

    def test_historical_completion_is_suppressed_including_first_enable(self):
        self.archive()
        self.values["notifications"]["enabled"] = False
        self.tick()
        self.values["notifications"]["enabled"] = True
        self.tick(10)
        self.worker = self.make_worker()
        self.tick(10)
        self.assertEqual(1, self.sender.call_count)  # Immediate start card, no historic completion alert.
        self.assertNotIn('归档完成', self.sender.call_args.args[1]['header']['title']['content'])

    def test_short_batch_finishing_between_disable_and_first_enabled_tick_not_lost(self):
        self.values["backup"]["enabled"] = False
        self.tick()
        self.values["backup"]["enabled"] = True
        self.archive()
        self.tick(5)
        self.tick(5)  # Immediate progress precedes the separately delivered completion.
        self.assertEqual(2, self.sender.call_count)
        self.assertIn("归档完成", self.sender.call_args.args[1]["header"]["title"]["content"])

    def test_first_install_already_enabled_does_not_send_historical_completion(self):
        self.archive()
        self.tick()
        self.tick(5)
        self.sender.assert_not_called()

    def test_completion_is_formal_archive_not_percentage_and_survives_restart(self):
        self.tick()
        self.view["batch"].update(percent=100, completed_files=2)
        self.tick(2)
        self.sender.assert_not_called()
        self.archive()
        self.tick(2)
        self.assertEqual(1, self.sender.call_count)
        self.assertIn("归档完成", self.sender.call_args.args[1]["header"]["title"]["content"])
        self.assertIn("未执行删除 2 个", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.worker = self.make_worker()
        self.tick(2)
        self.assertEqual(1, self.sender.call_count)

    def test_empty_batches_do_not_report_completion(self):
        self.store.batch(self.config.destination_id, [])
        self.tick()
        self.tick(10)
        self.sender.assert_not_called()

    def test_new_scan_joins_unfinished_round_and_cannot_report_partial_completion(self):
        self.tick()
        newer = self.store.batch(self.config.destination_id, [footage(3)])
        self.assertNotEqual(self.batch, newer)
        self.assertEqual(self.batch, self.store.reporting_run(self.config))
        self.archive()
        self.tick(2)
        self.sender.assert_not_called()
        self.archive(self.store.files(batch_id=newer))
        self.view['batch'].update(total_files=3, completed_files=3)
        self.tick(2)
        self.assertEqual(1, self.sender.call_count)
        self.assertIn('3 个文件', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))

    def test_completion_waits_for_source_cleanup_then_reports_pending_honestly(self):
        self.tick()
        self.archive(cleanup="blocked")
        self.view["policy"]["delete_source"] = True
        self.view["status"]["source_work"] = True
        self.view["status"]["phase"] = "checking_cleanup"
        self.tick(1)
        self.sender.assert_not_called()
        self.view["status"]["source_work"] = False
        self.view["status"]["phase"] = "completed"
        self.store.update(self.rows[0]["id"], cache_cleanup_error="test cleanup failure")
        # File cleanup faults are separate from archive completion, each sent once.
        self.tick(1)
        self.tick(1)
        self.tick(1)
        self.assertIn("归档完成", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.assertIn("原片：清理待处理 2", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.assertIn("缓存：已清理 1 个 · 清理待处理 1", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))

    def test_progress_requires_interval_change_and_active_transfer(self):
        self.tick()
        self.tick(301)
        self.sender.assert_not_called()
        self.view["stages"]["upload"]["current_bytes"] = 10
        self.view["batch"]["percent"] = 25
        self.tick(1)
        self.assertEqual(1, self.sender.call_count)
        self.assertIn("1 / 2", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.assertIn("0 / 2", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.assertIn("25.0%", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.tick(301)
        self.assertEqual(1, self.sender.call_count)
        self.view["stages"]["upload"].update(current_bytes=20, phase="paused")
        self.view["stages"]["import"]["phase"] = "paused"
        self.tick(1)
        self.assertEqual(1, self.sender.call_count)

    def test_network_alert_delayed_and_deduplicated_until_recovery(self):
        self.tick()
        self.view["health"]["destination"] = {"online": False, "issue": {"code": "network"}}
        self.view["stages"]["upload"]["phase"] = "waiting_destination"
        self.tick(1)
        self.tick(119)
        self.sender.assert_not_called()
        self.tick(1)
        self.assertEqual(1, self.sender.call_count)
        self.worker = self.make_worker()
        self.tick(120)
        self.assertEqual(1, self.sender.call_count)
        self.view["health"]["destination"]["online"] = True
        self.tick(1)
        self.view["health"]["destination"]["online"] = False
        self.tick(1)
        self.tick(120)
        self.assertEqual(2, self.sender.call_count)

    def test_authentication_alert_is_immediate_and_does_not_expose_raw_error(self):
        self.tick()
        self.view["stages"]["upload"].update(phase="waiting_destination", issue={"code": "authentication", "scope": "destination", "message": CREDENTIALS["app_secret"]})
        self.tick(1)
        self.assertIn("认证失败", self.sender.call_args.args[1]["header"]["title"]["content"])
        self.assertNotIn(CREDENTIALS["app_secret"], str(self.sender.call_args.args[1:]))

    def test_paused_stage_and_configuration_switch_are_not_failure(self):
        self.tick()
        self.view["stages"]["upload"].update(phase="waiting_destination", issue={"code": "permission", "scope": "destination"})
        self.view["controls"]["upload"]["requested_stop"] = True
        self.tick(1)
        self.sender.assert_not_called()
        self.view["controls"]["upload"]["requested_stop"] = False
        self.view["configuration"]["pending"] = True
        self.tick(1)
        self.sender.assert_not_called()

    def test_source_disconnect_after_import_is_not_a_failure(self):
        self.tick()
        self.view["health"]["source"]["online"] = False
        self.view["queues"]["import"]["pending_files"] = 0
        self.tick(121)
        self.tick(121)
        self.sender.assert_not_called()

    def test_old_issue_and_file_error_are_not_reported_during_active_retry(self):
        self.tick()
        self.view["stages"]["upload"].update(phase="uploading", current_file_id=self.rows[0]["id"],
                                              issue={"code": "network", "scope": "destination"})
        self.view["files"] = [{"id": self.rows[0]["id"], "state": "cached", "error": "old error", "error_stage": "upload"}]
        self.tick(1)
        self.tick(121)
        self.sender.assert_not_called()
        self.view["files"].append({"id": self.rows[1]["id"], "state": "cached", "error": "other file still failed", "error_stage": "upload"})
        self.tick(1)
        self.assertEqual(1, self.sender.call_count)
        self.assertIn("文件传输失败", self.sender.call_args.args[1]["header"]["title"]["content"])

    def test_delivery_failure_is_safe_backed_off_and_persistent(self):
        self.tick()
        self.archive()
        self.sender.side_effect = RuntimeError(CREDENTIALS["receive_id"] + CREDENTIALS["app_secret"])
        self.tick(1)
        first_event_id = self.sender.call_args.kwargs["event_id"]
        delivery = self.store.get_meta("notification_delivery")
        self.assertEqual("retrying", delivery["state"])
        self.assertEqual(1, delivery["pending_count"])
        self.assertTrue(delivery["next_retry_at"])
        self.assertNotIn(CREDENTIALS["app_secret"], json.dumps(delivery))
        self.worker = self.make_worker()
        self.tick(29)
        self.assertEqual(1, self.sender.call_count)
        self.sender.side_effect = None
        self.tick(1)
        self.assertEqual(2, self.sender.call_count)
        self.assertEqual(first_event_id, self.sender.call_args.kwargs["event_id"])
        self.assertEqual("sent", self.store.get_meta("notification_delivery")["state"])

    def test_disabled_sync_cancels_pending_automatic_delivery(self):
        self.tick()
        self.archive()
        self.sender.side_effect = RuntimeError("offline")
        self.tick(1)
        self.values["backup"]["enabled"] = False
        self.tick(100)
        self.values["backup"]["enabled"] = True
        self.sender.side_effect = None
        self.tick(100)
        self.assertEqual(2, self.sender.call_count)  # Retry cancelled; a fresh start card is sent.

    def test_enable_sends_immediate_card_while_waiting_without_a_batch(self):
        self.store.set_meta('notification_state', {})
        self.view['batch'] = {'scanned': False}
        self.view['stages'] = {'import': {'phase': 'waiting_source'}, 'upload': {'phase': 'idle'}}
        self.tick()
        self.assertEqual(1, self.sender.call_count)
        self.assertIn('尚未扫描', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.tick(5)
        self.assertEqual(1, self.sender.call_count)
        self.view['batch'] = {'id': self.batch, 'scanned': True, 'total_files': 2, 'completed_files': 0, 'imported_files': 0}
        self.tick(5)
        self.assertEqual(1, self.sender.call_count)
        self.assertEqual('om_testprogress', self.updater.call_args.args[1])

    def test_initial_card_retries_while_waiting_and_survives_restart(self):
        self.store.set_meta('notification_state', {})
        self.view['stages'] = {'import': {'phase': 'waiting_source'}, 'upload': {'phase': 'idle'}}
        self.sender.side_effect = feishu.NotificationDeliveryError('notification_network')
        self.tick()
        identity = self.sender.call_args.kwargs['event_id']
        self.worker = self.make_worker()
        self.tick(5)
        self.assertEqual(1, self.sender.call_count)
        self.sender.side_effect = None
        self.tick(25)
        self.assertEqual(2, self.sender.call_count)
        self.assertEqual(identity, self.sender.call_args.kwargs['event_id'])

    def test_disabled_notifications_never_send_initial_card(self):
        self.store.set_meta('notification_state', {})
        self.values['notifications']['enabled'] = False
        self.tick()
        self.tick(301)
        self.sender.assert_not_called()

    def test_initial_card_waits_for_configuration_application_without_being_lost(self):
        self.store.set_meta('notification_state', {})
        self.view['configuration']['pending'] = True
        self.tick()
        self.sender.assert_not_called()
        self.view['configuration']['pending'] = False
        self.tick(5)
        self.sender.assert_called_once()

    def test_unknown_initial_send_keeps_deduplication_id_after_scan(self):
        self.store.set_meta('notification_state', {})
        self.view['batch'] = {'scanned': False}
        self.view['stages'] = {'import': {'phase': 'waiting_source'}, 'upload': {'phase': 'idle'}}
        self.sender.side_effect = feishu.NotificationDeliveryError('notification_network')
        self.tick()
        identity = self.sender.call_args.kwargs['event_id']
        self.view['batch'] = {'id': self.batch, 'scanned': True, 'total_files': 2, 'completed_files': 0}
        self.sender.side_effect = None
        self.tick(30)
        self.assertEqual(2, self.sender.call_count)
        self.assertEqual(identity, self.sender.call_args.kwargs['event_id'])

    def test_recovered_failure_cancels_notification_retry(self):
        self.tick()
        self.view["stages"]["upload"].update(phase="waiting_destination", issue={"code": "permission", "scope": "destination"})
        self.sender.side_effect = RuntimeError("offline")
        self.tick(1)
        self.view["stages"]["upload"].update(phase="idle", issue=None)
        self.sender.side_effect = None
        self.tick(31)
        self.assertEqual(1, self.sender.call_count)
        delivery = self.store.get_meta("notification_delivery")
        self.assertEqual(0, delivery["pending_count"])
        self.assertIsNone(delivery["next_retry_at"])

    def test_progress_retry_is_cancelled_when_both_stages_pause(self):
        self.tick()
        self.sender.side_effect = RuntimeError("offline")
        self.view["stages"]["upload"]["current_bytes"] = 10
        self.tick(301)
        self.assertEqual(1, self.sender.call_count)
        for name in ("import", "upload"):
            self.view["controls"][name]["requested_stop"] = True
            self.view["stages"][name]["phase"] = "paused"
        self.sender.side_effect = None
        self.tick(31)
        self.assertEqual(1, self.sender.call_count)
        self.assertEqual(0, self.store.get_meta("notification_delivery")["pending_count"])


    def test_completion_retry_rechecks_formal_state_and_refreshes_cleanup(self):
        self.tick()
        self.archive()
        self.view["policy"]["delete_source"] = True
        self.sender.side_effect = RuntimeError("offline")
        self.tick(1)
        self.assertIn("原片：清理待处理", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.store.update(self.rows[0]["id"], state="pending")
        self.sender.side_effect = None
        self.tick(31)
        self.assertEqual(1, self.sender.call_count)
        self.archive(cleanup="deleted")
        self.tick(5)
        self.assertEqual(2, self.sender.call_count)
        self.assertIn("原片：已删除 2 个", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.assertNotIn("原片：清理待处理", json.dumps(self.sender.call_args.args[1], ensure_ascii=False))

    def test_completion_waiting_for_rearchive_does_not_block_progress_delivery(self):
        self.tick()
        self.archive()
        self.sender.side_effect = RuntimeError("offline")
        self.tick(1)
        self.store.update(self.rows[0]["id"], state="cached")
        self.view["stages"]["upload"]["current_bytes"] = 10
        self.sender.side_effect = None
        self.tick(301)
        self.assertEqual(2, self.sender.call_count)
        self.assertIn("正在归档", self.sender.call_args.args[1]["header"]["title"]["content"])
        self.archive()
        self.tick(5)
        self.assertEqual(3, self.sender.call_count)
        self.assertIn("归档完成", self.sender.call_args.args[1]["header"]["title"]["content"])

    def test_notification_tracking_does_not_modify_transfer_records(self):
        before = self.store.files()
        self.tick()
        self.view["stages"]["upload"].update(current_bytes=10)
        self.tick(301)
        self.assertEqual(before, self.store.files())

    def test_explicit_test_allowed_when_disabled_and_failure_redacted(self):
        self.values["backup"]["enabled"] = False
        self.values["notifications"]["enabled"] = False
        with patch.object(feishu, "send_card") as send:
            send_test(self.settings, self.directory)
        send.assert_called_once()
        self.assertEqual("test", self.store.get_meta("notification_delivery")["last_event"])
        with patch.object(feishu, "send_card", side_effect=RuntimeError(CREDENTIALS["receive_id"] + CREDENTIALS["app_secret"])):
            with self.assertRaises(BackupError) as caught:
                send_test(self.settings, self.directory)
        self.assertNotIn(CREDENTIALS["app_secret"], str(caught.exception))
        self.assertNotIn(CREDENTIALS["receive_id"], str(caught.exception))

    def test_import_complete_event_is_distinct_durable_and_not_repeated(self):
        self.tick()
        for row in self.rows:
            self.store.update(row['id'], state='cached')
        self.view['unplug'] = {'ready': True, 'can_disconnect': True}
        self.store.import_completed(self.batch)
        self.tick(5)
        self.assertEqual(1,self.sender.call_count)
        self.assertIn('相机可以拔走',self.sender.call_args.args[1]["header"]["title"]["content"])
        self.worker=self.make_worker();self.tick(5);self.assertEqual(1,self.sender.call_count)
        self.archive();self.tick(5);self.assertEqual(2,self.sender.call_count)
        self.assertIn('归档完成',self.sender.call_args.args[1]["header"]["title"]["content"])

    def start_progress(self):
        self.tick()
        self.view['stages']['upload']['current_bytes'] = 10
        self.tick(301)
        self.assertEqual(1, self.sender.call_count)

    def test_progress_updates_same_message_after_restart(self):
        self.start_progress()
        self.worker = self.make_worker()
        self.view['batch']['percent'] = 75
        self.view['stages']['upload']['current_bytes'] = 30
        self.tick(301)
        self.assertEqual(1, self.sender.call_count)
        self.assertEqual(1, self.updater.call_count)
        self.assertEqual('om_testprogress', self.updater.call_args.args[1])
        self.assertIn('75.0%', json.dumps(self.updater.call_args.args[2], ensure_ascii=False))

    def test_patch_network_failure_retries_patch_without_a_new_message(self):
        self.start_progress()
        self.updater.side_effect = feishu.NotificationDeliveryError('notification_network')
        self.view['stages']['upload']['current_bytes'] = 20
        self.tick(301)
        self.assertEqual(1, self.sender.call_count)
        self.assertEqual('retrying', self.store.get_meta('notification_delivery')['state'])
        self.worker = self.make_worker()
        self.updater.side_effect = None
        self.tick(30)
        self.assertEqual(1, self.sender.call_count)
        self.assertEqual(2, self.updater.call_count)

    def test_stopping_sync_finalizes_existing_card_without_sending_new_messages(self):
        self.start_progress()
        self.values['backup']['enabled'] = False
        for stage in self.view['stages'].values():
            stage.update(phase='paused', current_bytes=0)
        self.tick(1)
        self.assertEqual(1, self.sender.call_count)
        self.assertEqual(1, self.updater.call_count)
        self.assertIn('同步已关闭', json.dumps(self.updater.call_args.args[2], ensure_ascii=False))
        self.tick(5)
        self.assertEqual(1, self.updater.call_count)

    def test_completion_is_a_separate_card_and_final_progress_is_not_repeated(self):
        self.start_progress()
        self.archive()
        self.view['batch'].update(percent=100, completed_files=2, import_percent=100, imported_files=2)
        for stage in self.view['stages'].values():
            stage['phase'] = 'idle'
        self.tick(1)
        self.assertEqual(2, self.sender.call_count)
        self.assertIn('归档完成', self.sender.call_args.args[1]['header']['title']['content'])
        self.assertEqual(1, self.updater.call_count)
        self.assertIn('本轮已归档', self.updater.call_args.args[2]['header']['title']['content'])
        self.tick(5)
        self.assertEqual(1, self.updater.call_count)
        self.assertEqual(2, self.sender.call_count)

    def test_failure_alert_does_not_replace_progress_card(self):
        self.start_progress()
        self.view['stages']['upload'].update(phase='waiting_destination', issue={'code':'authentication','scope':'destination'})
        self.tick(1)
        self.assertEqual(2, self.sender.call_count)
        self.assertIn('认证失败', self.sender.call_args.args[1]['header']['title']['content'])
        self.assertEqual('om_testprogress', self.updater.call_args.args[1])

    def test_explicitly_obsolete_card_replaced_once_but_permission_error_does_not_replace(self):
        self.start_progress()
        first_uuid = self.sender.call_args.kwargs['event_id']
        self.updater.side_effect = feishu.NotificationDeliveryError('notification_permission')
        self.view['stages']['upload']['current_bytes'] = 20
        self.tick(301)
        self.assertEqual(1, self.sender.call_count)
        self.updater.side_effect = feishu.NotificationDeliveryError('notification_card_unavailable')
        self.tick(30)
        self.assertEqual(2, self.sender.call_count)
        self.assertNotEqual(first_uuid, self.sender.call_args.kwargs['event_id'])

    def test_disabling_notifications_updates_existing_card_then_stops(self):
        self.start_progress()
        self.values['notifications']['enabled'] = False
        self.tick(1)
        self.assertEqual(1, self.sender.call_count)
        self.assertIn('自动通知已关闭', self.updater.call_args.args[2]['header']['title']['content'])
        self.tick(5)
        self.assertEqual(1, self.updater.call_count)

    def test_new_recipient_gets_new_card_and_does_not_patch_previous_recipient(self):
        self.start_progress()
        self.settings.notification_credentials.return_value = {**CREDENTIALS, 'receive_id': 'other@example.invalid'}
        self.view['stages']['upload']['current_bytes'] = 20
        self.tick(301)
        self.assertEqual(2, self.sender.call_count)
        self.updater.assert_not_called()

    def test_completion_card_has_actual_archive_directory(self):
        self.tick()
        for row in self.rows:
            self.store.update(row['id'], remote_path='Camera-serial/original_1.MP4')
        self.archive()
        self.tick(1)
        content = json.dumps(self.sender.call_args.args[1], ensure_ascii=False)
        self.assertIn('归档位置', content)
        self.assertIn('Camera-serial', content)

    def test_finished_progress_card_does_not_follow_later_camera_changes(self):
        self.start_progress()
        self.archive()
        self.tick(1)
        patches = self.updater.call_count
        self.view['unplug'] = {'ready': False, 'message': 'new footage is importing'}
        self.view['stages']['import']['phase'] = 'scanning'
        self.tick(5)
        self.assertEqual(patches, self.updater.call_count)

    def test_resuming_updates_existing_card_without_waiting_an_interval(self):
        self.start_progress()
        self.values['backup']['enabled'] = False
        self.tick(1)
        self.values['backup']['enabled'] = True
        self.tick(1)
        self.assertEqual(1, self.sender.call_count)
        self.assertEqual(2, self.updater.call_count)
        self.assertNotIn('同步已关闭', json.dumps(self.updater.call_args.args[2], ensure_ascii=False))

    def test_completed_card_updates_cleanup_on_same_message_without_resending(self):
        self.tick()
        self.archive(cleanup='deleted')
        self.view['files'][0]['cache_state'] = 'verified'
        self.tick(1)
        self.assertEqual(1, self.sender.call_count)
        self.assertTrue(self.sender.call_args.args[1]['config']['update_multi'])
        self.assertIn('清理待处理 1 个', json.dumps(self.sender.call_args.args[1], ensure_ascii=False))
        self.view['files'][0]['cache_state'] = 'missing'
        self.worker = self.make_worker()
        self.tick(5)
        self.assertEqual(1, self.sender.call_count)
        self.assertEqual('om_testprogress', self.updater.call_args.args[1])
        self.assertIn('缓存：已清理 2 个', json.dumps(self.updater.call_args.args[2], ensure_ascii=False))
        self.tick(5)
        self.assertEqual(1, self.updater.call_count)

    def test_import_ready_event_requires_safe_disconnect_and_no_source_operation(self):
        self.tick()
        for row in self.rows:
            self.store.update(row['id'], state='cached')
        self.store.import_completed(self.batch)
        self.view['unplug'] = {'ready': True, 'can_disconnect': False}
        self.view['status']['source_work'] = True
        self.tick(1)
        self.sender.assert_not_called()
        self.view['status']['source_work'] = False
        self.view['unplug']['can_disconnect'] = True
        self.tick(1)
        self.assertIn('相机可以拔走', self.sender.call_args.args[1]['header']['title']['content'])

    def test_import_ready_retry_is_cancelled_when_a_new_round_supersedes_it(self):
        self.tick()
        for row in self.rows:
            self.store.update(row['id'], state='cached')
        self.store.import_completed(self.batch)
        self.view['unplug'] = {'ready': True, 'can_disconnect': True}
        self.sender.side_effect = RuntimeError('offline')
        self.tick(1)
        self.view['batch']['id'] = 'next-round'
        self.view['unplug'] = {'ready': False, 'can_disconnect': False}
        self.sender.side_effect = None
        self.tick(31)
        with self.worker_db() as db:
            row = db.execute("SELECT state FROM notification_events WHERE kind='imported'").fetchone()
        self.assertEqual('cancelled', row[0])
        self.assertEqual(1, self.sender.call_count)

    def worker_db(self):
        from media_importer.notifications import _database
        return _database(self.directory)
