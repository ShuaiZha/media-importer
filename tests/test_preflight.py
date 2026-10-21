import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from media_importer.config import preferences
from media_importer.model import SourceError, DestinationError
from media_importer.preflight import run
from media_importer.settings import Settings
from media_importer.store import Store
from test_bot import ENV
from test_service import configuration


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, ENV, clear=True); self.env.start()
        self.path = self.root / 'settings.json'
        self.path.write_text(json.dumps(preferences(configuration(enabled=False).values)))
        self.settings = Settings(self.path)
        self.store = Store(self.root / 'data')
        self.source, self.destination = Mock(), Mock()
        self.source.idle_reason.return_value = None
        self.patches = [patch('media_importer.preflight.create_source', return_value=self.source),
                        patch('media_importer.preflight.create_destination', return_value=self.destination),
                        patch('media_importer.adapters.feishu.check_credentials')]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.store.close(); self.env.stop(); self.tmp.cleanup()

    def test_checks_are_non_transferring_and_preserve_settings_and_records(self):
        before = self.path.read_bytes(), self.store.files()
        result = run(self.settings, self.store)
        self.assertEqual('ok', result['state'])
        self.assertEqual(before, (self.path.read_bytes(), self.store.files()))
        self.assertFalse(list(self.store.cache.iterdir()))
        self.source.download.assert_not_called()
        self.source.delete.assert_not_called()
        self.destination.upload.assert_not_called()
        self.destination.check_write.assert_not_called()
        self.assertNotIn(ENV['DESTINATION_PASSWORD'], json.dumps(result))

    def test_unavailable_peers_have_separate_waiting_results(self):
        self.source.identify.side_effect = SourceError('offline')
        self.destination.check.side_effect = DestinationError('network timeout')
        result = run(self.settings, self.store)
        self.assertEqual('ok', result['checks']['cache']['state'])
        self.assertEqual('waiting', result['checks']['source']['state'])
        self.assertEqual('waiting', result['checks']['destination']['state'])

    def test_auth_failure_is_actionable_and_redacted(self):
        self.destination.check.side_effect = DestinationError('authentication ' + ENV['DESTINATION_PASSWORD'])
        result = run(self.settings, self.store)
        self.assertEqual('error', result['checks']['destination']['state'])
        self.assertNotIn(ENV['DESTINATION_PASSWORD'], json.dumps(result))

    def test_missing_mount_does_not_create_fallback_cache(self):
        self.store.cache.rmdir()
        result = run(self.settings, self.store)
        self.assertEqual('waiting', result['checks']['cache']['state'])
        self.assertFalse(self.store.cache.exists())

    def test_invalid_json_is_reported_without_probing_media(self):
        self.path.write_text('{')
        result = run(self.settings, self.store)
        self.assertEqual('error', result['checks']['configuration']['state'])
        self.source.identify.assert_not_called()

    def test_current_session_is_used_instead_of_saved_usb_endpoint(self):
        import time
        self.store.set_meta('source_connection', {'state': 'approved', 'online': True,
                            'last_seen': time.time(), 'device': {'transport': 'wifi'}})
        with patch('media_importer.source_session.SourceSession', return_value=self.source) as session:
            result = run(self.settings, self.store)
        self.assertEqual('ok', result['checks']['source']['state'])
        self.assertEqual(self.store.directory, session.call_args.args[1])
        self.patches[0].target.create_source.assert_not_called()
