"""Feishu preview settings check the host without starting streams or changing networks."""
import json
import os
import unittest
from unittest.mock import patch, Mock

from media_importer.preview_settings import normalize_url, discover, inspect, save
from media_importer.model import ConfigError
from media_importer.settings import Conflict
from media_importer import bot_settings
from test_bot_settings import SettingsFormsTests

URL = 'https://preview.example.ts.net:8443'
MAPPING = {'Web': {'preview.example.ts.net:8443': {'Handlers': {'/': {'Proxy':'http://127.0.0.1:18765'}}}},
           'TCP': {'8443': {'HTTPS': True}}}
GOOD = {'ok': True, 'checks': [{'label':'fixture','ok':True,'detail':'pass'}]}


class PreviewSettingsTests(unittest.TestCase):
    setUp = SettingsFormsTests.setUp
    tearDown = SettingsFormsTests.tearDown
    callback = SettingsFormsTests.callback
    submit = SettingsFormsTests.submit
    process = SettingsFormsTests.process

    def test_form_entry_is_available_without_starting_preview(self):
        for page in ('source_settings','preview_settings'):
            text = json.dumps(self.bot.panel(page), ensure_ascii=False)
            self.assertIn('预览设置', text)
        self.assertEqual({}, self.store.get_meta('live_preview', {}))
        form = self.bot.panel('preview_settings')
        fields = [e for e in form['elements'] if e['tag']=='form'][0]['elements']
        self.assertEqual(['public_url','save_preview_settings'], [e['name'] for e in fields])

    def test_checked_save_preserves_tasks_identity_switches_and_cache(self):
        from test_service import footage
        self.store.batch(self.original.destination_id,[footage()]); row=self.store.files()[0]
        cache=self.store.cached_path(row); cache.write_bytes(b'complete-fixture')
        before=self.settings.read(); rows=self.store.files()
        with patch('media_importer.preview_settings.inspect',return_value=GOOD):
            self.assertEqual('info',self.submit('preview_settings', {'public_url': URL})['toast']['type'])
            self.process()
        after=self.settings.read()
        self.assertEqual(URL,after['config']['preview']['public_url'])
        for key in ('backup','archive','source_options','limits'):
            self.assertEqual(before['config'][key], after['config'][key])
        self.assertEqual(rows,self.store.files());self.assertEqual(b'complete-fixture',cache.read_bytes())
        self.assertEqual({},self.store.get_meta('live_preview',{}))

    def test_failed_connection_and_stale_forms_keep_original_json(self):
        item=self.settings.read();original=self.path.read_bytes()
        with patch('media_importer.preview_settings.inspect',return_value={'ok':False,'checks':[{'ok':False,'detail':'offline'}]}):
            with self.assertRaisesRegex(ConfigError,'设置未保存'):
                save(self.settings, {'public_url': URL}, item['revision'])
        self.assertEqual(original,self.path.read_bytes())
        with patch('media_importer.preview_settings.inspect') as check:
            with self.assertRaises(Conflict): save(self.settings,{'public_url':URL},'stale')
            check.assert_not_called()

    def test_running_or_uncertain_preview_blocks_address_change(self):
        for state in ({'state':'ready'}, {'state':'queued'}, {'state':'error','cleanup_pending':True}):
            self.store.set_meta('live_preview',state)
            before=self.path.read_bytes()
            with patch('media_importer.preview_settings.inspect',return_value=GOOD):
                with self.assertRaisesRegex(ConfigError,'结束实时预览'):
                    save(self.settings,{'public_url':URL},self.settings.read()['revision'])
            self.assertEqual(before,self.path.read_bytes())

    def test_preview_start_during_network_check_blocks_save(self):
        def probe(*args):
            self.store.set_meta('live_preview',{'state':'queued'});return GOOD
        with patch('media_importer.preview_settings.inspect',side_effect=probe):
            with self.assertRaises(ConfigError): save(self.settings,{'public_url':URL},self.settings.read()['revision'])
        self.assertEqual('',self.settings.read()['config']['preview']['public_url'])

    def test_local_edit_during_network_check_wins(self):
        def probe(*args):
            self.settings.update_preferences({'notifications':{'keyword':'new'}});return GOOD
        with patch('media_importer.preview_settings.inspect',side_effect=probe):
            with self.assertRaises(Conflict): save(self.settings,{'public_url':URL},self.settings.read()['revision'])
        self.assertEqual('new',self.settings.read()['config']['notifications']['keyword'])
        self.assertEqual('',self.settings.read()['config']['preview']['public_url'])

    def test_discovery_and_check_cards_are_read_only(self):
        before=self.path.read_bytes()
        with patch('media_importer.preview_settings.discover',return_value=URL), \
                patch('media_importer.preview_settings.inspect',return_value=GOOD):
            self.assertIn(URL,json.dumps(self.bot.panel('preview_detect')))
            self.assertIn('入口检查通过',json.dumps(self.bot.panel('preview_check'),ensure_ascii=False))
        self.assertEqual(before,self.path.read_bytes())

    def test_only_owner_can_submit_and_invalid_values_are_rejected(self):
        form=next(e for e in self.bot.panel('preview_settings')['elements'] if e['tag']=='form')
        value=form['elements'][-1]['value']
        event=self.callback(value,owner='ou_other',fields={'public_url':URL})
        self.assertEqual('error',self.bot.receive_card(event)['toast']['type'])
        for invalid in ('http://host', 'https://u:p@host','https://host/path','https://host/?x=1','https://host:0','https://host\\bad'):
            with self.assertRaises(ConfigError): bot_settings.parse('preview_settings',{'public_url':invalid})
        self.assertEqual({'preview':{'public_url':''}},bot_settings.parse('preview_settings',{'public_url':''}))


class HostChecksTests(unittest.TestCase):
    def test_discover_uses_only_existing_exact_local_mapping(self):
        with patch('media_importer.preview_settings.tailnet',return_value=('preview.example.ts.net','100.64.0.1',MAPPING)):
            self.assertEqual(URL,discover(18765))
            with self.assertRaises(ConfigError):discover(12345)

    def test_local_inspection_does_not_call_tailscale_or_start_preview(self):
        response=Mock(status=200);response.read.return_value=b'media-importer-preview'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock()
        with patch('media_importer.preview_settings.build_opener') as opener, \
                patch('media_importer.preview_settings.tailnet') as network:
            opener.return_value.open.return_value=response
            self.assertTrue(inspect('',18765)['ok']);network.assert_not_called()

    def test_foreign_host_never_receives_a_request(self):
        with patch('media_importer.preview_settings.build_opener') as opener, \
                patch('media_importer.preview_settings.tailnet',return_value=('preview.example.ts.net','100.64.0.1',MAPPING)), \
                patch('media_importer.preview_settings.socket.create_connection') as connection:
            opener.return_value.open.side_effect=OSError('down')
            self.assertFalse(inspect('https://unrelated.example',18765)['ok'])
            connection.assert_not_called()

    def test_https_probe_uses_private_ip_and_verifies_domain(self):
        response=Mock(status=200);response.read.return_value=b'media-importer-preview'
        response.__enter__=Mock(return_value=response);response.__exit__=Mock()
        tcp=Mock();tcp.__enter__=Mock(return_value=tcp);tcp.__exit__=Mock()
        tls=Mock();tls.__enter__=Mock(return_value=tls);tls.__exit__=Mock()
        with patch('media_importer.preview_settings.build_opener') as opener, \
                patch('media_importer.preview_settings.tailnet',return_value=('preview.example.ts.net','100.64.0.1',MAPPING)), \
                patch('media_importer.preview_settings.socket.create_connection',return_value=tcp) as connection, \
                patch('media_importer.preview_settings.ssl.create_default_context') as context:
            opener.return_value.open.return_value=response;context.return_value.wrap_socket.return_value=tls
            self.assertTrue(inspect(URL,18765)['ok'])
            self.assertEqual(('100.64.0.1',8443),connection.call_args.args[0])
            self.assertEqual('preview.example.ts.net',context.return_value.wrap_socket.call_args.kwargs['server_hostname'])
