"""Execute the real Windows launcher against disposable, deliberately locked files."""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from media_importer.native import windows


@contextmanager
def deny_delete(path):
    """Allow reads/writes and execution, but keep Windows from deleting the file."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    closed = False
    def release():
        nonlocal closed
        if not closed:
            kernel.CloseHandle(handle)
            closed = True
    try:
        yield release
    finally:
        release()


def purge_after_locked_attempt(command, path):
    """Release the real handle only after the wrapper observes a delete failure."""
    lines = []
    observed = threading.Event()
    with deny_delete(path) as release:
        process = subprocess.Popen([str(command), 'uninstall', '--purge', '--yes'],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace')
        def read():
            for line in process.stdout:
                lines.append(line)
                if 'Waiting for installation files to become available' in line:
                    observed.set(); release()
        reader = threading.Thread(target=read); reader.start()
        try:
            process.wait(timeout=60)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
            reader.join(timeout=5); process.stdout.close()
    result = subprocess.CompletedProcess(process.args, process.returncode, ''.join(lines), '')
    if not observed.is_set():
        raise AssertionError('Expected an actual blocked delete attempt: ' + result.stdout)
    return result


@unittest.skipUnless(os.name == 'nt', 'requires real Windows file sharing and cmd.exe')
class WindowsPurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='media-importer-lock-test-')
        self.root = Path(self.temp.name).resolve()
        # Include cmd and PowerShell metacharacters: these are literal paths.
        self.home = self.root / "native [100%] & ! (test)"
        self.home.mkdir()
        self.command = self.root / 'commands/media-importer.cmd'
        self.command.parent.mkdir()
        self.other = self.root / 'unrelated.txt'; self.other.write_text('keep')
        self.locked = self.home / 'locked.dat'; self.locked.write_text('in use')
        self.receipt = windows.purge_receipt(self.command)
        self.approved = {'home': str(self.home), 'command': str(self.command),
                         'directories': [], 'env': str(self.root / 'removed.env')}
        self.receipt.write_text(json.dumps(self.approved), encoding='utf-8')
        self.make_launcher(2)

    def tearDown(self):
        self.temp.cleanup()

    def make_launcher(self, seconds):
        with patch.object(windows, 'PURGE_WAIT_SECONDS', seconds):
            # Deliberately absent: cleanup must not need an executable, DLLs or
            # installation.json that an earlier failed removal may have deleted.
            self.command.write_text(windows.launcher(self.home, self.home / 'missing.exe'), encoding='utf-8')

    def run_command(self, *args):
        return subprocess.run([str(self.command), *args], capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=20)

    def assert_removed(self, result):
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse(self.home.exists())
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.command.exists())
        self.assertEqual('keep', self.other.read_text())

    def test_transient_file_lock_is_retried_until_released(self):
        self.make_launcher(8)
        self.assert_removed(purge_after_locked_attempt(self.command, self.locked))

    def test_timeout_keeps_recovery_command_and_requires_explicit_retry(self):
        with deny_delete(self.locked):
            result = self.run_command('uninstall', '--purge', '--yes')
            self.assertNotEqual(0, result.returncode)
            self.assertIn('Native uninstall incomplete', result.stderr)
            self.assertNotIn('Native uninstall complete.', result.stdout)
            self.assertTrue(self.locked.exists())
            self.assertTrue(self.command.exists())
            self.assertEqual(self.approved, json.loads(self.receipt.read_text()))
        for args in [('status',), ('uninstall',), ('uninstall', '--purge')]:
            with self.subTest(args=args):
                self.assertNotEqual(0, self.run_command(*args).returncode)
                self.assertTrue(self.locked.exists())
        self.assert_removed(self.run_command('uninstall', '--yes', '--purge'))

    def test_retry_can_finish_after_program_directory_was_already_removed(self):
        self.locked.unlink(); self.home.rmdir()
        self.assert_removed(self.run_command('uninstall', '--purge', '--yes'))

    def test_mismatched_receipt_does_not_delete_either_directory(self):
        self.approved['home'] = str(self.root)
        self.receipt.write_text(json.dumps(self.approved))
        result = self.run_command('uninstall', '--purge', '--yes')
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(self.locked.exists())
        self.assertTrue(self.other.exists())

    def test_recreated_local_data_blocks_installation_cleanup(self):
        path = Path(self.approved['env']); path.write_text('new data')
        result = self.run_command('uninstall', '--purge', '--yes')
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(self.locked.exists())
        self.assertEqual('new data', path.read_text())


if __name__ == '__main__':
    unittest.main()
