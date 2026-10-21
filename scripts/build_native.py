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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from media_importer import __version__
from media_importer.native.releases import platform_key, executable


def build(output):
    output = output.resolve(); output.mkdir(parents=True, exist_ok=True)
    key = platform_key()
    # Use the OS temporary filesystem. macOS/exFAT sidecar removal can race
    # PyInstaller's clean operation on an external build directory.
    work = Path(tempfile.mkdtemp(prefix='media-importer-native-build-'))
    env = {**os.environ, 'PYINSTALLER_CONFIG_DIR': str(work / 'pyinstaller-cache')}
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
        '--python-option', 'X utf8',  # Frozen Python ignores PYTHONUTF8; Chinese output/paths must work on Windows.
        '--name', 'media-importer', '--distpath', str(work / 'dist'), '--workpath', str(work / 'work'),
        '--specpath', str(work), '--paths', str(ROOT), '--collect-submodules', 'media_importer',
        '--hidden-import', 'scripts.setup', '--collect-data', 'certifi',
        *(['--collect-submodules', 'bleak'] if sys.platform == 'darwin' else []),
        '--exclude-module', 'tkinter', str(ROOT / 'scripts/native_entry.py')], cwd=ROOT, env=env, check=True)
    bundle = work / 'dist/media-importer'
    info = {'format': 1, 'version': __version__, 'platform': key, 'config_version': 4, 'database_version': 5}
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
    shutil.rmtree(work)
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/native')
    args = parser.parse_args()
    build(args.output)
