"""Exercise the packaged installer/uninstaller without camera, network or services."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from media_importer.config import DEFAULTS
from media_importer.native.releases import executable


@contextmanager
def isolated_commands():
    """Do not probe a CI host's Docker daemon from a disposable installation."""
    original = os.environ.get('PATH')
    with tempfile.TemporaryDirectory(prefix='media-importer-smoke-path-') as empty:
        commands = [empty]
        if os.name == 'nt':
            # cmd.exe is launched by Windows itself. Only PowerShell needs PATH;
            # hosted runners can place docker.exe directly in System32.
            system = Path(os.environ['SystemRoot']) / 'System32'
            commands.append(str(system / 'WindowsPowerShell/v1.0'))
        os.environ['PATH'] = os.pathsep.join(commands)
        try:
            if shutil.which('docker'):
                raise RuntimeError('Smoke command environment unexpectedly contains Docker')
            yield
        finally:
            if original is None:
                os.environ.pop('PATH', None)
            else:
                os.environ['PATH'] = original


def purge_with_windows_lock(root, launcher):
    # Exercise the packaged executable, not just generated script text. Keep
    # its handle open until the child has handed cleanup back to its wrapper.
    sys.path.insert(0, str(ROOT / 'tests'))
    from test_native_windows import purge_after_locked_attempt
    from media_importer.native.windows import purge_receipt
    record = json.loads((root / 'native/installation.json').read_text(encoding='utf-8'))
    result = purge_after_locked_attempt(launcher, executable(Path(record['bundle'])))
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    assert not purge_receipt(launcher).exists()
    print('Windows packaged purge passed with an explicitly held executable handle')


def smoke(bundle):
    with isolated_commands(), tempfile.TemporaryDirectory(prefix='media-importer-smoke-') as temporary:
        root = Path(temporary).resolve(); project = root / 'project with spaces 素材'; project.mkdir()
        for name in ('config', 'data', 'cache'):
            (project / name).mkdir()
        (project / '.env').write_text("DESTINATION_HOST='storage.example.com'\nDESTINATION_NAME='测试存储'\nDESTINATION_SHARE='media'\nDESTINATION_USERNAME='example'\nDESTINATION_PASSWORD='example-only'\nMEDIA_IMPORTER_CACHE_HOST_DIR='./cache'\n", encoding='utf-8')
        (project / 'config/settings.json').write_text(json.dumps(DEFAULTS, ensure_ascii=False), encoding='utf-8')
        (project / 'cache/完整素材.txt').write_text('complete user data', encoding='utf-8')
        (project / 'README.md').write_text('unrelated source checkout', encoding='utf-8')
        protected = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
        base = [str(executable(bundle)), '--home', str(root / 'native')]
        def run(*args):
            result = subprocess.run([*base, *args], capture_output=True, text=True, encoding='utf-8', timeout=60)
            if result.returncode:
                raise RuntimeError(result.stdout + '\n' + result.stderr)
        run('install', '--project', str(project), '--bin-dir', str(root / 'commands'), '--no-start')
        launcher = root / 'commands' / ('media-importer.cmd' if os.name == 'nt' else 'media-importer')
        base = [str(launcher)]
        run('status', '--json')
        run('update', '--bundle', str(bundle))
        run('uninstall')
        for path, content in protected.items():
            assert path.read_bytes() == content, path
        assert not (root / 'native/installation.json').exists()
        assert not launcher.exists()
        assert (root / 'native/uninstalled.json').exists()
        # Reinstall the disposable instance, then exercise the actual launcher
        # for full removal, including Windows' deferred command/program cleanup.
        base = [str(executable(bundle)), '--home', str(root / 'native')]
        run('install', '--project', str(project), '--bin-dir', str(root / 'commands'), '--no-start')
        base = [str(launcher)]
        refused = subprocess.run([*base, 'uninstall', '--purge'], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, encoding='utf-8', timeout=60)
        assert refused.returncode == 2, (refused.returncode, refused.stdout, refused.stderr)
        for path, content in protected.items():
            assert path.read_bytes() == content, path
        if os.name == 'nt':
            purge_with_windows_lock(root, launcher)
        else:
            run('uninstall', '--purge', '--yes')
        assert not (root / 'native').exists()
        assert not launcher.exists()
        for name in ('.env', 'config', 'data', 'cache'):
            assert not (project / name).exists(), name
        assert (project / 'README.md').read_text(encoding='utf-8') == 'unrelated source checkout'
    print('native install / update / retained-data uninstall / full local purge smoke passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('bundle', type=Path)
    smoke(parser.parse_args().bundle.resolve())
