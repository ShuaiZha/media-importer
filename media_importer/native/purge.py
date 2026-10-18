"""Explicit removal of one native installation's registered local data."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from ..settings import atomic_write
from . import platforms


def remove_tree(path):
    # On removable macOS volumes, deleting a file can also remove its AppleDouble
    # companion. A directory listing may therefore contain an already-gone file.
    def missing_ok(function, name, info):
        error = info[1]
        if not isinstance(error, FileNotFoundError):
            raise error
    if path.exists():
        shutil.rmtree(path, onerror=missing_ok)


def plan(home, record):
    """Fail before stopping the service if a registered path is ambiguous."""
    home = Path(home)
    project = Path(record['project'])
    paths = record['paths']
    targets = [Path(paths[name]) for name in ('config', 'data', 'cache')]
    env = Path(paths['env'])
    if env != project / '.env' or Path(paths['settings']) != targets[0] / 'settings.json':
        raise ValueError('安装记录中的配置路径不一致，未清理任何数据')
    if not Path(paths['cache_volume']).is_mount():
        raise ValueError('缓存盘未挂载；连接原磁盘后再彻底卸载')
    for path in [home, *targets, env]:
        if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
            raise ValueError('清理路径包含符号链接或已变化，未清理任何数据：' + str(path))
    shared = {Path.home().resolve(), Path(tempfile.gettempdir()).resolve()}
    if os.name != 'nt':
        shared.add(Path('/tmp').resolve())
    for path in [home, *targets]:
        if (path == Path(path.anchor) or path.is_mount()
                or any(path == common or path in common.parents for common in shared) or path == project
                or (path in project.parents and path != home)):
            raise ValueError('拒绝清理磁盘根目录、用户目录、项目目录或安装目录的上层：' + str(path))
    for path in targets:
        if path == home or path in home.parents:
            raise ValueError('数据目录与程序安装目录重叠，未清理：' + str(path))
        if path.exists() and not path.is_dir():
            raise ValueError('登记的数据目录不是普通目录，未清理：' + str(path))
    if env.exists() and not env.is_file():
        raise ValueError('登记的 .env 不是普通文件，未清理')
    command = Path(record['command'])
    if any(command == path or path in command.parents for path in targets):
        raise ValueError('命令入口与数据目录重叠，未清理任何数据')
    if sys.platform == 'win32' and (
            os.environ.get('MEDIA_IMPORTER_PURGE_WRAPPER') != platforms.label(home)
            or os.path.normcase(os.environ.get('MEDIA_IMPORTER_COMMAND_WRAPPER', '')) != os.path.normcase(str(command))):
        raise ValueError('Windows 彻底卸载请使用此安装生成的命令入口：' + str(command))
    if sys.platform == 'win32' and home in command.parents:
        raise ValueError('Windows 命令入口位于安装目录内；请先将命令安装到独立的 --bin-dir')
    result = {'home': str(home), 'directories': list(dict.fromkeys(map(str, targets))),
              'env': str(env), 'command': str(command), 'project': str(project)}
    pending = home / 'purge.json'
    if pending.exists():
        if pending.is_symlink() or json.loads(pending.read_text(encoding='utf-8')) != result:
            raise ValueError('未完成的清理范围与安装记录不一致，请先核对路径')
    else:
        # A changed .env or replaced data directory must not redirect a purge.
        from .lifecycle import assert_paths
        assert_paths(record)
        marker = Path(paths['data']) / 'deployment.json'
        if marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != {'mode': 'native', 'home': str(home)}:
            raise ValueError('数据目录未登记到此原生安装，未清理任何数据')
        if command.exists() and (command.is_symlink() or not command.is_file() or
                'media-importer managed launcher ' + platforms.label(home) not in command.read_text(encoding='utf-8', errors='replace')):
            raise ValueError('命令入口已被替换，未清理任何数据')
    return result


def remove_data(home, approved):
    """The service and task worker must already be stopped by the caller."""
    atomic_write(home / 'purge.json', json.dumps(approved, ensure_ascii=False, indent=2) + '\n')
    # Nested paths need removing only once. Never remove the project directory:
    # a checkout, hand-created backup or another application's files can live there.
    roots = [Path(p) for p in approved['directories']]
    for path in roots:
        if not any(other in path.parents for other in roots):
            if path.is_symlink():
                raise ValueError('数据目录在卸载期间发生变化，未继续清理：' + str(path))
            remove_tree(path)
    Path(approved['env']).unlink(missing_ok=True)


def finish(home, record):
    """Called after releasing install.lock; Windows finishes in its .cmd wrapper."""
    approved = plan(home, record)
    if any(Path(p).exists() for p in [*approved['directories'], approved['env']]):
        raise RuntimeError('本机数据尚未清理完成，未移除安装记录')
    if sys.platform == 'win32':
        atomic_write(home / 'purge-ready', 'local data removed; wrapper may remove installation\n')
        return
    command = Path(record['command'])
    if command.exists():
        signature = 'media-importer managed launcher ' + platforms.label(home)
        if command.is_symlink() or signature not in command.read_text(encoding='utf-8', errors='replace'):
            raise ValueError('命令入口已经变化，未删除；安装目录保留')
    remove_tree(home)
    command.unlink(missing_ok=True)
