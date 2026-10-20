import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.smoke_native import isolated_commands


class SmokeEnvironmentTests(unittest.TestCase):
    def test_host_docker_is_hidden_but_windows_purge_tools_remain_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            docker = Path(temporary) / ('docker.exe' if os.name == 'nt' else 'docker')
            docker.write_bytes(b'not a real executable')
            docker.chmod(0o755)
            with patch.dict(os.environ, {'PATH': temporary + os.pathsep + os.environ.get('PATH', '')}):
                before = os.environ['PATH']
                self.assertEqual(os.path.normcase(str(docker)), os.path.normcase(shutil.which('docker')))
                with isolated_commands():
                    self.assertIsNone(shutil.which('docker'))
                    if os.name == 'nt':
                        self.assertTrue((Path(os.environ['SystemRoot']) / 'System32/cmd.exe').is_file())
                        self.assertIsNotNone(shutil.which('powershell.exe'))
                self.assertEqual(before, os.environ['PATH'])

    def test_failure_restores_an_unset_path(self):
        with patch.dict(os.environ):
            os.environ.pop('PATH', None)
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                with isolated_commands():
                    raise RuntimeError('simulated smoke failure')
            self.assertNotIn('PATH', os.environ)
