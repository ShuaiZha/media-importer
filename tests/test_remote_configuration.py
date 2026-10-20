"""Remote configuration must preserve work and keep secrets out of control history."""
import copy
import json
import os
import threading
import time
from unittest import TestCase
from unittest.mock import patch

import test_bot
from test_bot_settings import SettingsFormsTests
from test_service import FakeDestination, FakeSource, footage
from media_importer.archive_settings import connection_revision
from media_importer.bot import Bot
from media_importer.config import Config
from media_importer.credentials import copy_credentials, credential_path, prune, read, stage
from media_importer.deployment import destination_password
from media_importer.model import ConfigError, DestinationError
from media_importer.service import Service
from media_importer.settings import Settings


class RemoteConfigurationTests(TestCase):
    tearDown = test_bot.BotTests.tearDown
    callback = test_bot.BotTests.callback
    submit = SettingsFormsTests.submit
    process = SettingsFormsTests.process

    def setUp(self):
        test_bot.BotTests.setUp(self)
        self.remote = FakeDestination()
        self.remote.check_write = lambda: self.remote.check()
        self.factory = patch('media_importer.archive_settings.create_destination', return_value=self.remote).start()
        self.addCleanup(patch.stopall)

    def fields(self, **extra):
        target = self.settings.read()['config']['destination']
        return {**{key: str(target[key]) for key in ('host', 'port', 'share', 'directory', 'name', 'username', 'domain')},
                'mode': 'connection', 'password': '', **extra}

    def form_token(self, page='destination_settings'):
        return next(e for e in self.bot.panel(page)['elements'] if e['tag'] == 'form')['elements'][-1]['value']

    def confirm(self):
        response = self.sender.call_args.args[1]
        token = response['elements'][-1]['actions'][-1]['value']
        reply = self.bot.receive_card(self.callback(token, event_id='confirm'))
        self.assertEqual('info', reply['toast']['type'])
        self.process()

    def assert_no_secret(self, secret):
        with self.bot.db() as db:
            text = '\n'.join(str(tuple(r)) for table in ('bot_jobs', 'bot_actions', 'meta')
                             for r in db.execute('SELECT * FROM ' + table))
        for content in (text, self.path.read_text(), str(self.sender.call_args_list),
                        str(self.updater.call_args_list), json.dumps(self.bot.panel('destination_settings'))):
            self.assertNotIn(secret, content)

    def test_password_rotation_checked_then_atomic_reference_survives_restart(self):
        secret = "  new$#'password\\with spaces  "
        before = self.path.read_bytes()
        token = self.form_token()
        reply = self.bot.receive_card(self.callback(token, fields=self.fields(password=secret)))
        self.assertEqual('info', reply['toast']['type'])
        self.assertEqual(before, self.path.read_bytes())
        # A duplicate callback cannot replace the first submission's credential.
        self.bot.receive_card(self.callback(token, fields=self.fields(password='different')))
        self.process()
        checked = self.factory.call_args.args[0]
        self.assertEqual(secret, destination_password(checked.destination))
        self.assertEqual(before, self.path.read_bytes())
        self.assert_no_secret(secret)
        self.confirm()
        loaded = Config.load(self.path)
        self.assertEqual(secret, destination_password(loaded.destination))
        self.assertEqual(self.original.destination_id, loaded.destination_id)
        settings = Settings(self.path, self.store.directory)
        again = Bot(settings, self.store.directory, sender=self.sender)
        self.assertEqual(secret, destination_password(again.settings.read()['config']['destination']))
        self.assert_no_secret(secret)
        password_input = next(field for element in again.panel('destination_settings')['elements']
                              if element['tag'] == 'form' for field in element['elements'] if field.get('name') == 'password')
        self.assertEqual('', password_input['default_value'])
        self.assertEqual('password', password_input['input_type'])
        self.assertFalse(loaded.backup['enabled'])
        self.assertFalse(loaded.backup['delete_source_after_import'])

    def test_rejected_owner_and_stale_form_do_not_write_credentials(self):
        token = self.form_token()
        reply = self.bot.receive_card(self.callback(token, owner='ou_intruder', fields=self.fields(password='test-only')))
        self.assertEqual('error', reply['toast']['type'])
        self.settings.update_preferences({'limits': {'max_cache_gib': 80}})
        reply = self.bot.receive_card(self.callback(token, fields=self.fields(password='test-only')))
        self.assertEqual('error', reply['toast']['type'])
        self.assertFalse((self.root / 'credentials').exists())

    def test_auth_failure_preserves_configuration_and_never_echoes_password(self):
        from media_importer.adapters.smb import SmbDestination
        secret = 'isolated-password-value'
        self.remote.check_write = lambda: (_ for _ in ()).throw(DestinationError('authentication ' + secret))
        self.remote.redact = lambda text: SmbDestination(self.factory.call_args.args[0]).redact(text)
        before = self.path.read_bytes()
        self.submit('destination_settings', self.fields(password=secret))
        self.process()
        self.assertEqual(before, self.path.read_bytes())
        self.assertIn('未保存', str(self.sender.call_args))
        self.assert_no_secret(secret)

    def test_new_account_with_matching_archive_preserves_pending_tasks_and_old_archives(self):
        values = copy.deepcopy(self.original.values)
        values['backup']['enabled'] = True
        service = Service(Config.from_values(values), self.store, FakeSource([footage()]), self.remote, threading.Event())
        self.assertTrue(service.cycle()['complete'])
        service.settle()
        self.store.batch(service.destination_id, [footage(2)])
        rows, objects = copy.deepcopy(self.store.files()), copy.deepcopy(self.remote.objects)
        cache = self.store.cached_path(rows[0]); cache.write_bytes(b'preserve-complete-cache')
        self.submit('destination_settings', self.fields(username='new-owner', domain='NEWDOMAIN', password='new-password'))
        self.process(); self.confirm()
        loaded = Config.load(self.path)
        self.assertEqual('new-owner', loaded.destination['username'])
        self.assertEqual(service.destination_id, loaded.destination_id)
        self.assertEqual(rows, self.store.files())
        self.assertEqual(objects, self.remote.objects)
        self.assertEqual(b'preserve-complete-cache', cache.read_bytes())

    def test_blank_password_preserves_reference_on_subsequent_name_change(self):
        self.submit('destination_settings', self.fields(password='saved-password'))
        self.process(); self.confirm()
        previous = Config.load(self.path).destination['credential_file']
        self.submit('destination_settings', self.fields(name='新的显示名称'), event='name')
        self.process()
        token = self.sender.call_args.args[1]['elements'][-1]['actions'][-1]['value']
        self.bot.receive_card(self.callback(token, event_id='name-confirm')); self.process()
        loaded = Config.load(self.path)
        self.assertEqual(previous, loaded.destination['credential_file'])
        self.assertEqual('saved-password', destination_password(loaded.destination))
        self.assertEqual('新的显示名称', loaded.destination['name'])

    def test_source_and_advanced_settings_validate_preserve_records_and_reload(self):
        self.store.batch(self.original.destination_id, [footage()])
        rows = self.store.files()
        self.store.cached_path(rows[0]).write_bytes(b'untouched')
        fields = {'url': 'http://192.0.2.55:8080', 'serial': '', 'timeout_seconds': '25', 'request_attempts': '2'}
        self.submit('source_settings', fields); self.process()
        settings = Settings(self.path, self.store.directory)
        self.assertEqual(fields['url'], settings.read()['source_hint']['url'])
        self.assertEqual(25, Config.load(self.path).source['timeout_seconds'])
        before = self.path.read_bytes()
        self.submit('source_settings', {**fields, 'url': 'http://user:pass@example.test'}, event='bad-url'); self.process()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual('error', self.submit('source_settings', {**fields, 'timeout_seconds': 'nan'}, event='nan')['toast']['type'])
        self.submit('advanced_settings', {'poll_seconds': '15', 'retry_seconds': '45', 'stability_seconds': '3'}, event='advanced')
        self.process()
        self.assertEqual(45, Config.load(self.path).backup['retry_seconds'])
        self.assertEqual(rows, self.store.files())
        self.assertEqual(b'untouched', self.store.cached_path(rows[0]).read_bytes())
        self.assertFalse(Config.load(self.path).backup['enabled'])
        self.assertFalse(Config.load(self.path).backup['delete_source_after_import'])

    def test_connection_changes_require_stopped_transfers(self):
        self.settings.set_enabled(True)
        self.assertEqual('error', self.submit('destination_settings', self.fields(password='test-only'))['toast']['type'])
        before = self.path.read_bytes()
        self.submit('source_settings', {'url': '', 'serial': '', 'timeout_seconds': '15', 'request_attempts': '1'}, event='source')
        self.process()
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse((self.root / 'credentials').exists())

    def test_credentials_backup_and_retention_keep_active_password(self):
        active, expired = 'a' * 32, 'b' * 32
        stage(self.root, active, 'keep'); stage(self.root, expired, 'discard-draft')
        for key in (active, expired):
            os.utime(credential_path(self.root, key), (time.time() - 9 * 86400,) * 2)
        snapshot = self.root / 'snapshot'
        copy_credentials(self.root, snapshot)
        prune(self.root, active)
        self.assertEqual('keep', read(credential_path(self.root, active)))
        self.assertFalse(credential_path(self.root, expired).exists())
        self.assertEqual('discard-draft', read(credential_path(snapshot, expired)))
        with self.assertRaises(ConfigError):
            credential_path(self.root, '../escape')

    def test_tiny_transfer_uses_saved_configuration_and_cleans_only_own_cache(self):
        self.submit('destination_settings', self.fields(mode='new', directory='isolated-test', password='test-credential'))
        self.process(); self.confirm()
        source = FakeSource([footage()])
        self.settings.set_enabled(True)
        service = Service(Config.load(self.path), self.store, source, self.remote, threading.Event())
        self.assertTrue(service.cycle()['complete'])
        self.assertEqual(1, service.settle())
        self.assertEqual(1, len(self.remote.uploads))
        self.assertEqual([], source.deletes)
        self.assertFalse(list(self.store.cache.glob('*.bin')))
        self.assertFalse(self.store.files())
