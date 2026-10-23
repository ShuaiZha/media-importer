"""Completion survives task retirement and delivery cannot infer or alter file work."""
import copy
import json
import unittest
from unittest.mock import patch

from media_importer.completion import read
from media_importer.outbox import freeze_completion
import test_architecture as fixtures


class CompletionBoundaryTests(unittest.TestCase):
    setUp = fixtures.ArchitectureTests.setUp
    tearDown = fixtures.ArchitectureTests.tearDown
    service = fixtures.ArchitectureTests.service

    def test_file_commit_and_result_update_roll_back_together(self):
        service = self.service()
        service.prepare_imports()
        row = next(r for r in self.store.files() if r['state'] != 'excluded')
        before = dict(self.store.db.execute('SELECT * FROM task_results').fetchone())
        with patch('media_importer.completion.refresh', side_effect=ValueError('fixture')):
            with self.assertRaises(ValueError):
                self.store.update(row['id'], state='uploaded')
        self.assertEqual(row, self.store.get(row['id']))
        self.assertEqual(before, dict(self.store.db.execute('SELECT * FROM task_results').fetchone()))

    def test_cleanup_updates_result_without_reversing_archival(self):
        service = self.service()
        self.assertTrue(service.cycle()['complete'])
        row = next(r for r in self.store.files() if r['state'] == 'uploaded')
        self.store.update(row['id'], cache_cleanup_error='fixture read-only cache')
        report = next(iter(read(self.store.db, service.config).values()))
        self.assertTrue(report['complete'])
        self.assertEqual(1, report['result']['cleanup']['cache_pending'])
        self.store.update(row['id'], cache_cleanup_error=None)
        report = next(iter(read(self.store.db, service.config).values()))
        self.assertTrue(report['complete'])
        self.assertEqual(0, report['result']['cleanup']['cache_pending'])

    def test_result_reader_does_not_query_file_details_or_remote_storage(self):
        service = self.service()
        service.cycle()
        statements = []
        self.store.db.set_trace_callback(statements.append)
        report = read(self.store.db, service.config)
        self.store.db.set_trace_callback(None)
        self.assertTrue(report)
        self.assertFalse([sql for sql in statements if ' files ' in sql.lower() or 'transfer_run_files' in sql])

    def test_retirement_removes_result_details_with_file_tasks(self):
        service = self.service()
        service.cycle()
        self.assertGreater(self.store.db.execute('SELECT count(*) FROM task_results').fetchone()[0], 0)
        self.assertGreater(service.settle(), 0)
        self.assertEqual(0, self.store.db.execute('SELECT count(*) FROM task_results').fetchone()[0])
        self.assertEqual([], self.store.files())

    def test_retirement_freezes_facts_without_importing_the_sender(self):
        service = self.service()
        service.cycle()
        run = dict(self.store.db.execute('SELECT * FROM transfer_runs').fetchone())
        values = copy.deepcopy(service.config.values)
        values['notifications']['enabled'] = True
        from media_importer.config import Config
        with self.store.db:
            freeze_completion(self.store.db, Config.from_values(values), run, self.store.files(), create=True)
        payload = json.loads(self.store.db.execute('SELECT payload FROM notification_events').fetchone()[0])
        self.assertEqual(1, payload['result']['file_count'])
        self.assertNotIn('text', payload)
        self.assertNotIn('title', payload)


if __name__ == '__main__':
    unittest.main()
