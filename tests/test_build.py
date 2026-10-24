import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('image_build', Path(__file__).parents[1] / 'scripts/build.py')
image_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_build)


class BuildContextTests(unittest.TestCase):
    def test_context_contains_only_sources_without_credentials_cache_or_sidecars(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('Dockerfile', '.dockerignore', 'requirements.txt', 'LICENSE', 'scripts/migrate_v010.py',
                         'media_importer/__init__.py', 'media_importer/._hidden.py', '.env', 'data/state.sqlite3',
                         'cache/video.bin', 'config/settings.json', '._.dockerignore'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            with image_build.build_context(root) as context:
                paths = {p.relative_to(context).as_posix() for p in context.rglob('*') if p.is_file()}
                self.assertEqual({'Dockerfile', '.dockerignore', 'requirements.txt', 'LICENSE', 'scripts/migrate_v010.py',
                                  'media_importer/__init__.py'}, paths)
            self.assertFalse(context.exists())
