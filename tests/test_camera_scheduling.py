"""Camera ownership and file-boundary dispatch, with no physical device IO."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from media_importer.camera_coordinator import dispatch, operation_reason
from media_importer.model import SourceBlocked
from media_importer.store import Store


class CameraSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / 'data')
        self.service = SimpleNamespace(store=self.store, activity=None,
            process_camera_operations=True, control=SimpleNamespace(source_interrupt=threading.Event()))

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def handlers(self, **results):
        stack = ExitStack()
        handlers = {}
        for name in ('live_preview', 'capture', 'camera_library', 'camera_operations', 'selected_imports'):
            handlers[name] = stack.enter_context(patch('media_importer.' + name + '.process_one',
                                                       return_value=results.get(name, False)))
        return stack, handlers

    def test_import_boundary_handles_capture_without_starting_a_selected_pipeline(self):
        stack, handlers = self.handlers(capture=True, selected_imports=True)
        self.service.control.source_interrupt.set()
        with stack:
            result = dispatch(self.service, None, import_lane=True)
        self.assertTrue(result.handled)
        self.assertFalse(result.selection)
        self.assertFalse(self.service.control.source_interrupt.is_set())
        handlers['selected_imports'].assert_not_called()

    def test_only_idle_dispatch_can_start_a_selected_transfer(self):
        stack, handlers = self.handlers(selected_imports=True)
        with stack:
            result = dispatch(self.service, None)
        self.assertTrue(result.selection)
        self.assertFalse(handlers['selected_imports'].call_args.kwargs['browsing_only'])
        stack, handlers = self.handlers()
        with stack:
            dispatch(self.service, None, import_lane=True)
        self.assertTrue(handlers['selected_imports'].call_args.kwargs['browsing_only'])

    def test_active_picture_blocks_source_import_and_deletion_not_read_only_capture(self):
        self.store.set_meta('live_preview', {'state': 'ready', 'mode': 'preview'})
        for operation in ('import', 'delete', 'browse'):
            self.assertIsNotNone(operation_reason(self.store, operation))
        self.assertIsNone(operation_reason(self.store, 'capture'))
        stack, handlers = self.handlers()
        with stack, self.assertRaises(SourceBlocked):
            dispatch(self.service, None, import_lane=True)
        handlers['camera_operations'].assert_not_called()

    def test_uncertain_live_stop_keeps_radio_owned_until_recovery_is_confirmed(self):
        self.store.set_meta('live_preview', {'state': 'error', 'mode': 'rtmp', 'cleanup_pending': True})
        for operation in ('connect', 'capture', 'import', 'delete', 'browse'):
            self.assertIsNotNone(operation_reason(self.store, operation))
        self.store.set_meta('live_preview', {'state': 'stopped', 'mode': 'rtmp', 'cleanup_pending': False})
        for operation in ('connect', 'capture', 'import', 'delete', 'browse'):
            self.assertIsNone(operation_reason(self.store, operation))

    def test_selected_lane_never_executes_unrelated_manual_cleanup(self):
        self.service.process_camera_operations = False
        stack, handlers = self.handlers(camera_operations=True)
        with stack:
            self.assertFalse(dispatch(self.service, None, import_lane=True).handled)
        handlers['camera_operations'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
