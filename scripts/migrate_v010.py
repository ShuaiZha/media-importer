#!/usr/bin/env python3
"""Offline v0.9 -> v0.10 migration. Run in the Docker environment with the original cache mounted."""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_importer.config import DEFAULTS, runtime_config
from media_importer.model import Asset
from media_importer.settings import atomic_write
from media_importer.storage import file_identity, inspect_cache
from media_importer.store import Store, worker_lock


def migrate(config_path, directory, backup=None):
    config_path, directory = Path(config_path), Path(directory)
    raw = json.loads(config_path.read_text())
    if raw.get('version') not in (3, 4):
        raise RuntimeError('只支持从 v0.9 的 settings version 3 迁移')
    target = json.loads(json.dumps(DEFAULTS))
    for group, defaults in target.items():
        if isinstance(defaults, dict):
            defaults.update({k:v for k,v in raw.get(group,{}).items() if k in defaults})
    target['backup'].update(enabled=False, delete_source_after_import=False)
    target = runtime_config(target)
    with worker_lock(directory) as acquired:
        if not acquired:
            raise RuntimeError('请先停止 Compose 两个服务')
        db = sqlite3.connect(directory/'state.sqlite3'); db.row_factory=sqlite3.Row
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (4, 5):
                raise RuntimeError('仅支持数据库版本 4 -> 5')
            if version == 5 and raw['version'] == 4:
                print(json.dumps({'already_migrated':True}));return
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('数据库完整性检查失败，未迁移')
            backup=Path(backup) if backup else directory/('pre-v0.10.0-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
            backup.mkdir(parents=True, mode=0o700, exist_ok=False)
            shutil.copy2(config_path,backup/'settings.json')
            with sqlite3.connect(backup/'state.sqlite3') as copy:
                db.backup(copy)
            env_path=os.environ.get('MEDIA_IMPORTER_ENV_FILE')
            if env_path and Path(env_path).is_file():
                shutil.copy2(env_path,backup/'deployment.env');(backup/'deployment.env').chmod(0o600)
            if version == 4:
                old_rows=[dict(r) for r in db.execute('SELECT * FROM files')]
                cache_id=db.execute("SELECT value FROM meta WHERE key='cache_identity'").fetchone()
                binding=json.loads(cache_id[0]) if cache_id else None
                health=inspect_cache(directory/'cache',binding)
                if not health['available'] and any(r['state'] in ('cached','staged','publishing') for r in old_rows):
                    raise RuntimeError('请连接原缓存盘后重新运行迁移；尚未修改数据库')
                # Do not bless a fallback folder as the source of a full cache identity.
                with db:
                    db.execute('ALTER TABLE files RENAME COLUMN upload_identity TO remote_identity')
                    for field in ('cache_identity','group_id','source_cleanup_error'):
                        db.execute('ALTER TABLE files ADD COLUMN '+field+' TEXT')
                    db.execute('ALTER TABLE files DROP COLUMN upload_offset')
                    db.execute('ALTER TABLE files DROP COLUMN upload_source')
                    db.execute('ALTER TABLE batches ADD COLUMN imported_at TEXT')
                    db.execute('''CREATE TABLE temporary_cleanup (
                        id TEXT PRIMARY KEY,file_id TEXT NOT NULL,destination TEXT NOT NULL,
                        kind TEXT NOT NULL,path TEXT NOT NULL,identity TEXT,
                        state TEXT NOT NULL DEFAULT 'pending',error TEXT,created_at TEXT NOT NULL,
                        UNIQUE(kind,destination,path))''')
                    for row in old_rows:
                        asset=Asset.from_dict(json.loads(row['asset']))
                        cache=directory/'cache'/(row['id']+'.bin')
                        if health['available'] and cache.exists() and row['state'] in ('cached','staged','publishing','uploaded'):
                            identity=file_identity(cache)
                            if identity['size']==asset.size:
                                db.execute('UPDATE files SET cache_identity=? WHERE id=?',(json.dumps(identity),row['id']))
                        partial=cache.with_suffix('.download')
                        # Register known names even if the original volume is offline.
                        db.execute('INSERT OR IGNORE INTO temporary_cleanup (id,file_id,destination,kind,path,created_at) VALUES (?,?,?,?,?,?)',
                                   (uuid.uuid4().hex,row['id'],row['destination'],'local',partial.name,row['updated_at']))
                        if row['temp_path'] and row['state'] not in ('staged','publishing','uploaded'):
                            # Old committed final-byte checkpoints represent a complete file, not a partial.
                            if row['upload_offset']==asset.size and row['upload_identity']:
                                db.execute("UPDATE files SET state='staged' WHERE id=?",(row['id'],))
                            else:
                                db.execute('''INSERT OR IGNORE INTO temporary_cleanup
                                    (id,file_id,destination,kind,path,identity,created_at) VALUES (?,?,?,?,?,?,?)''',
                                    (uuid.uuid4().hex,row['id'],row['destination'],'remote',row['temp_path'],row['upload_identity'],row['updated_at']))
                                db.execute('UPDATE files SET temp_path=NULL,remote_identity=NULL WHERE id=?',(row['id'],))
                    db.execute('DROP TABLE IF EXISTS requests')
                    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'bot_actions' in tables:db.execute('DELETE FROM bot_actions')
                    if 'bot_jobs' in tables:db.execute("UPDATE bot_jobs SET state='error',error='旧控制请求已失效，请重新发送' WHERE state!='done'")
                    if 'notification_events' in tables:db.execute("UPDATE notification_events SET state='cancelled' WHERE state='pending'")
                    db.execute("DELETE FROM meta WHERE key IN ('status','bot','notification_state','notification_delivery')")
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('service', ?)",(json.dumps({'running':False}),))
                    db.execute('PRAGMA user_version=5')
        finally:
            db.close()
        store=Store(directory)
        try:
            # Preserve adapter identities and assigned paths. Group membership is metadata only.
            from media_importer.adapters.gopro import GoProSource
            destinations={r['destination'] for r in store.files()}
            for destination in destinations:
                rows=store.files(destination=destination)
                serials={store.asset(r).serial for r in rows}
                for serial in serials:
                    assets=[store.asset(r) for r in rows if store.asset(r).serial==serial and store.asset(r).source_type=='gopro']
                    store.save_groups(destination,GoProSource.groups(assets))
        finally:
            store.close()
        atomic_write(config_path,json.dumps(target,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'migrated':True,'backup':str(backup),'sync_enabled':False,'delete_source':False},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default='/config/settings.json')
    parser.add_argument('--data',default='/data')
    parser.add_argument('--backup')
    args=parser.parse_args();migrate(args.config,args.data,args.backup)
