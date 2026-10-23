import io
from contextlib import closing
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from media_importer.config import DEFAULTS
from media_importer.deployment import assert_runtime
from media_importer.native import lifecycle, platforms, releases, purge
from media_importer.store import Store, worker_lock
from media_importer.storage import initialize_cache, inspect_cache, MARKER


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'existing project'; self.project.mkdir()
        for name in ('config', 'data', 'cache'):
            (self.project / name).mkdir()
        self.env_text = "DESTINATION_TYPE='smb'\nDESTINATION_HOST='storage.example.com'\nDESTINATION_SHARE='media'\nDESTINATION_USERNAME='example'\nDESTINATION_PASSWORD='example-password'\nMEDIA_IMPORTER_CACHE_HOST_DIR='./cache'\n"
        (self.project / '.env').write_text(self.env_text)
        self.preferences = json.loads(json.dumps(DEFAULTS))
        self.preferences['backup'].update(enabled=True, delete_source_after_import=True)
        (self.project / 'config/settings.json').write_text(json.dumps(self.preferences))
        with patch.dict(os.environ, {'MEDIA_IMPORTER_REQUIRE_CACHE_MOUNT': '0', 'MEDIA_IMPORTER_CACHE_DIR': '', 'MEDIA_IMPORTER_RUNTIME': ''}):
            db = Store.create(self.project / 'data')
            db.set_meta('cache_identity', 'original-volume'); db.set_meta('must-preserve', {'unfinished': ['a', 'b']}); db.close()
        (self.project / 'cache' / MARKER).write_text('original-volume\n')
        (self.project / 'cache/unfinished.bin').write_bytes(b'complete-small-cache')
        self.home = self.root / 'native'; self.home.mkdir()
        self.bin = self.root / 'commands'
        self.source = self.bundle('1.0.0')
        self.initial = {p: p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        self.no_docker = patch.object(lifecycle, 'assert_no_docker'); self.no_docker.start()
        self.validation = patch.object(lifecycle, 'validate', side_effect=lambda b, r: releases.metadata(b)); self.validation.start()
        self.services = [patch.object(platforms, name) for name in ('register', 'stop', 'unregister')]
        for p in self.services: p.start()

    def tearDown(self):
        for p in self.services: p.stop()
        self.validation.stop(); self.no_docker.stop(); self.temp.cleanup()

    def bundle(self, version):
        root = self.root / ('bundle-' + version); root.mkdir()
        (root / 'bundle.json').write_text(json.dumps({'format': 1, 'version': version, 'platform': releases.platform_key(), 'config_version': 4, 'database_version': 5}))
        releases.executable(root).write_bytes(b'test-program')
        return root

    def install(self):
        return lifecycle.install(self.home, self.source, self.project, self.bin, no_start=True)

    def assert_preserved(self):
        for path, content in self.initial.items():
            self.assertEqual(content, path.read_bytes(), str(path))

    def test_install_adopts_paths_and_uninstall_keeps_every_original_file(self):
        result = self.install()
        self.assertEqual(str(self.project / 'cache'), result['paths']['cache'])
        self.assertTrue(Path(result['command']).exists())
        self.assert_preserved()
        with patch.dict(os.environ, {'MEDIA_IMPORTER_RUNTIME': 'docker'}):
            with self.assertRaises(ValueError): assert_runtime(self.project / 'data')
        with patch.dict(os.environ, {'MEDIA_IMPORTER_RUNTIME': 'native'}):
            assert_runtime(self.project / 'data')
        lifecycle.uninstall(self.home)
        self.assert_preserved()
        self.assertFalse(Path(result['command']).exists())
        self.assertFalse((self.project / 'data/deployment.json').exists())
        self.assertTrue(list((self.home / 'backups').glob('*/state.sqlite3')))

    def test_update_changes_program_only_and_same_version_is_noop(self):
        record = self.install()
        with patch.object(lifecycle, 'stop', wraps=lifecycle.stop) as stop:
            same, changed = lifecycle.update(self.home, self.source)
            self.assertFalse(changed); stop.assert_not_called()
            newer, changed = lifecycle.update(self.home, self.bundle('1.0.1'))
            self.assertTrue(changed); self.assertNotEqual(record['bundle'], newer['bundle'])
        self.assert_preserved()
        self.assertTrue(Path(record['bundle']).is_dir())
        self.assertTrue((self.home / 'stopped').exists())
        with self.assertRaises(ValueError): lifecycle.update(self.home, self.source)

    def test_failed_candidate_does_not_stop_running_installation(self):
        self.install(); (self.home / 'stopped').unlink()
        before = (self.home / 'installation.json').read_bytes()
        with patch.object(lifecycle, 'validate', side_effect=ValueError('bad candidate')), patch.object(lifecycle, 'stop') as stop:
            with self.assertRaises(ValueError): lifecycle.update(self.home, self.bundle('1.1.0'))
            stop.assert_not_called()
        self.assertEqual(before, (self.home / 'installation.json').read_bytes()); self.assert_preserved()

    def test_live_worker_prevents_adoption(self):
        with worker_lock(self.project / 'data'):
            with self.assertRaises(ValueError): self.install()
        self.assertFalse((self.home / 'installation.json').exists()); self.assert_preserved()

    def test_existing_command_or_installation_not_overwritten(self):
        self.bin.mkdir(); lifecycle.command_path(self.bin).write_text('other program')
        with self.assertRaises(ValueError): self.install()
        self.assertEqual('other program', lifecycle.command_path(self.bin).read_text())
        self.assert_preserved()

    def test_changed_mounts_are_rejected_without_creating_directories(self):
        record = self.install()
        absent = self.project / 'missing-ssd'
        (self.project / '.env').write_text(self.env_text.replace('./cache', str(absent)))
        with self.assertRaises(ValueError): lifecycle.assert_paths(record)
        self.assertFalse(absent.exists())

    def test_snapshot_preserves_uncheckpointed_wal(self):
        record = self.install()
        connection = sqlite3.connect(self.project / 'data/state.sqlite3')
        try:
            connection.execute('PRAGMA wal_autocheckpoint=0')
            connection.execute("INSERT INTO meta VALUES ('wal-only','true')"); connection.commit()
            folder = lifecycle.snapshot(self.home, record)
            with closing(sqlite3.connect(folder / 'state.sqlite3')) as db:
                self.assertEqual('true', db.execute("SELECT value FROM meta WHERE key='wal-only'").fetchone()[0])
            self.assertFalse((folder / 'cache').exists())
        finally: connection.close()

    def test_missing_native_cache_is_never_created_and_volume_marker_is_required(self):
        missing = self.root / 'unplugged/cache'
        with patch.dict(os.environ, {'MEDIA_IMPORTER_RUNTIME': 'native', 'MEDIA_IMPORTER_CACHE_DIR': str(missing), 'MEDIA_IMPORTER_REQUIRE_CACHE_MOUNT': '0'}):
            db = Store.create(self.project / 'data')
            try:
                self.assertFalse(initialize_cache(db)['available'])
                self.assertFalse(missing.exists())
            finally: db.close()
        with patch.dict(os.environ, {'MEDIA_IMPORTER_CACHE_VOLUME': str(self.root / 'no-volume')}):
            self.assertFalse(inspect_cache(self.project / 'cache', 'original-volume')['available'])

    def purge_context(self, record):
        return patch.dict(os.environ, {'MEDIA_IMPORTER_COMMAND_WRAPPER': record['command'],
                                      'MEDIA_IMPORTER_PURGE_WRAPPER': platforms.label(self.home)})

    def test_purge_removes_registered_local_data_but_not_checkout_or_unrelated_files(self):
        record = self.install()
        unrelated = self.root / 'personal-file.txt'; unrelated.write_text('keep')
        (self.project / 'README.md').write_text('source checkout')
        (self.project / 'manual-backup').mkdir()
        (self.project / 'manual-backup/keep.txt').write_text('user backup')
        (self.home / 'logs').mkdir(); (self.home / 'logs/service.log').write_text('private log')
        with self.purge_context(record):
            result, retained = lifecycle.uninstall(self.home, purge_data=True)
            purge.finish(self.home, result)
        for name in ('.env', 'config', 'data', 'cache'):
            self.assertFalse((self.project / name).exists(), name)
        self.assertEqual('keep', unrelated.read_text())
        self.assertEqual('source checkout', (self.project / 'README.md').read_text())
        self.assertEqual('user backup', (self.project / 'manual-backup/keep.txt').read_text())
        if sys.platform == 'win32':
            from media_importer.native.windows import purge_receipt
            receipt = purge_receipt(result['command'])
            self.assertEqual(str(self.home), json.loads(receipt.read_text())['home'])
            self.assertNotIn(self.home, receipt.parents)
        else:
            self.assertFalse(self.home.exists())
            self.assertFalse(Path(record['command']).exists())

    def test_purge_rejects_broad_paths_before_stopping_any_service(self):
        record = self.install()
        for path in (self.project, self.home, Path.home().resolve(), Path(tempfile.gettempdir()).resolve(), Path(self.root.anchor)):
            with self.subTest(path=path), self.purge_context(record), patch.object(lifecycle, 'stop') as stop:
                changed = json.loads(json.dumps(record)); changed['paths']['cache'] = str(path)
                lifecycle.save(self.home / 'installation.json', changed)
                with self.assertRaises(ValueError): lifecycle.uninstall(self.home, purge_data=True)
                stop.assert_not_called(); self.assert_preserved()
        lifecycle.save(self.home / 'installation.json', record)

    def test_purge_refuses_changed_configuration_and_foreign_data_owner(self):
        record = self.install()
        with self.purge_context(record), patch.object(lifecycle, 'stop') as stop:
            (self.project / '.env').write_text(self.env_text.replace('./cache', './data'))
            with self.assertRaises(ValueError): lifecycle.uninstall(self.home, purge_data=True)
            (self.project / '.env').write_text(self.env_text)
            (self.project / 'data/deployment.json').write_text(json.dumps({'mode': 'native', 'home': 'other'}))
            with self.assertRaises(ValueError): lifecycle.uninstall(self.home, purge_data=True)
            stop.assert_not_called(); self.assert_preserved()

    def test_purge_checks_worker_lock_before_removing_data(self):
        record = self.install()
        with self.purge_context(record), worker_lock(self.project / 'data'):
            with self.assertRaisesRegex(RuntimeError, '传输进程'):
                lifecycle.uninstall(self.home, purge_data=True)
        self.assert_preserved()
        self.assertFalse((self.home / 'purge.json').exists())

    def test_interrupted_purge_can_retry_without_recreating_deleted_directories(self):
        record = self.install()
        original_remove = purge.remove_tree
        def fail_data(path):
            if path == self.project / 'data': raise PermissionError('test-only interruption')
            original_remove(path)
        with self.purge_context(record):
            with patch.object(purge, 'remove_tree', side_effect=fail_data):
                with self.assertRaises(PermissionError): lifecycle.uninstall(self.home, purge_data=True)
            self.assertFalse((self.project / 'config').exists())
            self.assertTrue((self.project / 'cache/unfinished.bin').exists())
            self.assertTrue((self.home / 'installation.json').exists())
            result, _ = lifecycle.uninstall(self.home, purge_data=True)
            purge.finish(self.home, result)
        self.assertFalse((self.project / '.env').exists())
        self.assertFalse((self.project / 'cache').exists())

    def test_interrupted_purge_can_retry_with_docker_off_after_owner_marker_was_removed(self):
        record = self.install()
        with self.purge_context(record):
            lifecycle.save(self.home / 'purge.json', purge.plan(self.home, record))
            (self.project / 'data/deployment.json').unlink()
            self.no_docker.stop()
            try:
                result = lifecycle.subprocess.CompletedProcess(['docker', 'ps'], 1, '', 'daemon unavailable')
                with patch.object(lifecycle.shutil, 'which', return_value='docker'), patch.object(lifecycle.subprocess, 'run', return_value=result):
                    with self.assertRaises(RuntimeError): lifecycle.assert_no_docker(record)
                    removed, _ = lifecycle.uninstall(self.home, purge_data=True)
                    purge.finish(self.home, removed)
            finally:
                self.no_docker.start()
        self.assertFalse((self.project / 'cache').exists())

    @unittest.skipIf(sys.platform == 'win32', 'symlink creation requires a Windows privilege')
    def test_purge_unlinks_nested_symlinks_without_touching_their_targets(self):
        record = self.install()
        unrelated = self.root / 'unrelated'; unrelated.mkdir()
        (unrelated / 'keep').write_text('keep')
        (self.project / 'cache/link').symlink_to(unrelated, target_is_directory=True)
        with self.purge_context(record):
            result, _ = lifecycle.uninstall(self.home, purge_data=True)
            purge.finish(self.home, result)
        self.assertEqual('keep', (unrelated / 'keep').read_text())

    @unittest.skipIf(sys.platform == 'win32', 'symlink creation requires a Windows privilege')
    def test_purge_refuses_replaced_directory_symlinks(self):
        record = self.install()
        original = self.project / 'cache'; moved = self.project / 'saved-cache'
        original.rename(moved); original.symlink_to(moved, target_is_directory=True)
        with self.purge_context(record), patch.object(lifecycle, 'stop') as stop:
            with self.assertRaises(ValueError): lifecycle.uninstall(self.home, purge_data=True)
            stop.assert_not_called()
        self.assertEqual(b'complete-small-cache', (moved / 'unfinished.bin').read_bytes())

    def test_purge_cli_requires_confirmation_and_cancellation_is_read_only(self):
        from media_importer.native.cli import main
        record = self.install()
        with self.purge_context(record), patch.object(lifecycle, 'uninstall') as remove:
            with patch('sys.stdin', io.StringIO()), patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
                self.assertEqual(2, main(['--home', str(self.home), 'uninstall', '--purge']))
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='cancel'), patch('sys.stdout', io.StringIO()):
                self.assertEqual(0, main(['--home', str(self.home), 'uninstall', '--purge']))
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=EOFError), patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
                self.assertEqual(2, main(['--home', str(self.home), 'uninstall', '--purge']))
            remove.assert_not_called()
        self.assert_preserved()

    def test_purge_cli_releases_install_lock_before_final_removal(self):
        from media_importer.native.cli import main
        from media_importer.locking import file_lock
        record = self.install()
        original_finish = purge.finish
        def finish(home, result):
            with file_lock(home / 'install.lock', False) as free: self.assertTrue(free)
            original_finish(home, result)
        with self.purge_context(record), patch.object(purge, 'finish', side_effect=finish), patch('sys.stdout', io.StringIO()):
            self.assertEqual(0, main(['--home', str(self.home), 'uninstall', '--purge', '--yes']))
        self.assertFalse((self.project / 'cache').exists())

    def test_purge_does_not_use_paths_changed_after_user_confirmation(self):
        record = self.install()
        with self.purge_context(record):
            approved = purge.plan(self.home, record)
            moved = self.project / 'new-cache'; moved.mkdir()
            (self.project / '.env').write_text(self.env_text.replace('./cache', './new-cache'))
            record['paths']['cache'] = str(moved)
            lifecycle.save(self.home / 'installation.json', record)
            with patch.object(lifecycle, 'stop') as stop:
                with self.assertRaisesRegex(ValueError, '确认后发生变化'):
                    lifecycle.uninstall(self.home, purge_data=True, purge_plan=approved)
                stop.assert_not_called()
            self.assertTrue((self.project / 'cache/unfinished.bin').exists())

    def test_windows_purge_rejects_foreign_recovery_receipt_before_deleting_data(self):
        from media_importer.native.windows import purge_receipt
        record = self.install()
        receipt = purge_receipt(record['command'])
        receipt.write_text('{"home":"another-installation"}')
        with patch.object(sys, 'platform', 'win32'), self.purge_context(record), patch.object(lifecycle, 'stop') as stop:
            with self.assertRaisesRegex(ValueError, '恢复记录'):
                lifecycle.uninstall(self.home, purge_data=True)
            stop.assert_not_called()
        self.assert_preserved()

    def test_windows_cleanup_handoff_is_only_created_after_data_removal(self):
        from media_importer.native.windows import purge_receipt
        record = self.install()
        receipt = purge_receipt(record['command'])
        with patch.object(sys, 'platform', 'win32'), self.purge_context(record):
            with patch.object(purge, 'remove_tree', side_effect=PermissionError('locked data')):
                with self.assertRaises(PermissionError):
                    lifecycle.uninstall(self.home, purge_data=True)
            self.assertFalse(receipt.exists())
            self.assertTrue((self.project / 'cache/unfinished.bin').exists())
            result, _ = lifecycle.uninstall(self.home, purge_data=True)
            purge.finish(self.home, result)
        self.assertEqual(str(self.home), json.loads(receipt.read_text())['home'])
        self.assertFalse((self.project / 'cache').exists())


class PlatformContractTests(unittest.TestCase):
    def test_macos_launch_agent_uses_argv_and_no_credentials(self):
        home = Path('/Users/example/Library/Application Support/MediaImporter')
        executable = home / 'releases/1.0.0/media-importer'
        value = plistlib.loads(platforms.definition(home, executable, 'darwin'))
        self.assertEqual([str(executable), '--home', str(home), 'run'], value['ProgramArguments'])
        self.assertTrue(value['RunAtLoad']); self.assertTrue(value['KeepAlive'])
        self.assertNotIn('EnvironmentVariables', value)

    def test_linux_and_windows_service_contracts_quote_paths_and_use_user_session(self):
        home = Path('/home/example/some space/100% app')
        linux = platforms.definition(home, home / 'media-importer', 'linux').decode()
        self.assertIn('100%% app', linux); self.assertIn('Restart=on-failure', linux)
        windows = platforms.definition(home, home / 'media-importer.exe', 'win32').decode('utf-16')
        self.assertIn('InteractiveToken', windows); self.assertIn('LeastPrivilege', windows)
        self.assertNotIn('Password', windows); self.assertIn('IgnoreNew', windows)

    def test_archive_traversal_and_escaping_symlinks_are_rejected(self):
        for name, link in (('../escape', None), ('media-importer/link', '/etc/passwd')):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); archive = root / 'bad.tar.gz'
                with tarfile.open(archive, 'w:gz') as handle:
                    entry = tarfile.TarInfo(name)
                    if link: entry.type = tarfile.SYMTYPE; entry.linkname = link
                    else: entry.size = 1
                    handle.addfile(entry, io.BytesIO(b'x') if not link else None)
                with self.assertRaises((ValueError, tarfile.FilterError)):
                    releases.unpack(archive, root / 'out')
                self.assertFalse((root / 'escape').exists())

    def test_file_lock_contends_across_processes(self):
        import subprocess
        import sys
        from media_importer.locking import file_lock
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lock'
            with file_lock(path):
                result = subprocess.run([sys.executable, '-c',
                    'from media_importer.locking import file_lock\nimport sys\nwith file_lock(sys.argv[1],False) as ok: sys.exit(1 if ok else 0)', str(path)])
                self.assertEqual(0, result.returncode)
