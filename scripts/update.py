"""Stable-release updates: stdlib + Git + Compose, never copy or delete media."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

REPOSITORY = 'ShuaiZha/media-importer'
REGISTRY = 'ghcr.io/shuaizha/media-importer'
UPDATER_VERSION = 1
SERVICES = ('backup', 'bot')


def run(args, root, *, capture=False):
    try:
        result = subprocess.run(args, cwd=root, text=True, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('命令不可用或执行超时，请检查 Git、Docker 与网络') from None
    if result.returncode:
        # Expanded Compose configuration and subprocess errors may contain secrets.
        raise RuntimeError(f'{args[0]} {args[1]} 操作失败；请检查网络、权限或服务状态')
    return result.stdout if capture else None


def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'media-importer-updater', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(256 * 1024 + 1)
        if len(body) > 256 * 1024:
            raise ValueError('响应过大')
        return json.loads(body)
    except (OSError, ValueError):
        raise RuntimeError('无法读取 GitHub 稳定版本信息；当前安装未更改') from None


def release(version=None):
    if version and not re.fullmatch(r'v?\d+\.\d+\.\d+', version):
        raise ValueError('版本格式应为 0.10.17 或 v0.10.17，不接受预发布版本')
    endpoint = 'tags/v' + version.removeprefix('v') if version else 'latest'
    value = fetch_json(f'https://api.github.com/repos/{REPOSITORY}/releases/{endpoint}')
    tag = value.get('tag_name', '')
    if value.get('draft') or value.get('prerelease') or not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('所选版本不是正式稳定版')
    expected = f'https://github.com/{REPOSITORY}/releases/download/{tag}/release.json'
    assets = [a for a in value.get('assets', []) if a.get('name') == 'release.json']
    if len(assets) != 1 or assets[0].get('browser_download_url') != expected:
        raise ValueError('稳定版缺少受支持的发布清单')
    manifest = fetch_json(expected)
    if (manifest.get('format') != 1 or manifest.get('version') != tag[1:] or
            manifest.get('updater_version') != UPDATER_VERSION or manifest.get('config_version') != 4 or
            manifest.get('database_version') != 5 or
            not re.fullmatch(re.escape(REGISTRY) + r'@sha256:[0-9a-f]{64}', manifest.get('image', '')) or
            not re.fullmatch(r'[0-9a-f]{40}', manifest.get('commit', ''))):
        raise ValueError('版本需要新的更新工具或数据迁移，请按发行说明操作；未更改当前安装')
    return manifest


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def update_lock(root):
    path = root / '.update.lock'
    if path.is_symlink():
        raise RuntimeError('更新锁不能是符号链接')
    with path.open('a+b') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('已有更新命令正在运行') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def deployment(root):
    spec = json.loads(run(['docker', 'compose', 'config', '--format', 'json'], root, capture=True))
    if set(spec['services']) != set(SERVICES):
        raise ValueError('统一更新只处理本项目的 backup、bot 两个服务')
    paths = {}
    for name in SERVICES:
        mounts = {v['target']: v for v in spec['services'][name]['volumes'] if v['type'] == 'bind'}
        for target in ('/config', '/data', '/data/cache', '/run/media-importer.env'):
            item = mounts.get(target)
            if not item or not Path(item['source']).exists():
                raise ValueError('配置、数据或缓存挂载缺失；请连接原磁盘，未创建替代目录')
            source = Path(item['source'])
            if source.is_symlink() or target in paths and paths[target] != source:
                raise ValueError('两个服务必须使用同一组真实挂载路径')
            paths[target] = source
    return paths


def managed_override(root):
    path = root / 'compose.override.yaml'
    if path.is_symlink():
        raise ValueError('Compose 覆盖文件不能是符号链接')
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            raise ValueError('存在自定义 compose.override.yaml，请先合并部署配置；不会覆盖') from None
        if (set(value) != {'x-media-importer-managed', 'services'} or value['x-media-importer-managed'] != 1 or
                set(value['services']) != set(SERVICES) or
                any(set(v) != {'image'} for v in value['services'].values())):
            raise ValueError('存在自定义 Compose 覆盖配置，未修改')
    return path


SNAPSHOT = r'''
import json, shutil, sqlite3
from pathlib import Path
source=Path('/input/state.sqlite3')
result={'database_present':source.is_file(),'database_version':0}
if source.is_file():
    # Both services are stopped. Copy the database and any uncheckpointed WAL
    # together; SQLite may need to rebuild SHM even for a read-only connection.
    # It may do so in /tmp, never on the original data volume.
    staging=Path('/tmp/snapshot');staging.mkdir()
    local=staging/source.name
    shutil.copyfile(source,local)
    wal=source.with_name(source.name+'-wal')
    if wal.exists():shutil.copyfile(wal,local.with_name(local.name+'-wal'))
    db=sqlite3.connect(local.as_uri()+'?mode=ro',uri=True)
    try:
        assert db.execute('PRAGMA quick_check').fetchall()==[('ok',)], 'database check failed'
        result['database_version']=db.execute('PRAGMA user_version').fetchone()[0]
        assert result['database_version']==5, 'offline migration required'
        target=sqlite3.connect('/output/state.sqlite3')
        try: db.backup(target)
        finally: target.close()
    finally: db.close()
Path('/output/database.json').write_text(json.dumps(result))
'''

VALIDATE = r'''
import json, shutil
from pathlib import Path
from media_importer.config import Config
from media_importer.store import Store
from media_importer.storage import inspect_cache
Config.load('/config/settings.json')
root=Path('/tmp/probe');root.mkdir()
source=Path('/snapshot/state.sqlite3')
if source.is_file(): shutil.copyfile(source,root/'state.sqlite3')
store=Store(root)
try:
    assert store.db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    identity=store.get_meta('cache_identity')
    if identity: assert inspect_cache(Path('/cache'),identity)['available'], 'original cache disk unavailable'
finally: store.close()
'''


def image_python(root, image, script, mounts=(), env=()):
    args = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp', '--entrypoint', 'python3']
    for value in env:
        args += ['-e', value]
    for source, target, readonly in mounts:
        # --mount also rejects absent host paths instead of creating replacements.
        if ',' in str(source):
            raise ValueError('更新工具暂不支持包含逗号的挂载路径')
        args += ['--mount', f'type=bind,source={source},target={target}' + (',readonly' if readonly else '')]
    return run([*args, image, '-c', script], root, capture=True)


def update(root, version=None):
    root = root.resolve()
    with update_lock(root):
        return perform(root, release(version))


def perform(root, manifest):
    """Download first; stop before snapshot; never rewind the DB after new code runs."""
    git = lambda *args: run(['git', *args], root, capture=True).strip()
    if Path(git('rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('请在通过 Git 克隆的项目目录中执行更新')
    if git('status', '--porcelain'):
        raise ValueError('项目有未提交的文件，请先处理；更新不会覆盖本地修改')
    if git('remote', 'get-url', 'origin') not in (f'https://github.com/{REPOSITORY}.git', f'git@github.com:{REPOSITORY}.git'):
        raise ValueError('origin 不是官方项目仓库，请手动检查发布来源')
    override = managed_override(root)
    paths = deployment(root)
    settings = paths['/config'] / 'settings.json'
    if not settings.is_file() or settings.is_symlink() or json.loads(settings.read_text())['version'] != 4:
        raise ValueError('运行配置需要先迁移到版本 4')
    tag = 'v' + manifest['version']
    print('检查稳定版本 ' + tag + '，下载期间现有服务继续运行…', flush=True)
    git('fetch', 'origin', 'tag', tag)
    if git('rev-parse', tag + '^{commit}') != manifest['commit']:
        raise ValueError('发布清单与 Git 标签不一致；未停止服务')
    git('merge-base', '--is-ancestor', 'HEAD', manifest['commit'])
    run(['docker', 'pull', manifest['image']], root)
    actual = run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python3', manifest['image'],
                  '-m', 'media_importer', '--version'], root, capture=True).strip()
    if actual.split()[-1] != manifest['version']:
        raise ValueError('镜像版本与发布清单不一致；未停止服务')
    running = run(['docker', 'compose', 'ps', '--services', '--status', 'running'], root, capture=True).splitlines()
    images = {}
    for name in SERVICES:
        container = run(['docker', 'compose', 'ps', '-a', '-q', name], root, capture=True).strip()
        if container:
            images[name] = run(['docker', 'inspect', '--format', '{{.Image}}', container], root, capture=True).strip()
    if (git('rev-parse', 'HEAD') == manifest['commit'] and override.is_file() and
            all(v['image'] == manifest['image'] for v in json.loads(override.read_text())['services'].values()) and
            len(images) == 2 and all(image == run(['docker', 'image', 'inspect', '--format', '{{.Id}}', manifest['image']], root, capture=True).strip() for image in images.values())):
        print('已安装此稳定版本；运行状态和配置保持不变。')
        return {'version': manifest['version'], 'changed': False}
    backup = root / 'backups' / ('update-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(parents=True, mode=0o700)
    record = {'format': 1, 'target': manifest, 'previous_commit': git('rev-parse', 'HEAD'),
              'previous_images': images, 'running_services': running, 'state': 'prepared'}
    atomic_json(backup / 'update.json', record)
    stopped = False
    try:
        print('停止服务并保存配置、任务数据库快照；完整缓存保留原位…', flush=True)
        stopped = True  # A failed stop may have stopped only one service.
        run(['docker', 'compose', 'stop', 'bot', 'backup'], root)
        if run(['docker', 'compose', 'ps', '--services', '--status', 'running'], root, capture=True).strip():
            raise RuntimeError('服务未完全停止，未读取数据库')
        for source, name in ((paths['/run/media-importer.env'], 'connection.env'), (settings, 'settings.json'),
                             (root / 'compose.yaml', 'compose.yaml')):
            shutil.copyfile(source, backup / name); os.chmod(backup / name, 0o600)
        if override.exists():
            shutil.copyfile(override, backup / 'compose.override.yaml')
        git('archive', '--format=tar', '--output=' + str(backup / 'source.tar'), 'HEAD')
        image_python(root, manifest['image'], SNAPSHOT,
                     [(paths['/data'], '/input', True), (backup, '/output', False)])
        image_python(root, manifest['image'], VALIDATE,
                     [(backup, '/snapshot', True), (paths['/config'], '/config', True),
                      (paths['/data/cache'], '/cache', True),
                      (paths['/run/media-importer.env'], '/run/media-importer.env', True)],
                     ['MEDIA_IMPORTER_ENV_FILE=/run/media-importer.env'])
        record['state'] = 'snapshot_verified'; atomic_json(backup / 'update.json', record)
        git('merge', '--ff-only', '--no-edit', manifest['commit'])
        atomic_json(override, {'x-media-importer-managed': 1,
                              'services': {name: {'image': manifest['image']} for name in SERVICES}})
        # Re-evaluate after source update. New releases may not move existing data.
        if deployment(root) != paths:
            raise RuntimeError('新版改变了挂载位置，已停止更新，请按发行说明处理')
        record['state'] = 'starting'; atomic_json(backup / 'update.json', record)
        if running:
            run(['docker', 'compose', 'up', '-d', '--no-build', '--pull', 'never',
                 '--wait', '--wait-timeout', '120', *running], root)
        record['state'] = 'complete'; atomic_json(backup / 'update.json', record)
    except BaseException:
        if stopped:
            try:
                run(['docker', 'compose', 'stop', 'bot', 'backup'], root)
            except RuntimeError:
                print('未能确认两个服务已停止，请检查 docker compose ps。', flush=True)
        record['state'] = 'failed'; atomic_json(backup / 'update.json', record)
        print('更新未完成，未回写旧数据库。请保留当前数据；恢复资料：' + str(backup), flush=True)
        raise
    print('更新完成：' + tag + ('；原来运行的服务已通过健康检查。' if running else '；服务保持停止状态。'))
    print('配置、开关、任务与完整缓存保留；相机重新连接确认规则不变。')
    print('更新快照：' + str(backup))
    return {'version': manifest['version'], 'changed': True, 'backup': str(backup)}
