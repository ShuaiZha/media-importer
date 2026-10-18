"""Exercise the packaged installer/uninstaller without camera, network or services."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from media_importer.config import DEFAULTS
from media_importer.native.releases import executable


def smoke(bundle):
    with tempfile.TemporaryDirectory(prefix='media-importer-smoke-') as temporary:
        root = Path(temporary).resolve(); project = root / 'project with spaces'; project.mkdir()
        for name in ('config', 'data', 'cache'):
            (project / name).mkdir()
        (project / '.env').write_text("DESTINATION_HOST='storage.example.com'\nDESTINATION_SHARE='media'\nDESTINATION_USERNAME='example'\nDESTINATION_PASSWORD='example-only'\nMEDIA_IMPORTER_CACHE_HOST_DIR='./cache'\n")
        (project / 'config/settings.json').write_text(json.dumps(DEFAULTS))
        (project / 'cache/keep.txt').write_text('complete user data')
        protected = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
        base = [str(executable(bundle)), '--home', str(root / 'native')]
        def run(*args):
            result = subprocess.run([*base, *args], capture_output=True, text=True, encoding='utf-8', timeout=60)
            if result.returncode:
                raise RuntimeError(result.stdout + '\n' + result.stderr)
        run('install', '--project', str(project), '--bin-dir', str(root / 'commands'), '--no-start')
        run('status', '--json')
        run('update', '--bundle', str(bundle))
        run('uninstall')
        for path, content in protected.items():
            assert path.read_bytes() == content, path
        assert not (root / 'native/installation.json').exists()
        assert (root / 'native/uninstalled.json').exists()
    print('native install / same-version update / uninstall smoke passed; user data retained')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('bundle', type=Path)
    smoke(parser.parse_args().bundle.resolve())
