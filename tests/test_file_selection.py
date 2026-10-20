"""File-level choices and whole-recording cleanup use disposable tiny fixtures."""
import copy
import json
import threading
import unittest
from types import SimpleNamespace

from media_importer import camera_operations, selected_cards, selected_imports
from media_importer.adapters import source_selection_groups
from media_importer.config import Config
from media_importer.selected_transfer import SelectedService
from media_importer.service import Service
import test_camera_operations
from test_selected_cards import Bot, text
from test_service import FakeSource, configuration, footage, replace


def chapters():
    return [footage(), footage(primary=False),
            replace(footage(2), path='100GOPRO/GH020001.MP4'),
            replace(footage(2, primary=False), path='100GOPRO/GL020001.LRV')]


class FileSelectionTests(unittest.TestCase):
    setUp = test_camera_operations.CameraOperationTests.setUp
    tearDown = test_camera_operations.CameraOperationTests.tearDown
    request = test_camera_operations.CameraOperationTests.request

    def recording(self):
        self.settings.update_preferences({'backup': {'archive_previews': False}})
        self.config = Config.from_values(self.settings.read()['config'])
        self.before = self.path.read_bytes()
        self.assets = chapters()
        self.source = FakeSource(self.assets)
        self.service = Service(self.config, self.store, self.source, self.remote, threading.Event())

    def manifest(self, indices=(0,), previews=False):
        groups = source_selection_groups(self.source, list(self.source.assets.values()))
        return {'id': 'file-choice-' + '-'.join(map(str, indices)), 'selection_mode': 'files',
                'archive_previews': previews, 'groups': [
                    {**groups[i], 'members': [a.to_dict() for a in groups[i]['members']]} for i in indices]}

    def transfer(self, manifest=None):
        return SelectedService(configuration(enabled=False, delete_source_after_import=True),
            self.store, self.source, self.remote, threading.Event(),
            selection=manifest or self.manifest())

    def test_each_mp4_is_one_choice_with_only_its_own_preview(self):
        self.recording()
        choices = source_selection_groups(self.source, self.assets)
        self.assertEqual(2, len(choices))
        self.assertEqual([self.assets[0], self.assets[1]], choices[0]['members'])
        self.assertEqual([self.assets[2], self.assets[3]], choices[1]['members'])
        self.assertEqual([self.assets[1].identity], choices[0]['preview_ids'])
        self.assertEqual([self.assets[3].identity], choices[1]['preview_ids'])
        self.assertTrue(all('recording_hint' not in g for g in choices))
        # Deletion retains all four members and reverse chapter order.
        cleanup = self.source.groups(self.assets)
        self.assertEqual(1, len(cleanup))
        self.assertEqual(4, len(cleanup[0]['members']))
        self.assertEqual(self.assets[2], cleanup[0]['members'][0])

    def test_generic_fallback_does_not_reuse_deletion_groups(self):
        self.recording()
        source = SimpleNamespace(groups=lambda _: self.fail('Deletion groups used as choices'))
        choices = source_selection_groups(source, self.assets)
        self.assertEqual([[asset] for asset in self.assets], [g['members'] for g in choices])
        self.assertTrue(all(not g['preview_ids'] for g in choices))

    def test_one_chapter_upload_keeps_other_chapter_and_all_originals(self):
        self.recording()
        service = self.transfer(self.manifest((1,)))
        result = service.cycle()
        self.assertTrue(result['complete'])
        self.assertEqual((1, 32, 1), (result['files'], result['bytes'], result['excluded_preview_files']))
        self.assertEqual([self.assets[2].path], self.source.downloads)
        self.assertEqual(1, len(self.remote.uploads))
        self.assertEqual(4, len(self.source.assets))
        self.assertFalse(self.source.deletes)
        self.assertFalse(self.store.groups(service.destination_id))
        self.assertEqual(2, service.settle())
        self.assertEqual({self.assets[2].identity, self.assets[3].identity},
                         {r['asset_id'] for r in self.store.db.execute('SELECT * FROM source_index')})
        self.assertTrue(self.transfer(service.selection).cycle()['complete'])
        self.assertEqual(1, len(self.remote.uploads))  # Still deduped after retiring details.

    def test_preview_switch_includes_only_selected_chapters_companions(self):
        self.recording()
        result = self.transfer(self.manifest((1,), previews=True)).cycle()
        self.assertTrue(result['complete'])
        self.assertEqual((2, 44), (result['files'], result['bytes']))
        self.assertEqual({a.path for a in self.assets[2:]}, set(self.source.downloads))
        self.assertFalse(self.source.deletes)

    def test_select_both_chapters_has_no_duplicate_members(self):
        self.recording()
        result = self.transfer(self.manifest((0, 1))).cycle()
        self.assertTrue(result['complete'])
        self.assertEqual((2, 64, 2), (result['files'], result['bytes'], result['excluded_preview_files']))
        self.assertEqual({self.assets[0].path, self.assets[2].path}, set(self.source.downloads))
        self.assertEqual(2, len(self.source.downloads))
        self.assertFalse(self.source.deletes)

    def test_changed_unselected_chapter_does_not_block_selected_file(self):
        self.recording()
        manifest = self.manifest()
        other = self.assets[2]
        self.source.assets[other.path] = replace(other, gumi='new-recording-identity')
        result = self.transfer(manifest).cycle()
        self.assertTrue(result['complete'])
        self.assertEqual([self.assets[0].path], self.source.downloads)
        self.assertFalse(self.source.deletes)

    def test_changed_selected_file_or_companion_does_not_expand_selection(self):
        self.recording()
        manifest = self.manifest()
        preview = self.assets[1]
        self.source.assets[preview.path] = replace(preview, size=13)
        result = self.transfer(manifest).cycle()
        self.assertFalse(result['complete'])
        self.assertFalse(self.source.downloads or self.source.deletes)
        self.assertNotIn(self.source.assets[preview.path].identity,
                         {row['asset_id'] for row in self.store.files()})

    def test_existing_sibling_tasks_cache_and_deletion_groups_are_unchanged(self):
        self.recording()
        prior = Service(configuration(), self.store, self.source, self.remote, threading.Event())
        prior.prepare_imports()
        sibling = next(r for r in prior.rows() if r['asset_id'] == self.assets[2].identity)
        prior.import_asset(sibling)
        cache = self.store.cached_path(sibling).read_bytes()
        sibling_ids = {a.identity for a in self.assets[2:]}
        rows = {r['id']: r for r in self.store.files() if r['asset_id'] in sibling_ids}
        groups = copy.deepcopy(self.store.groups(prior.destination_id))
        service = self.transfer()
        self.assertTrue(service.cycle()['complete'])
        service.settle()
        self.assertEqual(rows, {key: self.store.get(key) for key in rows})
        self.assertEqual(cache, self.store.cached_path(sibling).read_bytes())
        self.assertEqual(groups, self.store.groups(prior.destination_id))
        self.assertFalse(self.source.deletes)

    def test_cleanup_requires_both_chapters_even_after_individual_receipts_retired(self):
        self.recording()
        first = self.transfer()
        self.assertTrue(first.cycle()['complete'])
        first.settle()
        partial = self.request()
        result = json.loads(partial['result'])
        self.assertEqual(0, result['eligible_files'])
        self.assertEqual(4, sum(item['files'] for item in result['retained']))
        second = self.transfer(self.manifest((1,)))
        self.assertTrue(second.cycle()['complete'])
        second.settle()
        # No real or fake deletion is needed to verify the eligibility boundary.
        with self.store.db:
            camera_operations.cancel(self.store.db, partial['id'], self.owner)
        complete = self.request()
        self.assertEqual(4, json.loads(complete['result'])['eligible_files'])
        self.assertFalse(self.source.deletes)

    def test_partial_offline_cache_is_not_enough_for_recording_cleanup(self):
        self.recording()
        self.remote.online = False
        selected = self.transfer()
        self.assertFalse(selected.cycle()['complete'])
        result = json.loads(self.request(mode='imported')['result'])
        self.assertEqual(0, result['eligible_files'])
        self.assertTrue(selected.cache_valid(next(r for r in selected.rows() if r['state'] == 'cached')))
        self.assertFalse(self.source.deletes)

    def test_scan_preview_and_card_scope_is_one_file_without_duplicate_listing(self):
        self.recording()
        op = selected_imports.enqueue(self.store, self.settings, 'browse-files', self.owner)
        self.assertTrue(selected_imports.process_one(self.service, self.settings))
        op = selected_imports.get(self.store.db, op['id'])
        plan = json.loads(op['plan'])
        self.assertEqual('files', plan['selection_mode'])
        # Freeze the preview policy to the same default used for file selection.
        self.assertFalse(plan['archive_previews'])
        card = selected_cards.render(Bot(), op)
        field = next(e for e in card['elements'] if e['tag'] == 'form')['elements'][0]
        labels = [v['text']['content'] for v in field['options']]
        self.assertEqual(2, len(labels))
        self.assertEqual('GH010001.MP4 · 32.0 B', labels[0])
        self.assertEqual('GH020001.MP4 · 32.0 B', labels[1])
        self.assertNotIn('2 段视频', text(card))
        self.assertNotIn('同次录像', text(card))
        self.assertNotIn('.MP4', text([e for e in card['elements'] if e['tag'] != 'form']))
        selected_imports.choose(self.store, self.settings, op['id'], self.owner, [1], 0, 0)
        ready = selected_imports.review(self.store, self.settings, op['id'], self.owner)
        result = json.loads(ready['result'])
        self.assertEqual((1, 32, 1), (result['total_files'], result['total_bytes'],
                                     result['excluded_preview_files']))
        preview = text(selected_cards.render(Bot(), ready))
        self.assertIn('GH020001.MP4', preview)
        self.assertNotIn('GH010001.MP4', preview)
        self.assertNotIn('.LRV', preview)
        self.assertFalse(self.source.downloads or self.source.deletes or self.store.files())
        self.assertEqual(self.before, self.path.read_bytes())

    def test_legacy_confirmed_group_manifest_still_imports_both_chapters(self):
        self.recording()
        groups = self.source.groups(self.assets)
        legacy = {'id': 'legacy-pending', 'archive_previews': False,
                  'groups': [{**g, 'members': [a.to_dict() for a in g['members']]} for g in groups]}
        frozen = copy.deepcopy(legacy)
        result = self.transfer(legacy).cycle()
        self.assertTrue(result['complete'])
        self.assertEqual(2, result['files'])
        self.assertEqual(frozen, legacy)
        self.assertEqual({self.assets[0].path, self.assets[2].path}, set(self.source.downloads))
        self.assertFalse(self.source.deletes)
