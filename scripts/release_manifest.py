"""Create the small, non-secret contract consumed by the stable updater."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def manifest(root, tag, digest):
    version = re.search(r'__version__ = "([0-9.]+)"', (root / 'media_importer/__init__.py').read_text())[1]
    if tag != 'v' + version or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Release tag must match the application version')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Invalid image digest')
    return {'format': 1, 'version': version, 'image': 'ghcr.io/shuaizha/media-importer@' + digest,
            'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
            'updater_version': 1, 'config_version': 4, 'settings_version': 5, 'database_version': 5}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--output', type=Path, default=Path('release.json'))
    args = parser.parse_args()
    args.output.write_text(json.dumps(manifest(Path(__file__).resolve().parents[1], args.tag, args.digest), indent=2) + '\n')
