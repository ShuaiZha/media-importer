"""Local credential references. Passwords never enter settings, jobs or cards."""
import json
import re
import time
from pathlib import Path

from .model import ConfigError


def credential_path(directory, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise ConfigError('凭据引用无效，请重新填写归档存储')
    directory = Path(directory) / 'credentials'
    path = directory / (identifier + '.json')
    if directory.is_symlink() or path.is_symlink():
        raise ConfigError('凭据目录不能是符号链接')
    return path


def stage(directory, identifier, password):
    from .settings import atomic_write
    if (not isinstance(password, str) or not password or len(password) > 1000
            or any(ord(c) < 32 for c in password)):
        raise ConfigError('密码不能为空、超过 1000 字符或包含控制字符')
    path = credential_path(directory, identifier)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Duplicate delivery must not overwrite a credential already checked by a job.
    if path.exists():
        if read(path) != password:
            raise ConfigError('请求内容已变化，请重新打开归档存储')
        return identifier
    atomic_write(path, json.dumps({'password': password}, ensure_ascii=False) + '\n')
    return identifier


def read(path):
    path = Path(path)
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8'))
        password = value['password']
        if (set(value) != {'password'} or not isinstance(password, str) or not password
                or any(ord(c) < 32 for c in password)):
            raise ValueError()
        return password
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigError('本机 SMB 凭据不可用，请通过飞书归档存储重新填写密码') from None


def copy_credentials(config_directory, target):
    """Include references in update snapshots and portable backups, without symlinks."""
    source = Path(config_directory) / 'credentials'
    if not source.exists():
        return
    if source.is_symlink():
        raise ConfigError('凭据目录不能是符号链接')
    destination = Path(target) / 'credentials'
    for path in source.glob('*.json'):
        if not re.fullmatch(r'[0-9a-f]{32}\.json', path.name):
            continue
        read(path)  # Validate before copying; do not return or log the content.
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        from .settings import atomic_write
        atomic_write(destination / path.name, path.read_text(encoding='utf-8'))


def prune(directory, active):
    """Discard only our expired, unreferenced drafts after cards/jobs have expired."""
    root = Path(directory) / 'credentials'
    if root.is_symlink():
        raise ConfigError('凭据目录不能是符号链接')
    for path in root.glob('*.json'):
        if (re.fullmatch(r'[0-9a-f]{32}\.json', path.name) and path.stem != active
                and not path.is_symlink() and time.time() - path.stat().st_mtime > 7 * 86400):
            path.unlink()
