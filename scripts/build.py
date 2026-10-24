#!/usr/bin/env python3
"""Build from a small source-only context without macOS external-disk metadata."""
import argparse
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def build_context(root):
    with tempfile.TemporaryDirectory(prefix='media-importer-build-') as folder:
        target = Path(folder)
        files = [root / name for name in ('Dockerfile', '.dockerignore', 'requirements.txt', 'LICENSE', 'scripts/migrate_v010.py')]
        files += [p for p in (root / 'media_importer').rglob('*.py') if not p.name.startswith('._') and '__pycache__' not in p.parts]
        for source in files:
            if source.is_symlink():
                raise ValueError('构建源文件不能是符号链接')
            destination = target / source.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        yield target


def main(argv=None):
    version = re.search(r'__version__\s*=\s*"([0-9.]+)"', (ROOT / 'media_importer/__init__.py').read_text())[1]
    parser = argparse.ArgumentParser(description='构建本机镜像；配置、凭据和缓存不会进入构建目录')
    parser.add_argument('--tag', default='local/media-importer:' + version)
    parser.add_argument('--platform', choices=('linux/arm64', 'linux/amd64'))
    parser.add_argument('--base-image')
    parser.add_argument('--activate', action='store_true', help='构建成功后让本项目 Compose 使用本机镜像，不启动服务')
    args = parser.parse_args(argv)
    command = ['docker', 'build', '-t', args.tag]
    if args.platform:
        command += ['--platform', args.platform]
    if args.base_image:
        command += ['--build-arg', 'BASE_IMAGE=' + args.base_image]
    if args.activate:
        from update import managed_override, atomic_json, SERVICES
        override = managed_override(ROOT)
    with build_context(ROOT) as folder:
        code = subprocess.run([*command, str(folder)], cwd=ROOT).returncode
    if not code and args.activate:
        atomic_json(override, {'x-media-importer-managed': 1, 'services': {name: {'image': args.tag} for name in SERVICES}})
    return code


if __name__ == '__main__':
    raise SystemExit(main())
