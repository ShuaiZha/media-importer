"""Native lifecycle; data removal requires the explicit local purge operation."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

from ..config import Config
from ..locking import file_lock
from ..settings import atomic_write
from . import platforms, releases


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load(home):
    value = json.loads((home / 'installation.json').read_text())
    if value.get('format') != 1 or not value.get('project'):
        raise ValueError('原生安装记录不受支持，请保留数据并检查 installation.json')
    return value


def paths(project):
    from dotenv import dotenv_values
    project = project.expanduser().resolve()
    env = project / '.env'
    if not env.is_file():
        raise ValueError('项目缺少 .env；全新安装请运行 install 不带 --project，以进入配置向导')
    values = dotenv_values(env, interpolate=False)
    result = {'project': str(project), 'env': str(env)}
    for kind, fallback in (('config', './config'), ('data', './data'), ('cache', './data/cache')):
        path = Path(values.get('MEDIA_IMPORTER_' + kind.upper() + '_HOST_DIR') or fallback).expanduser()
        if not path.is_absolute():
            path = project / path
        if path.is_symlink() or not path.is_dir():
            raise ValueError(kind + ' 目录不可访问或为符号链接；请连接原磁盘，不创建替代目录')
        result[kind] = str(path.resolve())
    if result['cache'] in (result['data'], result['config']) or result['data'] == result['config']:
        raise ValueError('配置、任务和缓存必须是不同目录')
    result['settings'] = str(Path(result['config']) / 'settings.json')
    if not Path(result['settings']).is_file():
        raise ValueError('缺少 settings.json；未修改已有配置')
    volume = Path(result['cache'])
    while not volume.is_mount() and volume.parent != volume:
        volume = volume.parent
    result['cache_volume'] = str(volume)
    return result


def runtime_env(record):
    p = record['paths']
    return {**os.environ, 'MEDIA_IMPORTER_RUNTIME': 'native', 'MEDIA_IMPORTER_ENV_FILE': p['env'],
            'MEDIA_IMPORTER_CONFIG': p['settings'], 'MEDIA_IMPORTER_DATA_DIR': p['data'],
            'MEDIA_IMPORTER_CACHE_DIR': p['cache'], 'MEDIA_IMPORTER_CACHE_VOLUME': p['cache_volume'],
            'MEDIA_IMPORTER_CONFIG_HOST_DIR': p['config'], 'MEDIA_IMPORTER_DATA_HOST_DIR': p['data'],
            'MEDIA_IMPORTER_CACHE_HOST_DIR': p['cache'], 'MEDIA_IMPORTER_REQUIRE_CACHE_MOUNT': '0',
            'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1'}


@contextmanager
def environment(record):
    before = dict(os.environ)
    os.environ.update(runtime_env(record))
    try:
        yield
    finally:
        os.environ.clear(); os.environ.update(before)


def assert_no_docker(record, *, native_owned=False):
    if not shutil.which('docker'):
        return
    try:
        result = subprocess.run(['docker', 'ps', '-q'], capture_output=True, text=True, timeout=10)
        if result.returncode:
            # An existing Docker database cannot be adopted if Docker is uninspectable.
            if (Path(record['paths']['data'], 'state.sqlite3').exists()
                    and not Path(record['paths']['data'], 'deployment.json').exists() and not native_owned):
                raise RuntimeError('无法确认旧 Docker 已停止；请先启动 Docker、停止本项目服务，再安装原生版')
            return
        ids = result.stdout.split()
        if not ids:
            return
        result = subprocess.run(['docker', 'inspect', '--format', '{{json .Mounts}}', *ids],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError('无法核对 Docker 挂载，未启动原生服务')
        protected = {Path(record['paths'][k]).resolve() for k in ('data', 'cache')}
        for line in result.stdout.splitlines():
            for mount in json.loads(line):
                if mount.get('Type') == 'bind' and Path(mount['Source']).resolve() in protected:
                    raise RuntimeError('Docker 正在使用此任务或缓存目录；先执行 docker compose stop，再运行原生命令')
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise RuntimeError('无法核对 Docker 状态，未启动原生服务') from None


def assert_paths(record):
    fresh = paths(Path(record['project']))
    if fresh != record['paths']:
        raise ValueError('本机目录配置已改变；请先停止服务并按迁移说明重新安装，不自动切换数据位置')


def claim(home, record):
    path = Path(record['paths']['data']) / 'deployment.json'
    if path.exists():
        owner = json.loads(path.read_text())
        if owner != {'mode': 'native', 'home': str(home)}:
            raise ValueError('此任务目录属于另一套原生安装，请先卸载原安装；数据会保留')
    save(path, {'mode': 'native', 'home': str(home)})


def snapshot(home, record):
    """Services must be stopped. Includes WAL transactions; no cache/media copying."""
    folder = home / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder.mkdir(parents=True, mode=0o700)
    for source, name in ((record['paths']['env'], 'connection.env'),
                         (record['paths']['settings'], 'settings.json'),
                         (home / 'installation.json', 'installation.json')):
        if Path(source).is_file():
            shutil.copyfile(source, folder / name); os.chmod(folder / name, 0o600)
    from ..credentials import copy_credentials
    copy_credentials(Path(record['paths']['settings']).parent, folder)
    source = Path(record['paths']['data']) / 'state.sqlite3'
    if source.exists():
        # Work on copies: SQLite never creates SHM or checkpoints the original DB.
        with tempfile.TemporaryDirectory(dir=folder) as temporary:
            dbpath = Path(temporary) / source.name
            shutil.copyfile(source, dbpath)
            wal = source.with_name(source.name + '-wal')
            if wal.exists():
                shutil.copyfile(wal, dbpath.with_name(dbpath.name + '-wal'))
            with closing(sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('PRAGMA quick_check').fetchall() != [('ok',)] or db.execute('PRAGMA user_version').fetchone()[0] != 5:
                    raise ValueError('数据库核验未通过，服务保持停止；未修改原数据库')
                with closing(sqlite3.connect(folder / 'state.sqlite3')) as target:
                    db.backup(target)
    save(folder / 'snapshot.json', {'version': record.get('version'), 'paths': record['paths'],
                                   'cache_copied': False, 'complete': True})
    return folder


def validate(bundle, record):
    info = releases.metadata(bundle)
    result = subprocess.run([str(releases.executable(bundle)), '--version'], capture_output=True, text=True, timeout=30)
    if result.returncode or result.stdout.strip() != info['version']:
        raise ValueError('安装包版本与程序不匹配，未切换版本')
    result = subprocess.run([str(releases.executable(bundle)), '_worker', 'validate'],
                            env=runtime_env(record), capture_output=True, timeout=45)
    if result.returncode:
        raise ValueError('候选程序无法读取现有配置，未切换版本；凭据未输出')
    # Cache checks are read-only and work without an attached camera or SMB.
    with environment(record):
        Config.load(record['paths']['settings'])
    return info


def command_path(bin_dir):
    return bin_dir / ('media-importer.cmd' if sys.platform == 'win32' else 'media-importer')


def launcher(home, record):
    target = Path(record['command'])
    target.parent.mkdir(parents=True, exist_ok=True)
    signature = 'media-importer managed launcher ' + platforms.label(home)
    if target.exists() and signature not in target.read_text(errors='replace'):
        raise ValueError('命令路径已存在其他程序，未覆盖：' + str(target))
    executable = str(releases.executable(Path(record['bundle'])))
    if sys.platform == 'win32':
        from .windows import launcher as windows_launcher
        content = windows_launcher(home, executable)
    else:
        content = '#!/bin/sh\n# ' + signature + '\nexec ' + shlex.quote(executable) + ' --home ' + shlex.quote(str(home)) + ' "$@"\n'
    atomic_write(target, content, 0o755)


def copy_bundle(home, source):
    info = releases.metadata(source)
    destination = home / 'releases' / (info['version'] + '-' + uuid.uuid4().hex[:8])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    return destination, info


def stop(home, *, unregister=False):
    # Block supervisor restart before asking it to shut down both workers.
    atomic_write(home / 'stopped', 'stopped\n')
    running = home / 'running.json'
    if running.exists():
        until = time.monotonic() + 55
        while running.exists() and time.monotonic() < until:
            with file_lock(home / 'service.lock', False) as free:
                if free:
                    break
            time.sleep(.25)
    (platforms.unregister if unregister else platforms.stop)(home)
    with file_lock(home / 'service.lock', False) as free:
        if not free:
            raise RuntimeError('后台服务尚未停止，未修改程序或数据')
    running.unlink(missing_ok=True)


def start(home, record):
    assert_paths(record); assert_no_docker(record); claim(home, record)
    (home / 'logs').mkdir(exist_ok=True)
    (home / 'stopped').unlink(missing_ok=True)
    platforms.register(home, releases.executable(Path(record['bundle'])))
    until = time.monotonic() + 30
    while time.monotonic() < until:
        try:
            state = json.loads((home / 'running.json').read_text())
            if state.get('version') == record['version'] and all(state.get('workers', {}).values()) and len(state['workers']) == 2:
                return
        except (OSError, ValueError):
            pass
        time.sleep(.5)
    raise RuntimeError('后台服务仍在启动或重试；请查看 status 与 logs。Mac 若出现可移动卷访问提示，请允许本程序访问素材盘。配置和数据已保留')


def install(home, source, project, bin_dir, *, no_start=False):
    if (home / 'installation.json').exists():
        raise ValueError('此位置已安装原生版；请使用 update，未覆盖任何配置')
    target = command_path(bin_dir)
    if target.exists():
        raise ValueError('命令路径已存在；请使用其他 --bin-dir 或更新原安装')
    info = releases.metadata(source)
    if project is None:
        project = home / 'instance'
        from scripts.setup import setup
        project.mkdir(parents=True, exist_ok=True)
        setup(project, checks=None, native=True)
    record = {'format': 1, 'project': str(project.resolve()), 'paths': paths(project),
              'version': info['version'], 'command': str(target), 'bundle': ''}
    programs = home / 'releases'
    for key in ('config', 'data', 'cache'):
        path = Path(record['paths'][key])
        if path == programs or programs in path.parents or path in programs.parents and path != Path(record['project']):
            raise ValueError('程序目录不能与配置、任务或缓存目录重叠')
    assert_no_docker(record)
    data = Path(record['paths']['data'])
    from ..store import worker_lock
    with worker_lock(data) as free:
        if not free:
            raise ValueError('已有传输进程使用此任务目录')
        validate(source, record)
        snapshot(home, record)
        # Native and Docker use the same DB format; no tasks or paths are reset.
        bundle, _ = copy_bundle(home, source); record['bundle'] = str(bundle)
        claim(home, record)
        save(home / 'installation.json', record)
        launcher(home, record)
    if not no_start:
        start(home, record)
    else:
        atomic_write(home / 'stopped', 'stopped\n')
    return record


def update(home, source, *, no_start=False):
    record = load(home); info = releases.metadata(source)
    if releases.version(info['version']) < releases.version(record['version']):
        raise ValueError('不自动降级数据库和程序；请使用升级前快照恢复')
    if info['version'] == record['version']:
        return record, False
    assert_paths(record); assert_no_docker(record)
    validate(source, record)
    bundle, _ = copy_bundle(home, source)
    was_running = not (home / 'stopped').exists()
    stop(home)
    from ..store import worker_lock
    with worker_lock(record['paths']['data']) as free:
        if not free:
            raise ValueError('仍有进程使用此任务目录；未切换版本')
        snapshot(home, record)
        record.update(version=info['version'], bundle=str(bundle))
        save(home / 'installation.json', record); launcher(home, record)
    if was_running and not no_start:
        start(home, record)
    return record, True


def uninstall(home, *, purge_data=False, purge_plan=None):
    record = load(home)
    approved = None
    if purge_data:
        from .purge import plan
        approved = plan(home, record)
        if purge_plan is not None and approved != purge_plan:
            raise ValueError('清理范围在确认后发生变化，请重新运行命令核对；未卸载或删除')
    stop(home, unregister=True)
    # A partially completed purge may have removed deployment.json already.
    # The validated purge journal still proves this installation owns the paths.
    assert_no_docker(record, native_owned=approved is not None)
    if approved:
        from ..store import worker_lock
        from .purge import remove_data
        data = Path(record['paths']['data'])
        if data.is_dir():
            with worker_lock(data) as free:
                if not free:
                    raise RuntimeError('仍有传输进程使用任务目录，未清理数据')
        # Preserve installation.json until all registered data paths are gone,
        # allowing a failed, explicitly approved purge to be retried.
        remove_data(home, approved)
        return record, []
    marker = Path(record['paths']['data']) / 'deployment.json'
    if marker.exists() and json.loads(marker.read_text()) == {'mode': 'native', 'home': str(home)}:
        marker.unlink()
    command = Path(record['command'])
    wrapper_will_remove = (sys.platform == 'win32' and
        os.path.normcase(os.environ.get('MEDIA_IMPORTER_COMMAND_WRAPPER', '')) == os.path.normcase(str(command)))
    if not wrapper_will_remove and command.is_file() and ('media-importer managed launcher ' + platforms.label(home)) in command.read_text(errors='replace'):
        command.unlink()
    # Keep the running Windows executable until the OS releases it. Never use
    # recursive deletion on project, data, cache, or user-selected paths.
    retained = []
    folder = home / 'releases'
    if folder.is_dir() and not folder.is_symlink():
        for bundle in folder.iterdir():
            if bundle.is_dir() and not bundle.is_symlink() and (bundle / 'bundle.json').is_file():
                try:
                    shutil.rmtree(bundle)
                except OSError:
                    retained.append(str(bundle))
    save(home / 'uninstalled.json', {**record, 'retained_programs': retained})
    (home / 'installation.json').unlink()
    return record, retained
