"""Configuration caching never hides runtime bindings or a missing disk."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from media_importer.config import Config
import test_runtime


class SettingsRefreshTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown

    def test_unchanged_document_is_parsed_once_and_callers_cannot_mutate_it(self):
        with patch.object(Config, 'from_dict', wraps=Config.from_dict) as parse:
            item = self.settings.read()
            item['config']['backup']['enabled'] = False
            self.assertTrue(self.settings.read()['config']['backup']['enabled'])
            self.assertEqual(1, parse.call_count)

    def test_new_binding_is_read_even_when_document_is_cached(self):
        original = self.settings.read()['source_hint']
        self.store.set_meta('source_selection', {'type': original['type'], 'serial_number': original['serial'],
                                               'url': 'http://192.0.2.81:8080'})
        self.settings.directory = self.store.directory
        updated = self.settings.read()
        self.assertEqual('http://192.0.2.81:8080', updated['config']['source']['url'])
        self.assertEqual(original, updated['source_hint'])

    def test_atomic_save_and_missing_configuration_invalidate_cached_result(self):
        self.settings.read()
        self.settings.set_enabled(False)
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
        self.path.unlink()
        with self.assertRaises(OSError):
            self.settings.read()

    def test_coarse_filesystem_metadata_has_a_bounded_reread(self):
        stat = self.path.stat()
        original_stat = Path.stat
        def coarse(path, *args, **kwargs):
            return stat if path == self.path else original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', coarse), patch('media_importer.settings.monotonic', return_value=5):
            original = self.settings.read()['config']['backup']['poll_seconds']
            raw = json.loads(self.path.read_text())
            raw['backup']['poll_seconds'] = original + 1
            self.path.write_text(json.dumps(raw))
        with patch.object(Path, 'stat', coarse), patch('media_importer.settings.monotonic', return_value=6.1):
            self.assertEqual(original + 1, self.settings.read()['config']['backup']['poll_seconds'])


if __name__ == '__main__':
    unittest.main()
