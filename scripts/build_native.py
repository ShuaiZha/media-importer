"""Build a self-contained console bundle on the target OS/architecture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import shutil
import uuid
import ssl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from media_importer import __version__
from media_importer.native.releases import platform_key, executable

MACOS_IDENTIFIER = 'org.media-importer.cli'


def sign_macos(bundle, identity=None):
    """Stable identifier, without weakening the signer's designated requirement.

    Ad-hoc builds still have a per-build identity and may request privacy access
    again. Only a real Apple signing identity can support continuity across builds.
    """
    if sys.platform != 'darwin':
        if identity:
            raise ValueError('签名身份选项仅支持 macOS 构建')
        return
    command = ['/usr/bin/codesign', '--force', '--sign', identity or '-',
               '--identifier', MACOS_IDENTIFIER]
    if identity:
        command += ['--timestamp', '--options', 'runtime']
    subprocess.run([*command, str(executable(bundle))], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(executable(bundle))], check=True)


def runtime_data():
    # Some managed Python distributions need an adjacent CA rejection store.
    # Preserve that distribution's trust policy in frozen bundles as well.
    return sorted(Path(ssl.__file__).parent.glob('*-rejected-cas.pem'))


def build(output, codesign_identity=None):
    if codesign_identity and sys.platform != 'darwin':
        raise ValueError('签名身份选项仅支持 macOS 构建')
    # Failed builds must not accumulate temporary program copies either.
    with tempfile.TemporaryDirectory(prefix='media-importer-native-build-') as temporary:
        return build_in(Path(temporary), output, codesign_identity)


def build_in(work, output, codesign_identity):
    output = output.resolve(); output.mkdir(parents=True, exist_ok=True)
    key = platform_key()
    # Use the OS temporary filesystem. macOS/exFAT sidecar removal can race
    # PyInstaller's clean operation on an external build directory.
    env = {**os.environ, 'PYINSTALLER_CONFIG_DIR': str(work / 'pyinstaller-cache')}
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
        '--python-option', 'X utf8',  # Frozen Python ignores PYTHONUTF8; Chinese output/paths must work on Windows.
        '--name', 'media-importer', '--distpath', str(work / 'dist'), '--workpath', str(work / 'work'),
        '--specpath', str(work), '--paths', str(ROOT), '--collect-submodules', 'media_importer',
        '--hidden-import', 'scripts.setup', '--collect-data', 'certifi',
        *(['--codesign-identity', codesign_identity] if codesign_identity else []),
        *(arg for path in runtime_data() for arg in ('--add-data', str(path) + os.pathsep + '.')),
        *(['--collect-submodules', 'bleak'] if sys.platform == 'darwin' else []),
        '--exclude-module', 'tkinter', str(ROOT / 'scripts/native_entry.py')], cwd=ROOT, env=env, check=True)
    bundle = work / 'dist/media-importer'
    shutil.copyfile(ROOT / 'LICENSE', bundle / 'LICENSE')
    from scripts.preview_bundle import bundle as bundle_preview
    components = bundle_preview(bundle, key)
    sign_macos(bundle, codesign_identity)
    info = {'format': 1, 'version': __version__, 'platform': key, 'config_version': 4, 'settings_version': 5, 'database_version': 5}
    if components: info['components'] = components
    (bundle / 'bundle.json').write_text(json.dumps(info, indent=2) + '\n')
    actual = subprocess.check_output([str(executable(bundle)), '--version'], text=True).strip()
    if actual != __version__:
        raise RuntimeError('打包版本不一致')
    destination = output / key / 'media-importer'
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or not (destination / 'bundle.json').is_file():
            raise ValueError('输出目录已有非安装包文件，未覆盖')
        previous = destination.with_name('.previous-' + uuid.uuid4().hex)
        destination.rename(previous)
        shutil.rmtree(previous, ignore_errors=True)  # Only this build's obsolete bundle; exFAT sidecars can vanish concurrently.
    shutil.copytree(bundle, destination, symlinks=True)
    archive = output / f'media-importer-{__version__}-{key}.tar.gz'
    def include(item):
        return None if any(part.startswith('._') or part == '.DS_Store' for part in Path(item.name).parts) else item
    with tarfile.open(archive, 'w:gz') as handle:
        handle.add(bundle, arcname='media-importer', filter=include)
    with archive.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    asset = {'name': archive.name, 'sha256': digest}
    (output / ('native-' + key + '.json')).write_text(json.dumps({'format': 1, 'version': __version__, 'assets': {key: asset}}, indent=2) + '\n')
    print(str(archive))
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/native')
    parser.add_argument('--codesign-identity', help='macOS 钥匙串中的 Apple 签名身份；默认仍为临时签名')
    args = parser.parse_args()
    build(args.output, args.codesign_identity)
