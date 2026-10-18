import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from media_importer.native import releases
from scripts.native_manifest import merge, PLATFORMS


class NativeReleaseTests(unittest.TestCase):
    def package(self):
        info = {'format': 1, 'version': '0.10.20', 'platform': releases.platform_key(),
                'config_version': 4, 'database_version': 5}
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            for name, content in {'bundle.json': json.dumps(info).encode(),
                                  releases.executable(Path('.')).name: b'program fixture'}.items():
                item = tarfile.TarInfo('media-importer/' + name)
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
        payload = data.getvalue()
        name = f'media-importer-0.10.20-{releases.platform_key()}.tar.gz'
        manifest = {'format': 1, 'version': '0.10.20', 'assets': {releases.platform_key():
                    {'name': name, 'sha256': hashlib.sha256(payload).hexdigest()}}}
        return manifest, payload

    def test_latest_and_pinned_release_use_versioned_assets_and_verify_package(self):
        manifest, payload = self.package()
        for selected in (None, '0.10.20'):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as temporary:
                with patch.object(releases, 'request', side_effect=[io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(payload)]) as request:
                    bundle = releases.download_release(Path(temporary), selected)
                self.assertEqual('0.10.20', releases.metadata(bundle)['version'])
                prefix = '/download/v0.10.20' if selected else '/latest/download'
                self.assertEqual(releases.BASE + prefix + '/native-release.json', request.call_args_list[0].args[0])
                self.assertEqual(releases.BASE + '/download/v0.10.20/' + manifest['assets'][releases.platform_key()]['name'], request.call_args_list[1].args[0])

    def test_corrupt_download_is_not_extracted(self):
        manifest, payload = self.package()
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(releases, 'request', side_effect=[io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(payload + b'corrupt')]):
                with self.assertRaisesRegex(ValueError, '校验失败'):
                    releases.download_release(Path(temporary))
            self.assertFalse((Path(temporary) / 'unpacked').exists())

    def test_missing_native_release_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(releases, 'request', side_effect=HTTPError('https://example.com', 404, 'missing', {}, None)):
                with self.assertRaisesRegex(ValueError, 'update --bundle'):
                    releases.download_release(Path(temporary))
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_manifest_requires_all_platforms_and_matching_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for platform in sorted(PLATFORMS):
                name = f'media-importer-0.10.20-{platform}.tar.gz'
                content = platform.encode()
                (folder / name).write_bytes(content)
                (folder / ('native-' + platform + '.json')).write_text(json.dumps({
                    'format': 1, 'version': '0.10.20', 'assets': {platform: {
                        'name': name, 'sha256': hashlib.sha256(content).hexdigest()}}}))
            merge(folder)
            self.assertEqual(PLATFORMS, set(json.loads((folder / 'native-release.json').read_text())['assets']))
            (folder / 'native-windows-amd64.json').unlink()
            with self.assertRaisesRegex(ValueError, 'all five'):
                merge(folder)
