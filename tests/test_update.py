import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('stable_update', Path(__file__).parents[1] / 'scripts/update.py')
update = importlib.util.module_from_spec(spec); spec.loader.exec_module(update)


def manifest():
    return {'format': 1, 'version': '0.10.16', 'updater_version': 1, 'config_version': 4,
            'database_version': 5, 'commit': 'b' * 40, 'image': update.REGISTRY + '@sha256:' + 'c' * 64}


class ReleaseTests(unittest.TestCase):
    def response(self):
        return {'tag_name': 'v0.10.16', 'draft': False, 'prerelease': False, 'assets': [{
            'name': 'release.json', 'browser_download_url': 'https://github.com/ShuaiZha/media-importer/releases/download/v0.10.16/release.json'}]}

    def test_stable_release_is_digest_pinned_and_can_select_version(self):
        with patch.object(update, 'fetch_json', side_effect=[self.response(), manifest()]) as fetch:
            self.assertEqual(manifest(), update.release('0.10.16'))
            self.assertTrue(fetch.call_args_list[0].args[0].endswith('/tags/v0.10.16'))

    def test_default_update_uses_latest_asset_without_rest_rate_quota(self):
        with patch.object(update, 'fetch_json', return_value=manifest()) as fetch:
            self.assertEqual(manifest(), update.release())
            fetch.assert_called_once_with('https://github.com/ShuaiZha/media-importer/releases/latest/download/release.json')

    def test_untrusted_images_and_incompatible_contracts_rejected(self):
        for field, value in [('image', 'evil.example/image@sha256:' + 'd'*64), ('image', update.REGISTRY + ':stable'),
                             ('version', '0.10.17'), ('commit', 'main'), ('updater_version', 2),
                             ('config_version', 9), ('database_version', 9), ('format', 2)]:
            data = manifest(); data[field] = value
            with self.subTest(field=field, value=value), patch.object(update, 'fetch_json', side_effect=[self.response(), data]):
                with self.assertRaises(ValueError): update.release('0.10.16')

    def test_prerelease_and_foreign_asset_urls_rejected(self):
        for field, value in [('prerelease', True), ('draft', True), ('tag_name', 'v1.0.0-rc1'),
                             ('assets', [{'name': 'release.json', 'browser_download_url': 'https://evil.example/release.json'}])]:
            response = self.response(); response[field] = value
            with patch.object(update, 'fetch_json', return_value=response):
                with self.assertRaises(ValueError): update.release('0.10.16')

    def test_external_error_does_not_echo_secret(self):
        with patch.object(update.subprocess, 'run', return_value=type('Result', (), {'returncode': 1, 'stderr': 'private-secret'})()):
            with self.assertRaises(RuntimeError) as caught: update.run(['docker', 'compose'], Path('.'))
        self.assertNotIn('private-secret', str(caught.exception))

    def test_snapshot_includes_uncheckpointed_wal_without_writing_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('input', 'output', 'tmp'): (root/name).mkdir()
            source = root/'input/state.sqlite3'
            connection = sqlite3.connect(source)
            try:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA wal_autocheckpoint=0')
                connection.execute('PRAGMA user_version=5')
                connection.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY, state TEXT)')
                connection.execute("INSERT INTO tasks VALUES (1, 'cached')")
                connection.commit()
                before = {p: p.read_bytes() for p in source.parent.iterdir()}
                script = re.sub(r"'/(input|output|tmp)/", lambda m: "'"+(root/m[1]).as_posix()+'/', update.SNAPSHOT)
                exec(script, {})
                self.assertEqual(before, {p: p.read_bytes() for p in source.parent.iterdir()})
                with sqlite3.connect(root/'output/state.sqlite3') as backup:
                    self.assertEqual([(1, 'cached')], backup.execute('SELECT * FROM tasks').fetchall())
                    self.assertEqual(5, backup.execute('PRAGMA user_version').fetchone()[0])
            finally:
                connection.close()

    def test_readonly_cache_validation_accepts_bound_volume_and_rejects_wrong_marker(self):
        from media_importer.store import Store
        from media_importer.storage import MARKER
        with tempfile.TemporaryDirectory() as folder, patch.dict(update.os.environ, {'MEDIA_IMPORTER_REQUIRE_CACHE_MOUNT': '0'}):
            root = Path(folder)
            for name in ('snapshot', 'cache', 'tmp'): (root/name).mkdir()
            store = Store.create(root/'snapshot')
            store.set_meta('cache_identity', 'test-original-volume'); store.close()
            marker = root/'cache'/MARKER; marker.write_text('test-original-volume\n')
            cached = root/'cache/complete.bin'; cached.write_bytes(b'keep-complete-cache')
            script = re.sub(r"'/(snapshot|cache|tmp)", lambda m: "'"+(root/m[1]).as_posix(), update.VALIDATE)
            with patch('media_importer.config.Config.load'), patch('media_importer.storage.mounted', return_value=True), \
                    patch('os.access', return_value=False):
                exec(script, {})
                self.assertEqual(b'keep-complete-cache', cached.read_bytes())
                shutil.rmtree(root/'tmp/probe')
                marker.write_text('wrong-volume\n')
                with self.assertRaises(AssertionError): exec(script, {})


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        for name in ('config', 'data', 'cache'): (self.root / name).mkdir()
        (self.root / '.env').write_text("DESTINATION_PASSWORD='private$#value'\n")
        (self.root / 'config/settings.json').write_text(json.dumps({'version': 4, 'backup': {'enabled': True, 'delete_source_after_import': True}}))
        (self.root / 'data/state.sqlite3').write_bytes(b'db-preserved-by-fixture')
        (self.root / 'cache/complete.bin').write_bytes(b'x' * 128)
        (self.root / 'compose.yaml').write_text('test-deployment')
        self.protected = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.paths = {'/config': self.root/'config', '/data': self.root/'data',
                      '/data/cache': self.root/'cache', '/run/media-importer.env': self.root/'.env'}
        self.calls = []; self.fail = None; self.running = True; self.initial_running = True
        self.dirty = False; self.tag_commit = manifest()['commit']; self.head = 'a'*40

    def tearDown(self): self.temp.cleanup()

    def command(self, args, root, capture=False):
        self.calls.append(args)
        if self.fail and self.fail(args): raise RuntimeError('fixture failure')
        if args[0] == 'git':
            if args[1:] == ['rev-parse', '--show-toplevel']: return str(root)
            if args[1:] == ['status', '--porcelain']: return ' M README.md' if self.dirty else ''
            if args[1:3] == ['remote', 'get-url']: return 'git@github.com:ShuaiZha/media-importer.git'
            if args[1:] == ['rev-parse', 'HEAD']: return self.head
            if args[1] == 'rev-parse': return self.tag_commit
            if args[1] == 'merge': self.head = manifest()['commit']
        if args[:3] == ['docker', 'compose', 'ps']:
            if '--services' in args: return 'backup\nbot\n' if self.running else ''
            return 'container-' + args[-1]
        if args[:3] == ['docker', 'compose', 'stop']: self.running = False
        if args[:3] == ['docker', 'compose', 'up']: self.running = True
        if args[:2] == ['docker', 'inspect']: return 'old-image-id'
        if args[:3] == ['docker', 'image', 'inspect']: return 'new-image-id'
        if args[-1] == '--version': return manifest()['version']
        return ''

    def perform(self):
        with patch.object(update, 'run', side_effect=self.command), patch.object(update, 'deployment', return_value=self.paths), \
                patch.object(update, 'image_python', side_effect=self.probe), contextlib.redirect_stdout(io.StringIO()):
            return update.perform(self.root, manifest())

    def probe(self, root, image, script, mounts, env=()):
        self.assertFalse(self.running)
        if script == update.SNAPSHOT:
            output = next(p for p,t,ro in mounts if t == '/output')
            (output/'state.sqlite3').write_bytes((self.root/'data/state.sqlite3').read_bytes())

    def unchanged(self):
        for path, value in self.protected.items(): self.assertEqual(value, path.read_bytes(), path.name)

    def test_update_preserves_settings_database_cache_and_waits_for_health(self):
        result = self.perform(); self.unchanged()
        self.assertTrue(result['changed'])
        saved = json.loads((self.root/'compose.override.yaml').read_text())
        self.assertEqual(manifest()['image'], saved['services']['backup']['image'])
        up = next(a for a in self.calls if a[:3] == ['docker','compose','up'])
        self.assertIn('--wait', up); self.assertIn('--no-build', up)
        pull = next(i for i,a in enumerate(self.calls) if a[:2] == ['docker','pull'])
        stop = next(i for i,a in enumerate(self.calls) if a[:3] == ['docker','compose','stop'])
        self.assertLess(pull, stop)
        self.assertEqual('complete', json.loads((Path(result['backup'])/'update.json').read_text())['state'])

    def test_pull_failure_never_stops_current_services(self):
        self.fail = lambda a: a[:2] == ['docker','pull']
        with self.assertRaises(RuntimeError): self.perform()
        self.unchanged(); self.assertTrue(self.running)
        self.assertFalse(any(a[:3] == ['docker','compose','stop'] for a in self.calls))

    def test_health_failure_stops_new_services_and_never_rewinds_database(self):
        self.fail = lambda a: a[:3] == ['docker','compose','up']
        with self.assertRaises(RuntimeError): self.perform()
        self.unchanged(); self.assertFalse(self.running)
        records = list((self.root/'backups').glob('*/update.json'))
        self.assertEqual('failed', json.loads(records[0].read_text())['state'])
        self.assertTrue((records[0].parent/'state.sqlite3').exists())

    def test_already_stopped_services_stay_stopped(self):
        self.running = False
        self.perform(); self.unchanged()
        self.assertFalse(any(a[:3] == ['docker','compose','up'] for a in self.calls))

    def test_local_edits_and_changed_tag_are_not_overwritten(self):
        self.dirty = True
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(any(a[0] == 'docker' for a in self.calls))
        self.dirty = False; self.tag_commit = 'd'*40; self.calls.clear()
        with self.assertRaises(ValueError): self.perform()
        self.unchanged(); self.assertTrue(self.running)

    def test_custom_override_is_not_replaced(self):
        override = self.root/'compose.override.yaml'; override.write_text('services:\n  backup:\n    privileged: false\n')
        with self.assertRaises(ValueError): self.perform()
        self.assertIn('privileged', override.read_text()); self.assertTrue(self.running)

    def test_missing_mount_is_rejected_without_creating_directories(self):
        spec = {'services': {name: {'volumes': [{'type':'bind','target': t,'source':str(p)} for t,p in self.paths.items()]} for name in update.SERVICES}}
        (self.root/'cache/complete.bin').unlink(); (self.root/'cache').rmdir()
        with patch.object(update,'run',return_value=json.dumps(spec)):
            with self.assertRaises(ValueError): update.deployment(self.root)
        self.assertFalse((self.root/'cache').exists())

    def test_update_lock_releases_after_failure(self):
        with self.assertRaises(RuntimeError):
            with update.update_lock(self.root): raise RuntimeError('fixture')
        with update.update_lock(self.root): pass
