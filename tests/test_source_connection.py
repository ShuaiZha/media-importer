"""USB identity confirmation uses tiny media, isolated records and no network."""
import copy
import json
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from media_importer.adapters.gopro import GoProSource, make_asset
from media_importer.adapters.gopro_discovery import discover, usb_url
from media_importer.adapters.gopro_models import compatibility
from media_importer.bot import Bot
from media_importer.config import Config
from media_importer.model import ConfigError, SourceError, now
from media_importer.service import Service
from media_importer.settings import Settings
from media_importer.source_connection import (KEY, ConfirmationRequired, decide, fresh,
                                              observe, reason, require)
from media_importer.status import snapshot
from media_importer.store import Store
import test_bot
from test_service import configuration, footage, FakeSource, FakeDestination, replace


def device(model='HERO9 Black', firmware='HD9.01.01.72.00', serial='C0000000000123'):
    return {'type': 'gopro', 'model_name': model, 'firmware_version': firmware,
            'serial_number': serial, 'url': usb_url(serial), 'transport': 'usb',
            'compatibility': compatibility(model, firmware)}


class DiscoveryTests(unittest.TestCase):
    def test_usb_mapping_is_bounded_and_uses_serial_tail(self):
        self.assertEqual('http://172.21.123.51:8080', usb_url('C0000000000123'))
        self.assertEqual('http://172.20.100.51:8080', usb_url('000'))
        self.assertEqual('http://172.29.199.51:8080', usb_url('999'))
        self.assertIsNone(usb_url('bad'))
        self.assertIsNone(usb_url('12'))
        self.assertEqual(1000, len({usb_url(f'{i:03}') for i in range(1000)}))

    def test_successful_hint_does_not_scan_network_or_read_media(self):
        found = device()
        with patch('media_importer.adapters.gopro_discovery.probe', return_value=found) as probe, \
                patch('media_importer.adapters.gopro_discovery.socket.create_connection') as connect:
            self.assertEqual(found, discover([None, found['url']], threading.Event(), scan=True))
        probe.assert_called_once()
        connect.assert_not_called()

    def test_discovered_identity_must_match_usb_address(self):
        found = device()
        def open_socket(address, **kwargs):
            if address[0] != '172.21.123.51':
                raise OSError('fixture offline')
            from contextlib import nullcontext
            return nullcontext()
        with patch('media_importer.adapters.gopro_discovery.socket.create_connection', side_effect=open_socket), \
                patch('media_importer.adapters.gopro_discovery.probe', return_value=found) as probe:
            self.assertEqual(found, discover([], threading.Event(), scan=True))
            probe.return_value = device(serial='C0000000000999')
            self.assertIsNone(discover([], threading.Event(), scan=True))

    def test_cancelled_discovery_never_opens_connections(self):
        stop = threading.Event(); stop.set()
        with patch('media_importer.adapters.gopro_discovery.probe') as probe:
            self.assertIsNone(discover([usb_url('123')], stop, scan=True))
            probe.assert_not_called()

    def test_adapter_derives_usb_hints_and_ignores_other_source_types(self):
        from media_importer.adapters import discover_source
        hints = [{'type': 'other', 'url': 'other://device', 'serial': '999'},
                 {'type': 'gopro', 'url': 'http://camera.test', 'serial': '123'}]
        stop = threading.Event()
        with patch('media_importer.adapters.gopro_discovery.discover', return_value=device()) as search:
            self.assertEqual(device(), discover_source('gopro', hints, stop))
        search.assert_called_once_with(['http://camera.test', usb_url('123')], stop, scan=False)

    def test_model_profile_does_not_claim_all_cameras_validated(self):
        self.assertTrue(compatibility('HERO9 Black', 'HD9.01.01.72.00')['delete_verified'])
        for name, firmware in [('HERO9 Black', 'unknown'), ('HERO10 Black', '1'),
                               ('HERO11 Black Mini', '1'), ('HERO12 Black', '1'), ('HERO13 Black', '1')]:
            profile = compatibility(name, firmware)
            self.assertTrue(profile['import_supported'], name)
            self.assertFalse(profile['delete_verified'], name)
        for name in ('MAX', 'HERO8 Black', 'HERO', 'HERO9 Mini', 'unknown'):
            self.assertFalse(compatibility(name)['import_supported'], name)

    def test_identity_and_file_ids_do_not_depend_on_display_model(self):
        old = footage()
        generic = replace(old, device_name='GoPro')
        other = replace(old, device_name='HERO12')
        self.assertEqual(old.identity, generic.identity)
        self.assertEqual(old.identity, other.identity)
        self.assertEqual(Store.file_id('destination', old), Store.file_id('destination', other))
        self.assertEqual('HERO12-C0000000000123', other.device_directory)
        asset = make_asset(old.serial, old.path, old.size, old.created, old.modified, 'gumi', True)
        self.assertEqual('GoPro', asset.device_name)

    def test_hero9_metadata_workaround_is_not_applied_to_other_models(self):
        source = GoProSource(configuration())
        source.compatibility = compatibility('HERO12 Black', '1')
        error = SourceError('fixture metadata unavailable')
        error.__cause__ = HTTPError('http://camera', 404, 'missing', {}, None)
        with patch.object(source, '_json', side_effect=error), patch.object(source, '_mp4_media_info') as fallback:
            with self.assertRaises(SourceError): source.media_info('100GOPRO/GH020001.MP4')
            fallback.assert_not_called()

    def test_unverified_adapter_never_issues_delete_request(self):
        source = GoProSource(configuration())
        source.compatibility = compatibility('HERO12 Black', '1')
        with patch.object(source, 'verify_primary'), patch.object(source, '_json') as request:
            with self.assertRaises(SourceError): source.delete(footage())
            request.assert_not_called()


class ConnectionTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    callback = test_bot.BotTests.callback

    def config(self):
        return Config.from_values(self.settings.read()['config'])

    def confirmed(self, candidate=None):
        state = observe(self.store, candidate or device())
        self.assertTrue(decide(self.store, self.settings, state['id'], True))
        return state

    def test_empty_source_hints_are_valid_but_cannot_read_media(self):
        with patch.dict(os.environ, {'SOURCE_URL': '', 'SOURCE_SERIAL': ''}):
            values = self.settings.read()
            self.assertIsNone(values['error'])
            source = GoProSource(Config.from_values(values['config']))
            with self.assertRaises(SourceError): source.identify()

    def test_confirmation_retains_preferences_and_environment(self):
        original = json.loads(self.path.read_text())
        environment = dict(os.environ)
        state = self.confirmed()
        saved = json.loads(self.path.read_text())
        original['backup']['enabled'] = True
        self.assertEqual(original, saved)
        self.assertEqual(environment, dict(os.environ))
        self.assertIsNone(reason(self.store, self.config()))
        self.assertEqual('approved', self.store.get_meta(KEY)['state'])
        self.assertEqual(state['id'], observe(self.store, device())['id'])
        # Selection survives restart, approval does not.
        self.store.set_meta(KEY, {'state': 'searching', 'online': False})
        self.assertEqual(device()['serial_number'], Settings(self.path, self.store.directory).read()['config']['source']['serial'])
        with self.assertRaises(ConfirmationRequired): require(self.store, self.config())
        replacement = observe(self.store, device())
        self.assertNotEqual(state['id'], replacement['id'])
        with self.assertRaises(ConfigError): decide(self.store, self.settings, state['id'], True)

    def test_approval_is_not_reused_after_disconnect_identity_or_firmware_change(self):
        previous = self.confirmed()
        stamp = time.time()
        observe(self.store, None, stamp)
        observe(self.store, None, stamp + 10)
        offline = observe(self.store, None, stamp + 21)
        self.assertEqual('offline', offline['state'])
        with self.assertRaises(ConfirmationRequired): require(self.store, self.config())
        connected = observe(self.store, device())
        self.assertNotEqual(previous['id'], connected['id'])
        with self.assertRaises(ConfigError): decide(self.store, self.settings, previous['id'], True)
        next_connection = observe(self.store, device(firmware='new'))
        self.assertNotEqual(connected['id'], next_connection['id'])
        changed = observe(self.store, device(serial='C0000000000999'))
        self.assertNotEqual(next_connection['id'], changed['id'])

    def test_short_probe_failure_does_not_duplicate_confirmation_but_stale_blocks(self):
        self.confirmed()
        stamp = time.time()
        state = observe(self.store, None, stamp)
        self.assertEqual('approved', state['state'])
        self.assertTrue(fresh(state))
        state['last_seen'] = stamp - 46
        self.store.set_meta(KEY, state)
        with self.assertRaises(ConfirmationRequired): require(self.store, self.config())
        with self.assertRaises(ConfigError): decide(self.store, self.settings, state['id'], True)

    def test_duplicate_confirmation_cannot_restart_stopped_sync(self):
        state = self.confirmed()
        self.settings.set_enabled(False)
        self.assertFalse(decide(self.store, self.settings, state['id'], True))
        self.assertFalse(self.config().backup['enabled'])
        with self.assertRaises(ConfigError): decide(self.store, self.settings, state['id'], False)

    def test_decline_preserves_settings_tasks_and_cache(self):
        self.store.batch(self.config().destination_id, [footage()])
        cache = self.store.cached_path(self.store.files()[0]); cache.write_bytes(b'complete fixture')
        before = self.path.read_bytes(), self.store.files(), cache.read_bytes()
        state = observe(self.store, device())
        decide(self.store, self.settings, state['id'], False)
        self.assertEqual(before, (self.path.read_bytes(), self.store.files(), cache.read_bytes()))
        with self.assertRaises(ConfirmationRequired): require(self.store, self.config())
        self.assertTrue(decide(self.store, self.settings, state['id'], True))

    def test_switch_rejects_other_camera_unfinished_work(self):
        self.store.batch(self.config().destination_id, [footage()])
        state = observe(self.store, device(serial='C0000000000999'))
        before = self.path.read_bytes(), self.store.files()
        with self.assertRaises(ConfigError): decide(self.store, self.settings, state['id'], True)
        self.assertEqual(before, (self.path.read_bytes(), self.store.files()))

    def test_confirmed_different_camera_is_selected_without_rewriting_env(self):
        self.confirmed(device(serial='C0000000000999'))
        self.assertEqual('C0000000000999', self.config().source['serial'])
        self.assertEqual('C0000000000123', os.environ['SOURCE_SERIAL'])
        self.assertEqual('C0000000000123', self.settings.read()['source_hint']['serial'])

    def test_unknown_model_cannot_be_approved(self):
        state = observe(self.store, device(model='MAX'))
        with self.assertRaises(ConfigError): decide(self.store, self.settings, state['id'], True)
        self.assertFalse(self.config().backup['enabled'])
        response = self.bot.panel('source_confirmation')
        self.assertFalse(any(e['tag'] == 'action' for e in response['elements']))

    def test_unconfirmed_small_file_has_no_import_or_delete(self):
        self.settings.set_enabled(True)
        observe(self.store, device())
        source, target = FakeSource([footage()]), FakeDestination()
        service = Service(self.config(), self.store, source, target, threading.Event())
        result = service.cycle()
        self.assertFalse(result['complete'])
        self.assertEqual('source_confirmation', result['reason'])
        self.assertEqual([], source.downloads)
        self.assertEqual([], source.deletes)
        self.assertEqual(0, source.snapshot_calls)
        self.assertEqual([], self.store.files())

    def test_verified_model_small_group_only_deletes_after_confirmation_and_cache(self):
        self.settings.update_preferences({'backup': {'delete_source_after_import': True}})
        state = observe(self.store, device())
        source = FakeSource([footage(), footage(primary=False)])
        target = FakeDestination()
        Service(self.config(), self.store, source, target, threading.Event()).cycle()
        self.assertEqual([], source.downloads)
        self.assertEqual([], source.deletes)
        decide(self.store, self.settings, state['id'], True)
        service = Service(self.config(), self.store, source, target, threading.Event())
        def check_before_delete(asset):
            db = Store(self.store.directory)
            try:
                check = Service(service.config, db, source, target, threading.Event())
                rows = check.rows()
                self.assertEqual(2, len(rows))
                self.assertTrue(all(check.cache_valid(r) or r['state'] == 'uploaded' for r in rows))
            finally:
                db.close()
        source.on_delete = check_before_delete
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(2, len(target.uploads))
        self.assertEqual([footage().path], source.deletes)
        self.assertEqual({}, source.assets)
        self.assertTrue(all(r['cleanup'] == 'deleted' for r in self.store.files()))
        self.assertFalse(list(self.store.cache.glob('*.bin')))

    def test_complete_cache_uploads_while_current_camera_is_unconfirmed(self):
        self.confirmed()
        source, target = FakeSource([footage()]), FakeDestination()
        service = Service(self.config(), self.store, source, target, threading.Event())
        from media_importer.storage import initialize_cache
        initialize_cache(self.store)
        service.prepare_imports()
        service.import_asset(service.rows()[0])
        observe(self.store, device(serial='C0000000000999'))
        service.cycle()
        self.assertEqual(1, len(target.uploads))
        self.assertEqual('uploaded', self.store.files()[0]['state'])
        self.assertFalse(list(self.store.cache.glob('*.bin')))
        self.assertEqual([], source.deletes)

    def test_unverified_model_imports_small_file_without_deleting_or_stuck_cleanup(self):
        self.settings.update_preferences({'backup': {'delete_source_after_import': True}})
        self.confirmed(device(model='HERO12 Black', firmware='1'))
        source, target = FakeSource([replace(footage(), device_name='HERO12')]), FakeDestination()
        service = Service(self.config(), self.store, source, target, threading.Event())
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(1, len(target.uploads))
        self.assertEqual([], source.deletes)
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now())
        view = snapshot(self.store.directory, self.settings)
        self.assertTrue(view['policy']['delete_source_requested'])
        self.assertFalse(view['policy']['delete_source'])
        self.assertEqual(0, view['cleanup']['source_pending'])
        self.assertEqual(1, service.settle())
        self.assertTrue(self.config().backup['delete_source_after_import'])
        self.assertEqual(1, len(self.store.db.execute('SELECT * FROM source_index').fetchall()))

    def test_pending_connection_never_claims_old_inventory_complete(self):
        self.confirmed()
        self.store.set_meta('service', {'running': True})
        self.store.status(worker_heartbeat_at=now(), configuration={'applied_revision': self.settings.read()['revision']},
                          source={'online': True, 'checked_at': now()},
                          inventory={'verified': True, 'scanned': True, 'batch_id': None,
                                     'destination': self.config().destination_id,
                                     'serial': self.config().source['serial']})
        observe(self.store, device(serial='C0000000000999'))
        view = snapshot(self.store.directory, self.settings)
        self.assertFalse(view['unplug']['ready'])
        self.assertFalse(view['batch']['scanned'])
        response = self.bot.panel()
        self.assertEqual('相机待确认', response['header']['title']['content'])
        self.assertIn('确认相机', json.dumps(response, ensure_ascii=False))
        connection = self.store.get_meta(KEY)
        decide(self.store, self.settings, connection['id'], True)
        self.assertFalse(snapshot(self.store.directory, self.settings)['batch']['scanned'])

    def test_bot_connection_notice_once_even_with_progress_disabled(self):
        self.assertFalse(self.config().values['notifications']['enabled'])
        state = observe(self.store, device())
        self.bot.announce_source(); self.bot.announce_source()
        self.assertTrue(self.bot.process_one())
        self.assertFalse(self.bot.process_one())
        self.sender.assert_called_once()
        response = self.sender.call_args.args[1]
        actions = [b for e in response['elements'] if e['tag'] == 'action' for b in e['actions']]
        accept = next(b['value'] for b in actions if b['text']['content'] == '确认并备份')
        self.assertEqual('error', self.bot.receive_card(self.callback(accept, owner='ou_other'))['toast']['type'])
        self.assertFalse(self.config().backup['enabled'])
        self.bot.receive_card(self.callback(accept))
        self.assertTrue(self.bot.process_one())
        self.assertTrue(self.config().backup['enabled'])
        self.assertEqual('approved', self.store.get_meta(KEY)['state'])
        self.assertEqual(state['id'], self.store.get_meta(KEY)['id'])

    def test_stale_queued_notice_not_sent_for_new_device(self):
        observe(self.store, device()); self.bot.announce_source()
        observe(self.store, device(serial='C0000000000999'))
        self.bot.process_one()
        self.sender.assert_not_called()

    def test_start_menu_returns_confirmation_instead_of_enabling(self):
        observe(self.store, device())
        self.bot.enqueue('start', {'changes': {'backup': {'enabled': True}}})
        self.assertTrue(self.bot.process_one())
        self.assertFalse(self.config().backup['enabled'])
        self.assertEqual('相机待确认', self.sender.call_args.args[1]['header']['title']['content'])
