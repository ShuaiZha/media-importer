import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('manage', Path(__file__).resolve().parents[1] / 'scripts/manage.py')
manage = importlib.util.module_from_spec(spec); spec.loader.exec_module(manage)


class ManageTests(unittest.TestCase):
    def test_only_mount_values_change_and_secrets_stay_literal(self):
        original = "DESTINATION_PASSWORD='a$#b'\nMEDIA_IMPORTER_CACHE_HOST_DIR='old'\nFEISHU_APP_SECRET='unchanged'\n"
        result = manage.set_mounts(original, Path('/new project/app'), Path('/T7/cache'))
        self.assertIn("DESTINATION_PASSWORD='a$#b'", result)
        self.assertIn("FEISHU_APP_SECRET='unchanged'", result)
        self.assertIn("MEDIA_IMPORTER_CACHE_HOST_DIR='/T7/cache'", result)
        self.assertEqual(1, result.count('MEDIA_IMPORTER_CACHE_HOST_DIR='))

    def test_media_copy_preserves_required_identity_without_resource_forks(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'source.bin', Path(folder)/'target.bin'
            source.write_bytes(b'x' * 64)
            os.utime(source, ns=(1710000000000000000, 1710000000120000000))
            with patch.object(manage.shutil, 'copystat', side_effect=OSError('unsupported resource fork')):
                manage.copy_media(source, target)
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertEqual(source.stat().st_mtime_ns, target.stat().st_mtime_ns)
