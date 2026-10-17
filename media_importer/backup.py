"""Offline, portable bundles. Media uses bounded identity evidence, not full hashes."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath

from . import __version__
from .model import BackupError, now
from .settings import Settings, atomic_write
from .storage import cache_evidence, inspect_cache, inspect_cached_file, MARKER
from .store import Store, worker_lock, fsync_directory


def entry(root, path):
    relative = path.relative_to(root).as_posix()
    info = path.stat()
    result = {"path": relative, "bytes": info.st_size}
    if relative.startswith("cache/") and path.suffix in ('.bin', '.download'):
        result["evidence"] = cache_evidence(path, relative, "migration-bundle")
    else:
        result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def regular_files(root):
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError("备份目录不能包含符号链接")
        if path.is_file():
            # macOS rewrites AppleDouble sidecars when crossing filesystems.
            # They are filesystem metadata, not cache content or its proof.
            if path.name.startswith('._'):
                with path.open('rb') as handle:
                    if handle.read(4) == b'\x00\x05\x16\x07':
                        continue
            yield path


def safe_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or not path.parts:
        raise BackupError("备份清单包含无效路径")
    target = root.joinpath(*path.parts)
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise BackupError("备份文件不能通过符号链接访问")
    return target


def export(directory, settings_path, env_path, bundle):
    directory, bundle = Path(directory), Path(bundle)
    if any(bundle.iterdir()):
        raise BackupError("备份目标必须为空目录")
    with worker_lock(directory) as acquired:
        if not acquired:
            raise BackupError("请先停止传输与机器人服务再备份")
        store = Store(directory)
        try:
            if [r[0] for r in store.db.execute("PRAGMA quick_check")] != ["ok"]:
                raise BackupError("任务数据库检查失败，未生成有效备份")
            cache = inspect_cache(store.cache, store.get_meta("cache_identity"))
            if not cache["available"]:
                raise BackupError("缓存盘不可用，不能生成完整迁移备份")
            files = list(regular_files(store.cache))
            required = sum(p.stat().st_size for p in files) + (directory / 'state.sqlite3').stat().st_size + 16 * 1024 ** 2
            if shutil.disk_usage(bundle).free < required:
                raise BackupError("备份目的地空间不足；备份包含完整缓存")
            warnings = []
            for row in store.files():
                cached = store.cached_path(row)
                if row['state'] in ('cached', 'staged', 'publishing') and not cached.is_file():
                    raise BackupError("存在未归档但缺失的完整缓存，请先检查原缓存盘")
                if cached.is_file():
                    checked = inspect_cached_file(cached, json.loads(row['cache_identity'] or 'null'),
                                                  row['asset_id'], store.get_meta('cache_identity'))
                    if not checked['valid']:
                        warnings.append({"file_id": row['id'], "message": "缓存原样备份，恢复后仍需核验"})
            (bundle / 'cache').mkdir(mode=0o700)
            for source in files:
                relative = source.relative_to(store.cache)
                target = bundle / 'cache' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                before = cache_evidence(source, relative.as_posix(), 'copy')
                shutil.copy2(source, target)
                with target.open('rb') as handle:
                    os.fsync(handle.fileno())
                if before != cache_evidence(source, relative.as_posix(), 'copy') or before != cache_evidence(target, relative.as_posix(), 'copy'):
                    raise BackupError("缓存复制身份核验失败，备份未完成")
                print('已备份缓存：' + source.name, flush=True)
            with closing(sqlite3.connect(bundle / 'state.sqlite3')) as target:
                store.db.backup(target)
            shutil.copy2(settings_path, bundle / 'settings.json')
            shutil.copy2(env_path, bundle / 'connection.env')
            os.chmod(bundle / 'connection.env', 0o600)
            draft = {"format": 1, "version": __version__, "created_at": now(),
                     "cache_files": len(files), "cache_bytes": sum(p.stat().st_size for p in files),
                     "warnings": warnings, "entries": [entry(bundle, p) for p in regular_files(bundle)]}
            atomic_write(bundle / 'draft.json', json.dumps(draft, ensure_ascii=False, indent=2))
            return draft
        finally:
            store.close()


def verify(bundle, draft=False):
    root = Path(bundle)
    manifest = json.loads((root / ('draft.json' if draft else 'manifest.json')).read_text())
    if manifest.get('format') != 1 or not isinstance(manifest.get('entries'), list):
        raise BackupError("备份清单格式不受支持")
    paths = [e['path'] for e in manifest['entries']]
    required = {'state.sqlite3', 'settings.json', 'connection.env'}
    if not required.issubset(paths) or len(paths) != len(set(paths)):
        raise BackupError("备份清单缺少必要文件或包含重复路径")
    for expected in manifest['entries']:
        path = safe_path(root, expected['path'])
        if not path.is_file() or entry(root, path) != expected:
            raise BackupError("备份文件核验失败：" + expected['path'])
    # Export produces a closed, standalone snapshot. Immutable avoids creating
    # WAL sidecars on a read-only backup volume (never use it on the live DB).
    with closing(sqlite3.connect('file:' + str(root / 'state.sqlite3') + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise BackupError("备份数据库检查失败")
    return manifest


def seal(bundle):
    root = Path(bundle)
    manifest = verify(root, draft=True)
    manifest['entries'] += [entry(root, p) for p in regular_files(root / 'project')]
    atomic_write(root / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    (root / 'draft.json').unlink()
    fsync_directory(root)
    return {k: manifest[k] for k in ('version', 'cache_files', 'cache_bytes', 'warnings')}


def prepare_restore(directory, settings_path, bundle=None):
    """Only runtime commands and observations expire; archive/task rows are retained."""
    if bundle is not None:
        manifest = verify(bundle)
        for expected in manifest['entries']:
            if expected['path'] == 'state.sqlite3' or expected['path'].startswith('cache/'):
                path = safe_path(Path(directory), expected['path'])
                if not path.is_file() or entry(Path(directory), path) != expected:
                    raise BackupError('恢复后的记录或缓存与备份不符：' + expected['path'])
    store = Store(directory)
    try:
        cache = inspect_cache(store.cache, store.get_meta('cache_identity'))
        if not cache['available']:
            raise BackupError("恢复后的缓存挂载或卷身份不符，请保留文件并检查路径")
        Settings(settings_path).update_preferences({'backup': {'enabled': False, 'delete_source_after_import': False}})
        with store.db:
            tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'bot_actions' in tables:
                store.db.execute('DELETE FROM bot_actions')
            if 'bot_jobs' in tables:
                store.db.execute("UPDATE bot_jobs SET state='error',error='迁移后旧请求已失效' WHERE state IN ('queued','applied')")
            if 'notification_events' in tables:
                store.db.execute("UPDATE notification_events SET state='cancelled' WHERE state='pending'")
            if 'maintenance_requests' in tables:
                store.db.execute("UPDATE maintenance_requests SET state='cancelled' WHERE state IN ('pending','running','done')")
            store.db.execute("DELETE FROM meta WHERE key IN ('status','service','bot','preflight','notification_state','notification_delivery') OR key LIKE 'notification_progress_card:%'")
        store.invalidate_inventory('迁移后需重新核对相机清单')
        store.set_meta('restored_at', now())
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return {"sync_enabled": False, "delete_source": False, "files": len(store.files())}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('export', 'seal', 'verify', 'restore'))
    parser.add_argument('--bundle', default='/bundle')
    parser.add_argument('--data', default='/data')
    parser.add_argument('--settings', default='/config/settings.json')
    args = parser.parse_args()
    try:
        if args.action == 'export':
            export(args.data, args.settings, os.environ['MEDIA_IMPORTER_ENV_FILE'], args.bundle)
            result = {"exported": True}
        elif args.action == 'seal':
            result = seal(args.bundle)
        elif args.action == 'verify':
            manifest = verify(args.bundle)
            result = {"verified": True, "cache_files": manifest['cache_files'], "cache_bytes": manifest['cache_bytes']}
        else:
            result = prepare_restore(args.data, args.settings, args.bundle)
        print(json.dumps(result, ensure_ascii=False))
    except (BackupError, OSError, ValueError) as exc:
        # Paths and fixed messages only; never serialize environment values.
        print('备份迁移未完成：' + str(exc))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
