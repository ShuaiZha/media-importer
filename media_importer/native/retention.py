"""Reclaim owned program bundles after a healthy start; never touch instance data."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

from ..locking import file_lock
from . import releases

OWNER = '.native-owner.json'


def program_path(home, value, record):
    """Only generated, immediate program directories qualify for removal."""
    path = Path(value)
    root = home / 'releases'
    if root.is_symlink() or path.parent != root or path.is_symlink() or not path.is_dir():
        return None
    if not re.fullmatch(r'\d+\.\d+\.\d+-[0-9a-f]{8}', path.name):
        return None
    for key in ('config', 'data', 'cache'):
        protected = Path(record['paths'][key]).resolve()
        if path == protected or path in protected.parents or protected in path.parents:
            return None
    return path


def owned_path(home, value, record):
    path = program_path(home, value, record)
    if not path:
        return None
    try:
        info = releases.metadata(path)
        if not path.name.startswith(info['version'] + '-'):
            return None
    except (OSError, ValueError, KeyError):
        return None
    return path


def previous_records(home, record):
    """Completed upgrade snapshots provide ownership evidence for old installers."""
    root = home / 'backups'
    if root.is_symlink() or not root.is_dir():
        return []
    result = []
    for folder in sorted(root.iterdir(), reverse=True):
        if folder.is_symlink() or not folder.is_dir():
            continue
        receipt, installation = folder / 'snapshot.json', folder / 'installation.json'
        if receipt.is_symlink() or installation.is_symlink():
            continue
        try:
            snapshot = json.loads(receipt.read_text())
            old = json.loads(installation.read_text())
            if (snapshot.get('complete') is True and old.get('format') == 1
                    and old.get('command') == record['command']
                    and old.get('version') == snapshot.get('version')):
                path = owned_path(home, old['bundle'], record)
                if path and releases.metadata(path)['version'] == old['version']:
                    result.append(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def ready(record, since):
    """Require fresh heartbeats from this start, not just two living processes."""
    from ..status import healthy
    from ..bot import healthy as bot_healthy
    directory = Path(record['paths']['data'])
    if not healthy(directory) or not bot_healthy(directory):
        return False
    try:
        with closing(sqlite3.connect((directory / 'state.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            state = {key: json.loads(value) for key, value in db.execute(
                "SELECT key,value FROM meta WHERE key IN ('service','bot')")}
        stamps = (state['service']['started_at'], state['bot']['heartbeat_at'])
        # Runtime timestamps have one-second resolution.
        return all(datetime.fromisoformat(stamp).timestamp() >= int(since) for stamp in stamps)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False


def in_use(path):
    """Unknown process state postpones removal. Windows enforces open-file locks."""
    if Path(sys.executable).resolve().is_relative_to(path):
        return True
    if sys.platform == 'win32':
        return False
    if sys.platform == 'linux':
        try:
            for proc in Path('/proc').iterdir():
                if proc.name.isdigit():
                    try:
                        if (proc / 'exe').resolve(strict=True).is_relative_to(path):
                            return True
                    except (FileNotFoundError, PermissionError, ProcessLookupError):
                        continue
            return False
        except OSError:
            return True
    try:
        result = subprocess.run(['/usr/sbin/lsof', '-t', '+D', str(path)],
                                capture_output=True, text=True, timeout=3)
        return bool(result.stdout.strip() or result.stderr.strip() or result.returncode not in (0, 1))
    except (OSError, subprocess.TimeoutExpired):
        return True


def maintain(home, expected, since):
    """Called by the supervisor only; install/update and read-only queries never race GC.

    Return False to retry later (startup, installer lock or obsolete files in use).
    """
    from . import lifecycle
    if not ready(expected, since):
        return False
    with file_lock(home / 'install.lock', False) as free:
        if not free:
            return False
        record = lifecycle.load(home)
        if record['bundle'] != expected['bundle'] or (home / 'stopped').exists():
            return False
        current = owned_path(home, record['bundle'], record)
        if not current:
            return False
        history = previous_records(home, record)
        recovered = 'previous_bundle' not in record
        if 'previous_bundle' not in record:
            # The first start after upgrading from an older installer.
            record['previous_bundle'] = next((str(p) for p in history if p != current), '')
        previous = record.get('previous_bundle')
        if previous and not owned_path(home, previous, record):
            # An inaccessible rollback is a diagnostic issue, not permission to
            # guess a replacement and start deleting programs.
            return False
        keep = {current, *([Path(previous)] if previous else [])}
        if recovered or record.get('healthy_bundle') != str(current):
            record['healthy_bundle'] = str(current)
            lifecycle.save(home / 'installation.json', record)
        removed, pending = [], []
        known = set(history)
        try:
            journal = json.loads((home / 'program-cleanup.json').read_text())
            if journal.get('format') == 1:
                known.update(Path(p) for p in journal.get('pending', []))
        except (OSError, ValueError, TypeError):
            pass
        candidates = []
        for path in sorted((home / 'releases').iterdir()):
            if path in keep or not program_path(home, path, record):
                continue
            marker = path / OWNER
            try:
                owned = (not marker.is_symlink() and json.loads(marker.read_text()) ==
                         {'format': 1, 'home': str(home)})
            except (OSError, ValueError):
                owned = False
            if path not in known and not owned:
                continue
            candidates.append(path)
        def receipt(waiting):
            lifecycle.save(home / 'program-cleanup.json', {
                'format': 1, 'current': str(current), 'previous': previous or None,
                'checked_at': time.time(), 'removed': removed, 'pending': waiting})
        # Journal first: a crash or Windows sharing violation may remove some
        # package metadata before rmtree fails. Retry only these proven paths.
        receipt([str(path) for path in candidates])
        deadline = time.monotonic() + 5
        for path in candidates:
            if time.monotonic() >= deadline or in_use(path):
                pending.append(str(path)); continue
            try:
                shutil.rmtree(path)
                removed.append(str(path))
            except OSError:
                pending.append(str(path))
        receipt(pending)
        return not pending
