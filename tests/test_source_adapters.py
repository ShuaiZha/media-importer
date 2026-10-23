"""An independent source fixture exercises the shared pipeline with tiny files."""
import copy
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from media_importer import adapters
from media_importer.config import Config, preferences
from media_importer.media import category
from media_importer.model import Asset, ConfigError, SourceError
from media_importer.service import Service
from media_importer.settings import Settings
from media_importer.source_connection import KEY, decide, observe, reason, run
from media_importer.store import Store
from test_bot import ENV
from test_service import FakeDestination, configuration, footage


KIND = 'fixture_source'
SERIAL = 'C0000000000123'  # Deliberately also used by the GoPro fixtures.
URL = 'fixture://device'


def defaults():
    return dict(type=KIND, url='', serial='', transport='usb', timeout_seconds=7, request_attempts=2)


def identity():
    return dict(type=KIND, serial_number=SERIAL, model_name='Fixture Camera', firmware_version='fixture-1',
                url=URL, transport='usb', compatibility=dict(import_supported=True, delete_verified=True,
                                                           validation='模拟验证', reason='仅用于隔离测试'))


class FixtureSource:
    """No GoPro inheritance, metadata, filename convention, or HTTP endpoint."""
    def __init__(self):
        self.payloads = {'clips/clip.MOV': b'small-video-fixture', 'clips/clip.preview': b'preview'}
        self.assets = {path: Asset(SERIAL, path, len(body), '1', '1', 'fixture:' + path,
                                   'FixtureCamera', KIND, {'kind': 'video'})
                       for path, body in self.payloads.items()}
        self.downloads, self.deletes = [], []
        self.external_removal = False

    def identify(self):
        return identity()

    def snapshot(self):
        return list(self.assets.values())

    def paths(self):
        return set(self.assets)

    @staticmethod
    def groups(assets):
        members = list(assets)
        return [] if not members else [dict(id=members[0].identity, members=members, safe=True, reason=None,
                                            preview_ids=[a.identity for a in members if a.path.endswith('.preview')])]

    def download(self, asset, path, progress):
        if self.assets.get(asset.path) != asset:
            raise SourceError('fixture identity changed')
        Path(path).write_bytes(self.payloads[asset.path])
        self.downloads.append(asset.path)
        progress(asset.size, asset.size)

    @staticmethod
    def deletion_candidates(assets):
        return {asset.identity: None for asset in assets}

    def delete(self, asset):
        self.deletes.append(asset.path)
        del self.assets[asset.path]
        if self.external_removal:
            self.assets.clear()


class SourceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = FixtureSource()
        module = types.ModuleType('media_importer.adapters._fixture')
        module.defaults = defaults
        module.validate = lambda values: None
        module.create = lambda config, stop=None: self.source
        module.media_category = lambda asset: asset.metadata.get('kind')
        module.discover = lambda hints, stop, scan=False: identity()
        self.adapter = module
        for changes in (patch.dict(adapters.SOURCES, {KIND: '._fixture'}),
                        patch.dict(sys.modules, {module.__name__: module}),
                        patch.dict(os.environ, {**ENV, 'SOURCE_TYPE': KIND, 'SOURCE_URL': URL,
                                                'SOURCE_SERIAL': SERIAL}, clear=True)):
            changes.start()
            self.addCleanup(changes.stop)
        self.path = self.root / 'settings.json'
        raw = preferences(configuration(archive_previews=False).values)
        raw['source_options'] = {k:v for k,v in defaults().items() if k != 'transport'}
        raw['source_options'].update(url=URL, serial=SERIAL)
        raw['backup']['source_transport'] = 'usb'
        self.path.write_text(json.dumps(raw))
        self.store = Store.create(self.root / 'data')
        self.addCleanup(lambda: self.store.close())
        self.settings = Settings(self.path, self.store.directory)
        self.remote = FakeDestination()
        self.stop = threading.Event()

    def config(self):
        return Config.from_values(self.settings.read()['config'])

    def service(self):
        config = self.config()
        return Service(config, self.store, adapters.create_source(config, self.stop), self.remote, self.stop)

    def confirm(self):
        state = observe(self.store, identity())
        decide(self.store, self.settings, state['id'], True)

    def test_discovery_loop_dispatches_to_configured_source(self):
        calls = []
        def discover(hints, stop, scan=False):
            calls.append((hints, scan))
            stop.set()
            return identity()
        self.adapter.discover = discover
        with patch('media_importer.adapters.gopro_discovery.discover', side_effect=AssertionError('wrong adapter')):
            run(self.store.directory, self.settings, self.stop)
        self.assertEqual(1, len(calls))
        self.assertFalse(calls[0][1])
        self.assertEqual(KIND, calls[0][0][0]['type'])  # Configured hint is tried first.
        self.assertEqual(identity(), self.store.get_meta(KEY)['device'])

    def test_connections_use_adapter_defaults_and_reject_explicit_empty_type(self):
        source = self.config().source
        self.assertEqual((KIND, 7, 2), (source['type'], source['timeout_seconds'], source['request_attempts']))
        raw = json.loads(self.path.read_text()); raw['source_options']['type'] = ''
        self.path.write_text(json.dumps(raw))
        self.assertIsNotNone(self.settings.read()['error'])

    def test_setup_leaves_address_and_serial_validation_to_adapter(self):
        from test_setup import wizard
        project = self.root / 'new-install'
        project.mkdir()
        answers = iter([URL, 'camera-A-123', './cache', 'storage.example.com', '', 'media', '',
                        'example', '', 'cli_EXAMPLE00000000', 'ou_EXAMPLE00000000'])
        with patch.dict(adapters.SOURCES, {KIND: '._fixture'}, clear=True), patch('builtins.print'):
            wizard.setup(project, read=lambda _: next(answers), hidden=lambda _: 'example-only',
                         probe=lambda _: None, checks=None)
        raw = json.loads((project / 'config/settings.json').read_text())
        self.assertEqual('fixture_source', raw['source_options']['type'])
        self.assertEqual('camera-A-123', raw['source_options']['serial'])
        self.assertEqual('fixture://device', raw['source_options']['url'])

    def test_monitoring_uses_adapter_clients_and_keep_alive_interval(self):
        self.confirm()
        self.store.status(source_work=True)
        purposes, calls = [], []
        observed = threading.Event()
        def fork(stop=None, *, purpose='transfer'):
            purposes.append(purpose)
            return self.source
        def keep_alive():
            calls.append(1)
            if len(calls) >= 2:
                observed.set()
        self.source.fork = fork
        self.source.keep_alive = keep_alive
        self.source.keep_alive_interval_seconds = .01
        self.source.timeout, self.source.attempts = 7, 2
        with self.service().monitoring():
            self.assertTrue(observed.wait(1), 'adapter heartbeat interval was not used')
        self.assertEqual({'monitor', 'keep_alive'}, set(purposes))
        self.assertEqual((7, 2), (self.source.timeout, self.source.attempts))

    def test_same_serial_and_address_never_reuse_another_type_approval(self):
        self.confirm()
        values = copy.deepcopy(self.config().values)
        values['source'].update(type='gopro', url='http://camera.test')
        # Compare identical addresses without asking GoPro to validate a fixture URL.
        state = self.store.get_meta(KEY)
        state['device']['url'] = values['source']['url']
        self.store.set_meta(KEY, state)
        self.assertIsNotNone(reason(self.store, Config.from_values(values)))
        raw = json.loads(self.path.read_text()); raw['source_options'].update(type='gopro', url=values['source']['url'])
        self.path.write_text(json.dumps(raw))
        with self.assertRaises(ConfigError):
            decide(self.store, self.settings, state['id'], True)

    def test_same_serial_with_other_type_unfinished_work_blocks_switch(self):
        self.store.batch(self.config().destination_id, [footage()])
        state = observe(self.store, identity())
        before = self.path.read_bytes(), self.store.files()
        with self.assertRaises(ConfigError):
            decide(self.store, self.settings, state['id'], True)
        self.assertEqual(before, (self.path.read_bytes(), self.store.files()))

    def test_complete_cache_and_assigned_paths_survive_restart_without_reimport(self):
        self.settings.update_preferences({'backup': {'archive_previews': True}})
        self.confirm()
        before_settings = self.path.read_bytes()
        service = self.service()
        self.remote.online = False
        service.cycle()
        rows = self.store.files()
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row['state'] == 'cached' for row in rows))
        # A path already reserved before an earlier attempt must remain stable.
        self.remote.online = True
        for row in rows:
            self.store.update(row['id'], remote_path=service.allocate(row))
        rows = self.store.files()
        saved = {row['id']: (row['asset_id'], row['asset'], row['remote_path'],
                              self.store.cached_path(row).read_bytes()) for row in rows}
        self.store.close()
        self.store = Store.create(self.root / 'data')
        self.remote.online = True
        self.assertTrue(self.service().cycle()['complete'])
        self.assertEqual(before_settings, self.path.read_bytes())
        self.assertEqual(2, len(self.source.downloads))
        for row in self.store.files():
            original = saved[row['id']]
            self.assertEqual(original[:2], (row['asset_id'], row['asset']))
            self.assertEqual(original[2], row['remote_path'])
            self.assertEqual(original[3], self.remote.objects[row['remote_path']])
            self.assertEqual('uploaded', row['state'])
            self.assertFalse(self.store.cached_path(row).exists())
            self.assertEqual('FixtureCamera-' + SERIAL, str(Path(row['remote_path']).parent))
        self.assertEqual([], self.source.deletes)

    def test_adapter_preview_policy_and_single_file_cleanup_use_generic_pipeline(self):
        self.settings.update_preferences({'backup': {'delete_source_after_import': True}})
        self.confirm()
        self.assertTrue(self.service().cycle()['complete'])
        self.assertEqual(['clips/clip.MOV'], self.source.downloads)
        self.assertEqual(1, len(self.remote.uploads))
        self.assertEqual(['clips/clip.MOV', 'clips/clip.preview'], self.source.deletes)
        self.assertTrue(all(row['cleanup'] == 'deleted' for row in self.store.files()))

    def test_unrelated_absence_is_not_claimed_as_delete_side_effect(self):
        self.settings.update_preferences({'backup': {'delete_source_after_import': True}})
        self.confirm()
        self.source.external_removal = True
        self.service().cycle()
        rows = {self.store.asset(row).path: row for row in self.store.files()}
        self.assertEqual('deleted', rows['clips/clip.MOV']['cleanup'])
        self.assertEqual('absent', rows['clips/clip.preview']['cleanup'])
        self.assertIsNone(rows['clips/clip.preview']['source_deleted_at'])

    def test_adapter_cannot_expand_delete_beyond_verified_group(self):
        self.settings.update_preferences({'backup': {'delete_source_after_import': True}})
        self.confirm()
        self.source.deletion_effects = lambda asset, members: {asset.identity, 'unrelated'}
        self.service().cycle()
        self.assertEqual([], self.source.deletes)
        self.assertTrue(any(row['source_cleanup_error'] for row in self.store.files()))

    def test_special_formats_do_not_leak_into_other_adapters_or_change_identity(self):
        original = footage(primary=False)
        before = original.to_dict()
        self.assertEqual('video', category(original))
        plain = Asset(SERIAL, 'some.LRV', 1, '', '', 'opaque', 'Fixture', KIND)
        self.assertEqual('other', category(plain))
        self.assertEqual('video', category(self.source.snapshot()[0]))
        self.assertEqual(before, original.to_dict())


if __name__ == '__main__':
    unittest.main()
