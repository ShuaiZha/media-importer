#!/usr/bin/env python3
"""Deployment maintenance using Python stdlib, Git and Docker Compose."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(args, cwd=ROOT, capture=False):
    result = subprocess.run(args, cwd=str(cwd), text=True, capture_output=True)
    if result.returncode:
        # Docker config and service output may contain expanded credentials.
        safe = next((line for line in result.stdout.splitlines() if line.startswith('备份迁移未完成：')), None)
        if safe:
            raise RuntimeError(safe)
        raise RuntimeError('Docker 操作失败（退出码 %s）；请检查 Docker、挂载权限与可用空间。' % result.returncode)
    if capture:
        return result.stdout
    if result.stdout:
        print(result.stdout.strip(), flush=True)


def within(path, parent):
    return path == parent or parent in path.parents


def project_files(root, target):
    target.mkdir(mode=0o700)
    for name in ('media_importer', 'scripts'):
        for source in (root / name).rglob('*'):
            relative = source.relative_to(root)
            if '__pycache__' in relative.parts or source.suffix == '.pyc' or source.name.startswith('._'):
                continue
            if source.is_symlink():
                raise RuntimeError('程序备份不支持符号链接')
            if source.is_file():
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                copy_media(source, destination)
    for name in ('Dockerfile', '.dockerignore', 'compose.yaml', 'requirements.txt', 'README.md', '.env.example'):
        copy_media(root / name, target / name)
    for name in ('.gitignore', 'compose.override.yaml'):
        if (root / name).is_file():
            copy_media(root / name, target / name)
    (target / 'config').mkdir()
    copy_media(root / 'config/settings.example.json', target / 'config/settings.example.json')


def helper(root, bundle, action):
    return command(['docker', 'compose', 'run', '--rm', '--no-deps', '-T',
                    '-v', str(root) + ':/app:ro', '-v', str(bundle) + ':/bundle',
                    '--entrypoint', 'python3', 'backup', '-m', 'media_importer.backup', action], cwd=root)


def backup(root, output):
    spec = json.loads(command(['docker', 'compose', 'config', '--format', 'json'], cwd=root, capture=True))
    paths = [Path(v['source']).resolve() for v in spec['services']['backup']['volumes'] if v.get('type') == 'bind']
    output = output.expanduser().resolve()
    if output.exists() or any(within(output, path) for path in paths if path.is_dir()):
        raise RuntimeError('备份目标必须是新的目录，且不能放在正在使用的配置、数据或缓存目录内。')
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + '.incomplete-' + uuid.uuid4().hex[:8])
    partial.mkdir(mode=0o700)
    running = command(['docker', 'compose', 'ps', '--services', '--status', 'running'], cwd=root, capture=True).splitlines()
    try:
        print('暂停两个服务，保存一致的任务记录和完整缓存…', flush=True)
        command(['docker', 'compose', 'stop', 'backup', 'bot'], cwd=root)
        helper(root, partial, 'export')
        project_files(root, partial / 'project')
        helper(root, partial, 'seal')
        partial.rename(output)
        print('备份完成：' + str(output), flush=True)
        print('包含连接凭据，请私下保管。完整缓存已复制；原缓存保持不变。', flush=True)
    finally:
        if running:
            command(['docker', 'compose', 'up', '-d', '--no-build', *running], cwd=root)
        if partial.exists():
            print('未完成的备份保留在：' + str(partial) + '（不能用于迁移）', flush=True)


def set_mounts(text, project, cache):
    for key, value in {'MEDIA_IMPORTER_CONFIG_HOST_DIR': project / 'config',
                       'MEDIA_IMPORTER_DATA_HOST_DIR': project / 'data',
                       'MEDIA_IMPORTER_CACHE_HOST_DIR': cache}.items():
        # Both dotenv and Compose honor single-quoted literal values.
        literal = value.as_posix().replace('\\', '\\\\').replace("'", "\\'")
        line = key + "='" + literal + "'"
        pattern = r'^' + re.escape(key) + r'=.*$'
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, lambda _: line, text, flags=re.MULTILINE)
        else:
            text += '\n' + line + '\n'
    return text


def copy_media(source, target):
    # Preserve the evidence the importer actually uses, without copying macOS
    # xattrs/resource forks that exFAT and Windows may reject with EINVAL.
    info = source.stat()
    shutil.copyfile(source, target)
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))


def restore(bundle, project, cache):
    bundle, project, cache = (p.expanduser().resolve() for p in (bundle, project, cache))
    # A completed bundle must have been sealed; incomplete directories never restore.
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1:
        raise RuntimeError('不支持的备份格式')
    if project.exists() or cache.exists():
        raise RuntimeError('恢复只写入新目录；项目目录和缓存目录都不能已存在。')
    if within(project, bundle) or within(cache, bundle) or within(project, cache) or within(cache, project):
        raise RuntimeError('项目、缓存、备份目录必须互相独立。')
    image = 'local/media-importer:' + manifest['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', manifest['version']):
        raise RuntimeError('备份中的程序版本无效')
    # Build source is bundled. Never start the old project or share its Compose name.
    print('准备对应版本的本机镜像并核验备份…', flush=True)
    if subprocess.run(['docker', 'image', 'inspect', image], capture_output=True).returncode:
        command(['docker', 'build', '-t', image, str(bundle / 'project')], cwd=bundle)
    base = ['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python3',
            '-v', str(bundle / 'project') + ':/app:ro', '-v', str(bundle) + ':/bundle:ro']
    command([*base, image, '-m', 'media_importer.backup', 'verify'], cwd=bundle)
    project.parent.mkdir(parents=True, exist_ok=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    project.mkdir(mode=0o700)
    cache.mkdir(mode=0o700)
    for expected in manifest['entries']:
        for prefix, destination in (('project', project), ('cache', cache)):
            if expected['path'].startswith(prefix + '/'):
                relative = Path(expected['path']).relative_to(prefix)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_media(bundle / expected['path'], target)
    (project / 'data').mkdir()
    copy_media(bundle / 'state.sqlite3', project / 'data/state.sqlite3')
    settings = json.loads((bundle / 'settings.json').read_text(encoding='utf-8'))
    settings['backup'].update(enabled=False, delete_source_after_import=False)
    (project / 'config/settings.json').write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(project / 'config/settings.json', 0o600)
    env = set_mounts((bundle / 'connection.env').read_text(encoding='utf-8'), project, cache)
    (project / '.env').write_text(env, encoding='utf-8')
    os.chmod(project / '.env', 0o600)
    # Offline restores use the image verified/built from the bundle, regardless
    # of the registry tag in the bundled Compose file.
    (project / 'compose.override.yaml').write_text(json.dumps({'x-media-importer-managed': 1,
        'services': {name: {'image': image} for name in ('backup', 'bot')}}, indent=2) + '\n', encoding='utf-8')
    # The restored DB is only opened by Linux SQLite, never the host's SQLite.
    command([*base, '-e', 'MEDIA_IMPORTER_ENV_FILE=/run/media-importer.env',
             '-e', 'MEDIA_IMPORTER_REQUIRE_CACHE_MOUNT=1',
             '-v', str(project / '.env') + ':/run/media-importer.env:ro',
             '-v', str(project / 'config') + ':/config', '-v', str(project / 'data') + ':/data',
             '-v', str(cache) + ':/data/cache', image, '-m', 'media_importer.backup', 'restore'], cwd=project)
    print('恢复完成：' + str(project), flush=True)
    print('同步与删除原片已关闭。尚未启动服务；停止旧电脑的两个服务后，在新项目目录执行 docker compose up -d。', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='素材导入助手：稳定版更新、完整备份与迁移')
    commands = parser.add_subparsers(dest='action', required=True)
    upgrade = commands.add_parser('update', help='更新到 GitHub 最新稳定版；保留配置、任务和完整缓存')
    upgrade.add_argument('--version', help='指定稳定版本，例如 0.10.18；默认最新正式发行版')
    save = commands.add_parser('backup', help='暂停服务，备份配置、任务、完整缓存，然后恢复原服务运行状态')
    save.add_argument('--output', type=Path, required=True, help='新备份目录，建议放在外接盘或另一块磁盘')
    load = commands.add_parser('restore', help='恢复到新的项目与缓存目录，保持两个开关关闭')
    load.add_argument('--from', dest='bundle', type=Path, required=True)
    load.add_argument('--project', type=Path, required=True)
    load.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'update':
            from update import update
            update(ROOT, args.version)
        elif args.action == 'backup':
            backup(ROOT, args.output)
        else:
            restore(args.bundle, args.project, args.cache)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print('操作未完成：' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
