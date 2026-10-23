"""User workflows share admission, observed state and independent upload facts."""
import time
import unittest

from media_importer.camera_coordinator import OPERATIONS, dispatch, operation_reason, state
from media_importer.model import now
from media_importer.bot_routes import TEXT_ACTIONS, MENU_ACTIONS, MENUS
import test_camera_scheduling as scheduling
import test_runtime as runtime_fixtures
import test_bot as bot_fixtures


class CameraWorkflowTests(unittest.TestCase):
    setUp = scheduling.CameraSchedulingTests.setUp
    tearDown = scheduling.CameraSchedulingTests.tearDown
    handlers = scheduling.CameraSchedulingTests.handlers

    def observe(self, **values):
        self.store.set_meta('source_connection', {'id': 'session', 'state': 'approved', 'online': True,
                            'last_seen': time.time(), 'device': {'serial_number': 'fixture', 'transport': 'wifi'}})
        self.store.set_meta('camera_control', {'serial': 'fixture', 'observed_at': now(), **values})

    def test_all_capture_commands_have_one_admission_contract(self):
        from media_importer.capture import COMMANDS
        self.assertTrue(COMMANDS <= OPERATIONS.keys())
        self.assertEqual('snapshot', OPERATIONS['status'].resource)
        self.assertTrue(OPERATIONS['stop_recording'].stop)
        self.assertNotIn('upload', OPERATIONS)  # Upload owns no camera resource.

    def test_recording_blocks_reads_and_new_picture_but_allows_stop_and_hilight(self):
        self.observe(recording=True, mode='video')
        for operation in ('import', 'delete', 'browse', 'preview', 'livestream'):
            self.assertIsNotNone(operation_reason(self.store, operation), operation)
        for command in ('state', 'catalog', 'stop_recording', 'hilight'):
            self.assertIsNone(operation_reason(self.store, 'capture', command=command), command)
        self.assertIsNotNone(operation_reason(self.store, 'capture', command='set_parameter'))

    def test_preview_transition_is_explicit_and_does_not_allow_plain_start(self):
        self.observe(recording=False)
        self.store.set_meta('live_preview', {'state': 'ready', 'mode': 'preview'})
        self.assertIsNotNone(operation_reason(self.store, 'capture', command='start_recording'))
        self.assertIsNone(operation_reason(self.store, 'capture', command='preview_record'))
        self.assertIsNone(operation_reason(self.store, 'capture', command='state'))

    def test_uncertain_stop_never_releases_occupation_on_age_alone(self):
        self.store.set_meta('live_preview', {'state': 'error', 'mode': 'preview', 'cleanup_pending': True,
                            'observed_at': '2000-01-01T00:00:00+00:00'})
        self.assertEqual('recovering', state(self.store)['activity']['state'])
        for operation in ('preview', 'livestream', 'import', 'browse', 'delete'):
            self.assertIsNotNone(operation_reason(self.store, operation))
        self.assertIsNone(operation_reason(self.store, 'connect'))
        self.assertIsNone(operation_reason(self.store, 'status'))

    def test_live_session_is_connected_without_requiring_camera_http(self):
        self.store.set_meta('source_connection', {'state': 'offline', 'device': {'http_ready': False}})
        self.store.set_meta('live_preview', {'state': 'ready', 'mode': 'rtmp'})
        observed = state(self.store)
        self.assertEqual('connected', observed['connection']['state'])
        self.assertEqual('livestream', observed['activity']['state'])
        self.assertFalse(observed['capabilities']['media_readable'])
        self.assertTrue(observed['capabilities']['picture_ready'])
        self.assertIsNotNone(operation_reason(self.store, 'capture', command='state'))

    def test_stop_intent_is_stopping_not_stopped_and_revokes_picture_readiness(self):
        self.store.set_meta('live_preview', {'state': 'ready', 'mode': 'rtmp', 'stop_requested': True})
        observed = state(self.store)
        self.assertEqual('正在结束直播', observed['activity']['label'])
        self.assertTrue(observed['picture']['active'])
        self.assertFalse(observed['capabilities']['picture_ready'])

    def test_old_or_missing_hardware_state_does_not_render_idle(self):
        self.observe(recording=False)
        value = self.store.get_meta('camera_control')
        value['observed_at'] = '2000-01-01T00:00:00+00:00'
        self.store.set_meta('camera_control', value)
        self.assertEqual('unknown', state(self.store)['activity']['state'])
        self.assertTrue(state(self.store)['observed']['stale'])

    def test_stale_external_recording_requires_new_observation_before_source_io(self):
        self.observe(recording=False)
        self.store.set_meta('camera_control', {})
        self.store.status(source={'serial_number': 'fixture', 'telemetry': {
            'available': True, 'recording': True, 'observed_at': '2000-01-01T00:00:00+00:00'}})
        self.assertEqual('recovering', state(self.store)['activity']['state'])
        self.assertIsNotNone(operation_reason(self.store, 'import'))
        self.assertIsNone(operation_reason(self.store, 'capture', command='stop_recording'))

    def test_capture_uncertainty_blocks_writes_until_fresh_observation(self):
        self.observe(uncertain=True, error='fixture result unknown')
        self.assertIsNotNone(operation_reason(self.store, 'import'))
        self.assertIsNone(operation_reason(self.store, 'capture', command='state'))
        self.assertIsNone(operation_reason(self.store, 'capture', command='stop_recording'))
        self.observe(recording=False)
        self.assertIsNone(operation_reason(self.store, 'import'))

    def test_new_stop_intent_runs_before_queued_picture(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO capture_requests (id,owner,command,session_id,device_type,serial,created,updated) VALUES ('fixture','owner','stop_recording','session','fixture','fixture',0,0)")
        stack, handlers = self.handlers(capture=True, live_preview=True)
        with stack:
            self.assertTrue(dispatch(self.service, None).handled)
        handlers['live_preview'].assert_not_called()

    def test_menu_names_change_without_changing_events_or_legacy_commands(self):
        self.assertEqual(['备份', '相机', '设置'], [label for label, _ in MENUS])
        for new, old, key in (('备份状态', '同步状态', 'sync.status'),
                              ('开启备份', '开启同步', 'sync.start'), ('停止备份', '停止同步', 'sync.stop')):
            self.assertEqual(MENU_ACTIONS[key], TEXT_ACTIONS[new])
            self.assertEqual(MENU_ACTIONS[key], TEXT_ACTIONS[old])


class TransferWorkflowTests(unittest.TestCase):
    setUp = runtime_fixtures.RuntimeTests.setUp
    tearDown = runtime_fixtures.RuntimeTests.tearDown

    def test_picture_pauses_camera_import_while_complete_cache_uploads_then_resumes(self):
        from media_importer.runtime import Control
        from media_importer.service import Service
        from media_importer.store import Store
        from test_service import FakeSource, FakeDestination, footage
        control = Control(self.settings, self.stop)
        config = control.adopt()
        source, remote = FakeSource([footage(1), footage(2)]), FakeDestination()
        download, upload = source.download, remote.upload
        facts = []

        def importing(asset, *args):
            result = download(asset, *args)
            if asset.identity == footage(1).identity:
                db = Store(self.data)
                try: db.set_meta('live_preview', {'state': 'ready', 'mode': 'preview'})
                finally: db.close()
            return result

        def uploading(*args, **kwargs):
            if not facts:
                db = Store(self.data)
                try:
                    facts.append(state(db))
                    self.assertEqual(1, len(source.downloads))
                    self.assertIsNotNone(operation_reason(db, 'import'))
                    db.set_meta('live_preview', {'state': 'stopped', 'mode': 'preview'})
                finally: db.close()
            return upload(*args, **kwargs)

        source.download, remote.upload = importing, uploading
        service = Service(config, self.store, source, remote, self.stop, control)
        result = service.safe_cycle()
        # The outer daemon may start the next pipeline after a waiting pass;
        # neither path changes the user's enabled setting or task scope.
        if not result['complete']:
            result = service.safe_cycle()
        self.assertTrue(result['complete'], result)
        self.assertEqual('preview', facts[0]['activity']['state'])
        self.assertEqual(2, len(source.downloads))
        self.assertTrue(all(row['state'] == 'uploaded' for row in self.store.files()))
        self.assertEqual([], source.deletes)
        self.assertTrue(self.settings.read()['config']['backup']['enabled'])


class QueryWorkflowTests(unittest.TestCase):
    setUp = bot_fixtures.BotTests.setUp
    tearDown = bot_fixtures.BotTests.tearDown

    def test_status_menu_does_not_enqueue_scan_or_change_switches_during_live(self):
        self.store.set_meta('live_preview', {'state': 'ready', 'mode': 'rtmp'})
        before = self.path.read_bytes()
        from unittest.mock import patch
        with patch('media_importer.adapters.source_link', side_effect=AssertionError('query performs camera IO')):
            result = self.bot.panel('camera_status')
        import json
        self.assertIn('有声直播', json.dumps(result, ensure_ascii=False))
        self.assertEqual(0, self.store.db.execute('SELECT COUNT(*) FROM camera_operations').fetchone()[0])
        self.assertEqual(before, self.path.read_bytes())

    def test_original_filenames_are_never_translated_by_message_rendering(self):
        from media_importer.bot_cards import paragraph
        self.assertEqual('素材/同步.MP4', paragraph('素材/同步.MP4')['text']['content'])


if __name__ == '__main__':
    unittest.main()
