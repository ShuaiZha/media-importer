"""Small durable cache receipts and offline recovery, independent of the live DB."""
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath

from .config import DEFAULTS, runtime_config
from .model import Asset, BackupError, CacheFileError, Cancelled, now
from .paths import safe_relative
from .settings import atomic_write
from .storage import MARKER, inspect_cache, inspect_cached_file, require_cache, is_appledouble, unlink_cache
from .store import Store, fsync_directory

RECEIPTS = '.receipts'
STATES = {'cached', 'staged', 'publishing', 'uploaded'}


def receipt_path(cache, identifier):
    if not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise CacheFileError('缓存恢复清单的任务编号无效')
    folder = Path(cache) / RECEIPTS
    if folder.is_symlink():
        raise CacheFileError('缓存恢复清单目录不能是符号链接')
    path = folder / (identifier + '.json')
    if path.is_symlink():
        raise CacheFileError('缓存恢复清单不能是符号链接')
    return path


def checkpoint(store, row):
    """Must succeed before deleting a source or issuing a remote publish."""
    require_cache(store)
    path = store.cached_path(row)
    evidence = json.loads(row['cache_identity'] or 'null')
    if row['state'] not in STATES or not inspect_cached_file(
            path, evidence, row['asset_id'], store.get_meta('cache_identity'))['valid']:
        raise CacheFileError('完整缓存未通过核验，未更新恢复清单；文件保留')
    receipt = {
        'format': 1, 'file_id': row['id'], 'asset': json.loads(row['asset']),
        'destination': row['destination'], 'binding': store.get_meta('destination_binding'),
        'cache_identity': evidence, 'state': row['state'], 'remote_path': row['remote_path'],
        'temp_path': row['temp_path'], 'remote_identity': json.loads(row['remote_identity'] or 'null'),
        'cleanup': row['cleanup'], 'source_deleted_at': row.get('source_deleted_at'),
        'source_absent_at': row.get('source_absent_at'), 'created_at': row['created_at'],
    }
    target = receipt_path(store.cache, row['id'])
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True) + '\n'
    try:
        if target.exists() and target.read_text() == encoded:
            return
        atomic_write(target, encoded)
    except OSError as exc:
        require_cache(store)
        raise CacheFileError('此文件的恢复清单无法保存，保留完整缓存和原片，请检查目录权限') from exc


def forget(store, row):
    target = receipt_path(store.cache, row['id'])
    if target.exists():
        saved = json.loads(target.read_text())
        if saved.get('file_id') != row['id'] or saved.get('asset', {}).get('identity_key') != row['asset_id']:
            raise CacheFileError('缓存恢复清单归属不符，保留待处理')
        unlink_cache(target)
        fsync_directory(target.parent)


def inspect_receipts(cache):
    """Read bounded proofs only. Never guess ownership from size or filename."""
    cache = Path(cache)
    marker = cache / MARKER
    if marker.is_symlink() or not marker.is_file():
        raise BackupError('缓存盘缺少原卷标识，不能从清单恢复')
    volume = marker.read_text().strip()
    try:
        uuid.UUID(hex=volume)
    except ValueError as exc:
        raise BackupError('缓存卷标识无效，保留原目录等待核对') from exc
    health = inspect_cache(cache, volume)
    if not health['available']:
        raise BackupError(health['reason'])
    folder = cache / RECEIPTS
    if folder.is_symlink():
        raise BackupError('缓存恢复清单目录不能是符号链接')
    valid, rejected, known = [], [], set()
    for path in sorted(folder.glob('*.json')):
        if is_appledouble(path):
            continue
        try:
            if path.is_symlink() or path.stat().st_size > 256 * 1024:
                raise ValueError('清单类型或大小无效')
            value = json.loads(path.read_text())
            if value.get('format') != 1 or value.get('state') not in STATES:
                raise ValueError('清单格式不受支持')
            asset = Asset.from_dict(value['asset'])
            safe_relative(asset.path)
            identifier = Store.file_id(value['destination'], asset)
            if receipt_path(cache, identifier) != path or value['file_id'] != identifier:
                raise ValueError('素材与任务编号不符')
            known.add(identifier)
            evidence = value['cache_identity']
            if not isinstance(evidence, dict) or evidence.get('version') != 2 or evidence.get('volume_id') != volume:
                raise ValueError('完整导入凭据或缓存卷不符')
            if evidence.get('asset_id') != asset.identity or evidence.get('size') != asset.size:
                raise ValueError('素材与完整导入凭据不符')
            for key in ('remote_path', 'temp_path'):
                if value.get(key):
                    safe_relative(value[key])
            binding = value.get('binding')
            if not isinstance(binding, dict) or binding.get('id') != value['destination'] or not isinstance(binding.get('connection'), str):
                raise ValueError('目的地绑定不完整')
            if value['state'] in ('staged', 'publishing', 'uploaded') and not all(
                    value.get(k) for k in ('remote_path', 'temp_path', 'remote_identity')):
                raise ValueError('远端操作恢复依据不完整')
            checked = inspect_cached_file(cache / (identifier + '.bin'), evidence, asset.identity, volume)
            if not checked['valid']:
                raise ValueError(checked['reason'])
            valid.append(value)
        except (OSError, ValueError, TypeError, KeyError, BackupError):
            rejected.append({'receipt': path.name, 'reason': '清单或完整缓存核验失败，原文件保留'})
    for path in sorted(cache.glob('*.bin')):
        if not is_appledouble(path) and path.stem not in known:
            rejected.append({'cache': path.name, 'reason': '缺少有效恢复清单，不按同名或大小接管'})
    return volume, valid, rejected


def recover(cache, output, settings_path=None, *, export=False):
    """Create a new export/recovery directory; never replace the original data."""
    cache, output = Path(cache), Path(output)
    if output.exists() or output.is_symlink() or cache.resolve() in output.resolve().parents:
        raise BackupError('恢复输出须为缓存目录之外的新目录')
    volume, receipts, rejected = inspect_receipts(cache)
    if not receipts:
        raise BackupError('没有通过恢复清单核验的完整文件；请保留原缓存和数据库备份')
    bindings = {json.dumps(r['binding'], sort_keys=True) for r in receipts}
    destinations = {r['destination'] for r in receipts}
    if len(destinations) != 1 or len({r['binding']['connection'] for r in receipts}) != 1:
        raise BackupError('恢复清单的目的地不一致，请先核对；未接管任何记录')
    output.mkdir(parents=True)
    result = {'created_at': now(), 'mode': 'export' if export else 'rebuild',
              'cache': str(cache), 'recovered': [], 'rejected': rejected,
              'sync_enabled': False, 'delete_source_after_import': False}
    if export:
        occupied = set()
        for receipt in receipts:
            asset = Asset.from_dict(receipt['asset'])
            base = PurePosixPath(asset.device_directory) / PurePosixPath(asset.path).name
            candidate, number = base, 0
            while str(candidate).casefold() in occupied:
                number += 1
                candidate = base.with_name(f'{base.stem}_{number}{base.suffix}')
            occupied.add(str(candidate).casefold())
            source = cache / (receipt['file_id'] + '.bin')
            target = output / str(candidate)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open('rb') as incoming, target.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            stamp = receipt['cache_identity']['mtime_ns']
            os.utime(target, ns=(stamp, stamp))
            if not all(inspect_cached_file(p, receipt['cache_identity'], asset.identity, volume)['valid'] for p in (source, target)):
                raise BackupError('导出期间缓存发生变化；输出未标记完成，原缓存保留')
            fsync_directory(target.parent)
            result['recovered'].append({'id': receipt['file_id'], 'path': str(candidate)})
    else:
        store = Store.create(output / 'data')
        try:
            store.set_meta('cache_identity', volume)
            store.set_meta('destination_binding', json.loads(next(iter(bindings))))
            with store.db:
                for receipt in receipts:
                    # Remote completion is checked again by the normal publish
                    # recovery path, even when the receipt says "uploaded".
                    state = 'publishing' if receipt['state'] in ('staged', 'publishing', 'uploaded') else 'cached'
                    asset = Asset.from_dict(receipt['asset'])
                    store.db.execute('''INSERT INTO files
                        (id,destination,asset_id,asset,state,remote_path,temp_path,remote_identity,cache_identity,
                         cleanup,source_deleted_at,source_absent_at,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (receipt['file_id'], receipt['destination'], asset.identity, json.dumps(receipt['asset']), state,
                         receipt.get('remote_path'), receipt.get('temp_path'), json.dumps(receipt.get('remote_identity')),
                         json.dumps(receipt['cache_identity']),
                         'deleted' if receipt.get('source_deleted_at') else 'absent' if receipt.get('source_absent_at') else 'pending',
                         receipt.get('source_deleted_at'), receipt.get('source_absent_at'), receipt['created_at'], now()))
                    store.db.execute('INSERT INTO cache_receipts VALUES (?,?)', (receipt['file_id'], receipt['created_at']))
                    result['recovered'].append({'id': receipt['file_id'], 'state': state, 'path': asset.path})
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            store.close()
        values = json.loads(Path(settings_path).read_text()) if settings_path and Path(settings_path).is_file() else json.loads(json.dumps(DEFAULTS))
        values = runtime_config(values)
        values['backup'].update(enabled=False, delete_source_after_import=False, stop_after_current=False)
        if settings_path:
            from .credentials import copy_credentials
            copy_credentials(Path(settings_path).parent, output / 'config')
        atomic_write(output / 'config/settings.json', json.dumps(values, ensure_ascii=False, indent=2) + '\n')
        result['activation'] = '停止原 Compose 后使用新 data/config 目录，缓存仍挂载原目录；原 .env 凭据不变。'
    atomic_write(output / 'recovery-report.json', json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def snapshot_metadata(store, stop=None, *, interval=300, keep=3):
    """Rolling consistent SQLite snapshots; no media copies and no .env secrets."""
    previous = store.get_meta('metadata_backup', {})
    reset = store.get_meta('metadata_backup_reset', False)
    if not reset and time.time() - previous.get('saved_timestamp', 0) < interval:
        return False
    folder = store.directory / 'recovery'
    if folder.is_symlink():
        raise BackupError('元数据快照目录不能是符号链接')
    folder.mkdir(exist_ok=True)
    token = str(time.time_ns()) + '-' + uuid.uuid4().hex[:8]
    temporary, final = folder / ('.snapshot-' + token), folder / ('state-' + token + '.sqlite3')
    started = time.monotonic()
    def progress(*_):
        if stop and stop.is_set() or time.monotonic() - started > 60:
            raise Cancelled('元数据快照已中止，现有快照保留')
    try:
        with closing(sqlite3.connect(temporary)) as target:
            store.db.backup(target, pages=128, progress=progress, sleep=.05)
            if target.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise BackupError('元数据快照未通过检查')
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, final)
        fsync_directory(folder)
        store.set_meta('metadata_backup', {'saved_timestamp': time.time(), 'saved_at': now(), 'path': str(final), 'error': None})
        owned = sorted(p for p in folder.iterdir() if re.fullmatch(r'state-\d+-[0-9a-f]{8}\.sqlite3', p.name) and not p.is_symlink())
        for path in owned[:-(1 if reset else keep)]:
            unlink_cache(path)
        if reset:
            with store.db:
                store.db.execute("DELETE FROM meta WHERE key='metadata_backup_reset' AND value=?", (json.dumps(reset),))
        return True
    finally:
        temporary.unlink(missing_ok=True)
