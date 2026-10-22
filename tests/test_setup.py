import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

spec = importlib.util.spec_from_file_location('first_setup', Path(__file__).parents[1] / 'scripts/setup.py')
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        sample = Path(__file__).parents[1] / 'config/settings.example.json'
        (self.root / 'config/settings.example.json').write_bytes(sample.read_bytes())

    def tearDown(self):
        self.temp.cleanup()

    def test_first_install_disables_sync_and_preserves_source_and_secret_defaults(self):
        answers = iter(['http://192.0.2.10:8080', './my cache', 'storage.example.com', '', 'media', 'imports',
                        'backup', '', 'cli_EXAMPLE00000000', 'ou_EXAMPLE00000000'])
        secrets = iter(["x$#'\\password", 'fake-app-secret'])
        with patch('builtins.print') as output:
            self.assertEqual(0, wizard.setup(self.root, read=lambda _: next(answers), hidden=lambda _: next(secrets),
                                             probe=lambda _: 'CEXAMPLE000001', checks=None))
        values = dotenv_values(self.root / '.env', interpolate=False)
        from media_importer.credentials import credential_path, read
        raw = json.loads((self.root / 'config/settings.json').read_text())
        self.assertEqual("x$#'\\password", read(credential_path(self.root / 'config', raw['archive']['credential_id'])))
        self.assertNotIn('DESTINATION_PASSWORD', values)
        self.assertNotIn('fake-app-secret', str(output.call_args_list))
        self.assertNotIn('password', str(output.call_args_list))
        settings = json.loads((self.root / 'config/settings.json').read_text())
        self.assertFalse(settings['backup']['enabled'])
        self.assertFalse(settings['backup']['delete_source_after_import'])
        self.assertFalse(settings['backup']['stop_after_current'])
        self.assertFalse(settings['notifications']['enabled'])
        self.assertFalse(settings['notifications']['connection_events'])
        self.assertEqual(str((self.root / 'my cache').resolve()), values['MEDIA_IMPORTER_CACHE_HOST_DIR'])
        self.assertEqual(0o600, (self.root / '.env').stat().st_mode & 0o777)

    def test_existing_configuration_is_not_touched_or_prompted(self):
        (self.root / '.env').write_text('keep-this')
        with self.assertRaises(ValueError):
            wizard.setup(self.root, read=lambda _: self.fail('must not ask credentials'))
        self.assertEqual('keep-this', (self.root / '.env').read_text())

    def test_identity_probe_uses_selected_adapter_without_vendor_http_in_wizard(self):
        from media_importer.model import ConfigError, SourceConnectionError
        with patch('media_importer.adapters.source_defaults', return_value={'type': 'fixture'}), \
                patch('media_importer.adapters.probe_source', return_value={'serial_number': 'fixture-1'}) as probe:
            self.assertEqual('fixture-1', wizard.camera_identity('fixture://device'))
            probe.assert_called_once_with('fixture', 'fixture://device')
            probe.side_effect = SourceConnectionError('offline')
            self.assertIsNone(wizard.camera_identity('fixture://device'))
            probe.side_effect = ConfigError('invalid address')
            with self.assertRaises(ConfigError):
                wizard.camera_identity('invalid')

    def test_first_install_can_defer_camera_to_usb_discovery(self):
        answers = iter(['', './cache', 'storage.example.com', '', 'media', '', 'backup', '',
                        'cli_EXAMPLE00000000', 'ou_EXAMPLE00000000'])
        with patch('builtins.print'):
            wizard.setup(self.root, read=lambda _: next(answers), hidden=lambda _: 'example-secret',
                         probe=lambda _: self.fail('empty hint must not make a request'), checks=None)
        values = dotenv_values(self.root / '.env', interpolate=False)
        raw = json.loads((self.root / 'config/settings.json').read_text())
        self.assertEqual('', raw['source_options']['url'])
        self.assertEqual('', raw['source_options']['serial'])
        self.assertNotIn('SOURCE_URL', values)

    def test_missing_external_disk_does_not_create_fallback(self):
        with patch.object(wizard.sys, 'platform', 'darwin'), patch.object(Path, 'is_mount', return_value=False):
            with self.assertRaises(ValueError):
                wizard.cache_directory('/Volumes/NotConnected/media/cache', self.root)
        self.assertFalse((self.root / 'cache').exists())

    def test_reused_cache_and_multiline_values_rejected(self):
        cache = self.root / 'cache'
        cache.mkdir()
        (cache / '.media-importer-cache-id').write_text('existing-binding')
        with self.assertRaises(ValueError):
            wizard.cache_directory(str(cache), self.root)
        with self.assertRaises(ValueError):
            wizard.literal('value\nDESTINATION_PASSWORD=oops')

    def test_connection_failure_keeps_config_and_never_starts_services(self):
        calls = []
        class Result:
            returncode = 2
            stdout = json.dumps({'checks': {'source': {'state': 'waiting', 'message': '相机未连接'}}})
        def run(args, root):
            calls.append(args)
            result = Result()
            if args[-1] != '--json':
                result.returncode = 0
            return result
        with patch.object(wizard, 'docker', side_effect=run), patch.object(wizard.subprocess, 'run', return_value=type('Build', (), {'returncode': 0})()), patch('builtins.print'):
            self.assertEqual(2, wizard.check(self.root))
        self.assertFalse(any('up' in command or 'daemon' in command for command in calls))
