"""Archive publication and cleanup remain independent facts."""
import unittest

from media_importer.task_facts import file_facts, archive_complete


class TaskFactsTests(unittest.TestCase):
    def test_uploaded_bytes_do_not_publish_a_file(self):
        row = {'state': 'publishing', 'current_bytes': 32, 'size': 32}
        facts = file_facts(row, cached=True)
        self.assertTrue(facts['imported'])
        self.assertFalse(facts['archived'])
        self.assertFalse(archive_complete([row]))

    def test_archive_verification_is_required_when_reusing_a_record(self):
        facts = file_facts({'state': 'uploaded'}, archive_verified=False)
        self.assertTrue(facts['archive_pending'])
        self.assertFalse(facts['imported'])
        self.assertTrue(file_facts({'state': 'uploaded'}, cached=True, archive_verified=False)['imported'])

    def test_failed_cleanup_never_reverses_archival(self):
        row = {'state': 'uploaded', 'cache_cleared_at': 'done', 'cache_cleanup_error': 'read-only', 'cleanup': 'deleting'}
        facts = file_facts(row)
        self.assertTrue(facts['archived'])
        self.assertTrue(facts['source_uncertain'])
        self.assertFalse(facts['cache_cleared'])
        self.assertFalse(facts['source_deleted'])
        self.assertTrue(archive_complete([row]))

    def test_excluded_previews_never_inflate_completion(self):
        row = {'state': 'excluded'}
        self.assertFalse(file_facts(row, cached=True)['imported'])
        self.assertFalse(archive_complete([row]))
        self.assertTrue(archive_complete([row, {'state': 'uploaded'}]))


if __name__ == '__main__':
    unittest.main()
