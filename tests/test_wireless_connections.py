"""Transport switching keeps stable media identity and cannot interrupt SMB work."""
import copy
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_bot
from test_source_connection import device
from test_service import configuration
from media_importer.connections import ConnectionManager
from media_importer.config import Config
from media_importer.model import ConfigError, SourceConnectionError
from media_importer.source_connection import observe, decide, reason, selected
from media_importer.source_session import SourceSession


def wireless(serial='C0000000000123'):
    item = device(serial=serial)
    return {**item, 'url': 'http://10.5.5.9:8080', 'transport': 'wifi', 'http_ready': True,
            'compatibility': {**item['compatibility'], 'delete_verified': False}}


class ConnectionTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown

    def bind(self):
        state = observe(self.store, device())
        decide(self.store, self.settings, state['id'], True)
        self.settings.set_enabled(False)
        return state

    def test_same_camera_changes_route_without_new_consent_or_settings_token(self):
        first = self.bind()
        before = self.settings.token()[1]
        second = observe(self.store, wireless())
        self.assertEqual('approved', second['state'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(before, self.settings.token()[1])
        self.assertFalse(self.settings.read()['config']['backup']['enabled'])
        self.assertIsNone(reason(self.store, Config.from_values(self.settings.read()['config'])))
        with self.assertRaises(ConfigError):
            decide(self.store, self.settings, first['id'], True)

    def test_reconnect_trust_requires_an_actual_previous_approval(self):
        state = observe(self.store, wireless())
        self.assertEqual('pending', state['state'])
        self.bind()
        stamp = time.time()
        for tick in (stamp, stamp + 10, stamp + 21): observe(self.store, None, tick)
        self.assertEqual('approved', observe(self.store, wireless())['state'])
        self.assertEqual('pending', observe(self.store, wireless('C0000000000999'))['state'])

    def test_wireless_never_inherits_verified_usb_delete_capability(self):
        self.bind()
        item = wireless()
        item['compatibility']['delete_verified'] = True  # A USB proof is insufficient.
        observe(self.store, item)
        config = Config.from_values(self.settings.read()['config'])
        self.assertIsNotNone(reason(self.store, config, deleting=True))

    def test_verified_wireless_profile_does_not_enable_user_preference(self):
        from media_importer.adapters.gopro_models import compatibility
        from media_importer.source_connection import deletion_enabled
        self.bind()
        item = wireless();item['compatibility'] = compatibility(item['model_name'], item['firmware_version'], 'wifi')
        observe(self.store, item)
        raw = self.settings.read()['config'];raw['source']['transport'] = 'wifi'
        config = Config.from_values(raw)
        self.assertIsNone(reason(self.store, config, deleting=True))
        self.assertFalse(deletion_enabled(config, item))
        raw['backup']['delete_source_after_import'] = True
        self.assertTrue(deletion_enabled(Config.from_values(raw), item))

    def test_binding_refreshes_capability_without_changing_approval_or_settings(self):
        from media_importer.adapters.gopro_models import compatibility
        self.bind();before=self.settings.path.read_bytes()
        item=wireless();item['compatibility']=compatibility(item['model_name'],item['firmware_version'],'wifi')
        observe(self.store,item)
        binding=self.store.get_meta('source_selection')
        self.assertEqual(item['serial_number'],binding['serial_number'])
        self.assertEqual('wifi',binding['transport'])
        self.assertTrue(binding['compatibility']['delete_verified'])
        item['firmware_version']='unknown';item['compatibility']=compatibility(item['model_name'],'unknown','wifi')
        self.assertEqual('approved',observe(self.store,item)['state'])
        self.assertFalse(self.store.get_meta('source_selection')['compatibility']['delete_verified'])
        self.assertEqual(before,self.settings.path.read_bytes())

    def test_auto_prefers_usb_and_does_not_run_radio_when_usb_available(self):
        self.bind()
        manager = ConnectionManager(self.store, self.settings, threading.Event())
        with patch('media_importer.connections.discover_source', return_value=device()), \
                patch('media_importer.connections.source_link') as radio:
            manager.tick()
        self.assertEqual('usb', self.store.get_meta('source_connection')['device']['transport'])
        radio.assert_not_called()

    def test_preferred_usb_waits_for_wireless_download_boundary(self):
        self.bind(); observe(self.store, wireless()); self.store.status(source_work=True)
        manager = ConnectionManager(self.store, self.settings, threading.Event())
        with patch('media_importer.connections.discover_source', return_value=device()), \
                patch('media_importer.connections.wireless_supported', return_value=True), \
                patch('media_importer.connections.source_link', return_value=wireless()):
            manager.tick()
            self.assertEqual('wifi', self.store.get_meta('source_connection')['device']['transport'])
            self.store.status(source_work=False)
            manager.tick()
        self.assertEqual('usb', self.store.get_meta('source_connection')['device']['transport'])

    def test_disabled_sync_only_probes_and_does_not_join_wifi(self):
        self.bind()
        manager = ConnectionManager(self.store, self.settings, threading.Event())
        with patch('media_importer.connections.discover_source', return_value=None), \
                patch('media_importer.connections.wireless_supported', return_value=True), \
                patch('media_importer.connections.source_link', return_value=None) as radio:
            manager.tick()
        self.assertTrue(all(call.args[1] == 'probe' for call in radio.call_args_list))

    def test_connect_failure_has_bounded_retry_and_preserves_preference(self):
        self.bind()
        self.store.set_meta('source_connection', {'state': 'offline', 'online': False})
        self.settings.set_enabled(True)
        manager = ConnectionManager(self.store, self.settings, threading.Event())
        def radio(kind, action, *args):
            if action == 'probe': return None
            raise SourceConnectionError('fixture retry')
        with patch('media_importer.connections.discover_source', return_value=None), \
                patch('media_importer.connections.wireless_supported', return_value=True), \
                patch('media_importer.connections.source_link', side_effect=radio) as mocked:
            manager.tick(); manager.tick()
        self.assertEqual(1, sum(c.args[1] == 'connect' for c in mocked.call_args_list))
        self.assertTrue(self.settings.read()['config']['backup']['enabled'])

    def test_session_resolves_new_endpoint_without_recreating_destination(self):
        self.bind()
        config = Config.from_values(self.settings.read()['config'])
        usb, wifi = Mock(), Mock()
        usb.fork.return_value = usb; wifi.fork.return_value = wifi
        usb.identify.return_value = device(); wifi.identify.return_value = wireless()
        def client(config, stop): return wifi if config.source['transport'] == 'wifi' else usb
        with patch('media_importer.source_session.create_source', side_effect=client):
            source = SourceSession(config, self.store.directory, threading.Event())
            self.assertEqual('usb', source.identify()['transport'])
            observe(self.store, wireless())
            self.assertEqual('wifi', source.identify()['transport'])
            observe(self.store, wireless('C0000000000999'))
            with self.assertRaises(SourceConnectionError): source.identify()

    def test_usb_address_hint_survives_preference_changes(self):
        before = self.settings.read()['source_hint']['url']
        for mode in ('wifi', 'auto', 'usb'):
            self.settings.update_preferences({'backup': {'source_transport': mode}})
            self.assertEqual(before, self.settings.read()['source_hint']['url'])
        self.settings.set_enabled(True)
        with self.assertRaises(ConfigError):
            self.settings.update_preferences({'backup': {'source_transport': 'wifi'}})

    def test_connection_notification_names_actual_wireless_route(self):
        import json
        from media_importer.bot_cards import event_card
        card = event_card('connected', '', '', {'health': {'source': {'transport': 'wifi'}}})
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn('Wi-Fi', text)
        self.assertNotIn('USB', text)
