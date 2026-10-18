"""Merge architecture manifests only after their native smoke tests pass."""
import argparse
import hashlib
import json
from pathlib import Path
import re

PLATFORMS = {'macos-arm64', 'macos-amd64', 'linux-arm64', 'linux-amd64', 'windows-amd64'}


def merge(folder):
    result = {'format': 1, 'version': None, 'assets': {}}
    for path in sorted(folder.glob('native-*.json')):
        if path.name == 'native-release.json':
            continue
        value = json.loads(path.read_text())
        if value.get('format') != 1 or not re.fullmatch(r'\d+\.\d+\.\d+', value.get('version', '')):
            raise ValueError('Invalid native manifest')
        if result['version'] not in (None, value['version']):
            raise ValueError('Native versions disagree')
        result['version'] = value['version']
        for platform, asset in value['assets'].items():
            if platform in result['assets'] or asset['name'] != f"media-importer-{value['version']}-{platform}.tar.gz":
                raise ValueError('Duplicate or invalid native platform')
            with (folder / asset['name']).open('rb') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != asset['sha256']:
                    raise ValueError('Native artifact checksum mismatch')
            result['assets'][platform] = asset
    if set(result['assets']) != PLATFORMS:
        raise ValueError('Native release must contain all five supported platforms')
    (folder / 'native-release.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('folder', type=Path)
    merge(parser.parse_args().folder)
