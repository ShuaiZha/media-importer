"""A frozen, one-time selection on the normal two-lane transfer engine.

The caller persists the manifest and owns its cancellation/retry lifecycle.
This module never changes preferences, expands the selection, or deletes source
media. Existing tasks outside the manifest are not scheduled or reconciled.
"""
import json
import threading
import time
import uuid

from .model import Asset, BackupError, ConfigError, SourceError, now
from .service import Service
from .storage import initialize_cache, require_cache
from .task_facts import file_facts


class SelectedService(Service):
    process_camera_operations = False
    tracks_source_inventory = False

    def __init__(self, *args, selection, **kwargs):
        super().__init__(*args, **kwargs)
        self.selection = selection
        if not isinstance(selection.get('id'), str) or not selection['id']:
            raise ConfigError('选定导入缺少任务标识')
        if type(selection.get('archive_previews')) is not bool:
            raise ConfigError('选定导入缺少预览文件归档策略')
        # Already-confirmed requests keep their original frozen group scope.
        self.selection_mode = selection.get('selection_mode', 'groups')
        if self.selection_mode not in ('groups', 'files'):
            raise ConfigError('选定导入范围类型无效')
        self.selected_groups = []
        self.assets = {}
        self.preview_ids = set()
        for frozen in selection.get('groups', []):
            group = {**frozen, 'members': [Asset.from_dict(a) for a in frozen['members']]}
            if not group['members'] or not isinstance(group.get('id'), str):
                raise ConfigError('选定素材组无效')
            for asset in group['members']:
                if (asset.source_type, asset.serial) != (self.config.source['type'], self.config.source['serial']):
                    raise ConfigError('选定素材不属于当前相机')
                if asset.identity in self.assets:
                    raise ConfigError('选定素材组存在重复成员')
                self.assets[asset.identity] = asset
            previews = set(group.get('preview_ids', []))
            if not previews <= {a.identity for a in group['members']}:
                raise ConfigError('预览文件不属于选定素材组')
            self.preview_ids.update(previews)
            self.selected_groups.append(group)
        if not self.assets:
            raise ConfigError('尚未选择素材')
        self.archive_ids = set(self.assets)
        if not selection['archive_previews']:
            self.archive_ids -= self.preview_ids
        self.file_ids = {self.store.file_id(self.destination_id, a) for a in self.assets.values()}
        self.run_id = uuid.uuid5(uuid.NAMESPACE_URL, self.destination_id + ':selected:' + selection['id']).hex
        self.batch_id = None
        self.archive_checks = {}
        self.archive_checks_lock = threading.RLock()

    def transfer_task(self, store, source, destination, stage):
        task = SelectedService(self.config, store, source, destination, self.stop, self.control,
                               stage=stage, selection=self.selection)
        task.archive_checks = self.archive_checks
        task.archive_checks_lock = self.archive_checks_lock
        return task

    def allowed(self, policy=None):
        if policy is not None:
            return False  # Explicit selection never grants source deletion.
        if self.control:
            return self.control.allowed()
        return not self.stop.is_set() and not self.config.backup['enabled']

    def rows(self, *, work=False):
        verified = self._verified()
        return [row for row in super().rows(work=False)
                if row['asset_id'] in self.assets and
                (row['asset_id'] in self.archive_ids or row['state'] == 'excluded') and
                (not work or row['state'] not in ('uploaded', 'excluded') or
                 row['state'] == 'uploaded' and
                 (not row.get('cache_cleared_at') or row['id'] not in verified))]

    def _verified(self):
        with self.archive_checks_lock:
            return {key for key, value in self.archive_checks.items() if value.get('verified')}

    def _publish_checks(self):
        # One small write per completed verification/upload, never per chunk.
        with self.archive_checks_lock:
            self.store.set_meta('selected_archive_checks', {
                'id': self.selection['id'], 'verified_file_ids': sorted(self._verified()),
                'errors': {key: value['error'] for key, value in self.archive_checks.items()
                           if not value.get('verified') and value.get('error')}})

    def _record_check(self, identifier, result):
        with self.archive_checks_lock:
            self.archive_checks[identifier] = result
            self._publish_checks()

    def verify_archived(self, row):
        """Check recorded identity, not just filename or size; never redownload."""
        if row['id'] in self._verified():
            return True
        try:
            self.verify_destination()
            if (not row.get('remote_path') or not row.get('remote_identity') or
                    not self.confirmed_final(row, self.destination.stat(row['remote_path']))):
                message = '既有归档缺失或身份已变化，请检查目的地；未覆盖文件或重新上传'
            else:
                self._record_check(row['id'], {'verified': True})
                return True
        except (BackupError, OSError, ValueError) as exc:
            message = self.destination.redact(str(exc)) if hasattr(self.destination, 'redact') else str(exc)
        self._record_check(row['id'], {'verified': False, 'error': message})
        return False

    def commit_uploaded(self, row):
        super().commit_uploaded(row)
        # The normal publish path has just checked the formal file's identity
        # and size. Share this evidence with the coordinator for this cycle.
        self._record_check(row['id'], {'verified': True})

    def clean_temporaries(self, kind=None, *, file_ids=None):
        selected = {row['id'] for row in self.rows()}
        if file_ids is not None:
            selected &= set(file_ids)
        super().clean_temporaries(kind, file_ids=selected)

    def cleanup_groups(self, *args, **kwargs):
        return  # Manual cleanup has its own separately confirmed operation.

    def settle(self):
        """Retire only this confirmed scope after its result has been saved."""
        from .lifecycle import retirement_ready, retire_rows
        require_cache(self.store)
        rows = self.rows()
        if not rows or not self.result()['complete']:
            return 0
        blocked = {item['file_id'] for item in self.store.temporary_items(self.destination_id)}
        if not all(retirement_ready(self.store, row, blocked=blocked) for row in rows):
            return 0
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            retire_rows(self.store, self.destination_id, rows)
        return len(rows)

    def initialize_selection(self):
        """Create only frozen members, preserving known tasks and compact dedupe."""
        from .lifecycle import restore_member
        with self.store.db:
            for asset in self.assets.values():
                existing = self.store.db.execute(
                    'SELECT * FROM source_index WHERE destination=? AND asset_id=?',
                    (self.destination_id, asset.identity)).fetchone()
                if existing and (existing['source_type'], existing['serial']) == (
                        asset.source_type, asset.serial):
                    restore_member(self.store, self.destination_id, asset, dict(existing))
        self.batch_id = self.store.batch(self.destination_id, list(self.assets.values()),
                                        preview_ids=self.preview_ids,
                                        archive_previews=self.selection['archive_previews'], reporting=False)
        if self.selection_mode == 'groups':
            self.store.save_groups(self.destination_id, self.selected_groups)
        # File choices are not deletion boundaries. Leave existing group records
        # untouched; full sync and manual cleanup reconstruct complete groups
        # from the live source and verify every required member before deletion.
        # An earlier round may have deliberately excluded a preview. Explicitly
        # including previews in this new selection makes only those members work.
        if self.selection['archive_previews']:
            for row in self.rows():
                if row['asset_id'] in self.preview_ids and row['state'] == 'excluded':
                    self.store.update(row['id'], state='pending', error=None, error_stage=None)
        rows = self.rows()
        with self.store.db:
            self.store.db.execute('''INSERT OR IGNORE INTO transfer_runs
                (id,destination,source_type,serial,selection,created_at,archive_previews)
                VALUES (?,?,?,?,?,?,?)''',
                (self.run_id, self.destination_id, self.config.source['type'], self.config.source['serial'],
                 'selected:' + self.selection['id'], now(), int(self.selection['archive_previews'])))
            self.store.db.executemany('INSERT OR IGNORE INTO transfer_run_files VALUES (?,?)',
                                     ((self.run_id, row['id']) for row in rows))
            self.store.db.execute('UPDATE transfer_runs SET finished_at=NULL WHERE id=?', (self.run_id,))
            from .completion import refresh
            refresh(self.store.db, [self.run_id])

    @staticmethod
    def _signature(group):
        return (group['id'], frozenset(a.identity for a in group['members']),
                frozenset(group.get('preview_ids', [])))

    def _validate_groups(self, fresh):
        from .adapters import source_selection_groups
        groups = (source_selection_groups(self.source, fresh) if self.selection_mode == 'files'
                  else self.source.groups(fresh))
        observed = {self._signature(group) for group in groups}
        return {group['id'] for group in self.selected_groups if self._signature(group) in observed}

    def prepare_imports(self):
        """A full observation is read-only; only the frozen members are eligible."""
        from .source_connection import require
        require(self.store, self.config)
        self.boundary()
        self.store.status(source_work=True)
        self.observe_source()
        self.report('scanning', '正在核对选定素材清单', current_file=None, current_file_id=None, issue=None)
        fresh = self.source_scan().assets
        valid = self._validate_groups(fresh)
        rows = {row['asset_id']: row for row in self.rows()}
        eligible = []
        for group in self.selected_groups:
            for asset in group['members']:
                if asset.identity not in self.archive_ids:
                    continue
                row = rows[asset.identity]
                if group['id'] in valid:
                    eligible.append(asset)
                    if row['state'] == 'missing':
                        self.store.update(row['id'], state='pending', error=None, error_stage=None)
                elif row['state'] in ('pending', 'missing'):
                    self.store.update(row['id'], state='missing',
                                      error='选定文件或附属关系已变化，保留原任务；请重新连接原相机或重新选择',
                                      error_stage='import')
        return {'assets': eligible, 'batch_id': self.batch_id, 'known_sources': set(),
                'selected_request': self.selection['id']}

    def import_asset(self, row):
        # Recheck the frozen file/companion scope before each download. Other
        # primary files do not affect new file-level selections.
        group = next(g for g in self.selected_groups
                     if row['asset_id'] in {a.identity for a in g['members']})
        if group['id'] not in self._validate_groups(self.source_scan().assets):
            raise SourceError('选定文件或附属关系已变化，本次未导入新增或变更的文件')
        super().import_asset(row)

    def verify_imports(self, inventory):
        self.boundary()
        require_cache(self.store)
        rows = {row['asset_id']: row for row in self.rows()}
        for key in self.archive_ids:
            row = rows.get(key)
            if row and (row['state'] == 'excluded' or row['state'] == 'uploaded' and row['id'] in self._verified()):
                continue
            if (not row or row['state'] not in ('cached', 'staged', 'publishing') or
                    not (self.cache_valid(row) or self.store.get(row['id'])['state'] == 'uploaded')):
                self.report('waiting_retry', '仍有选定素材未完整导入，其他完整缓存可继续上传',
                            current_file=None, current_file_id=None, current_bytes=0, current_total_bytes=0)
                return False
        # Each download verified its source identity before committing its cache.
        # No live camera is required to finish uploading those durable files.
        with self.store.db:
            self.store.db.execute('UPDATE transfer_runs SET imported_at=COALESCE(imported_at,?) WHERE id=?',
                                  (now(), self.run_id))
            from .completion import refresh
            refresh(self.store.db, [self.run_id])
        self.report('complete', '选定素材已完整导入，未选素材保留', current_file=None,
                    current_file_id=None, current_bytes=0, current_total_bytes=0, issue=None)
        return True

    def result(self):
        rows = self.rows()
        archives = [row for row in rows if row['state'] != 'excluded']
        with self.archive_checks_lock:
            verified = self._verified()
            checks = dict(self.archive_checks)
        outcomes = {row['id']: file_facts(row, archive_verified=row['id'] in verified,
                    cached=not (row['state'] == 'uploaded' and row['id'] in verified) and
                    row['state'] in ('cached', 'staged', 'publishing', 'uploaded') and self.cache_valid(row))
                    for row in archives}
        pending = [row for row in archives if not outcomes[row['id']]['archived']]
        cleanup = [row for row in archives
                   if row['state'] == 'uploaded' and not row.get('cache_cleared_at')]
        imported = [row for row in archives if outcomes[row['id']]['imported']]
        unresolved = [row for row in archives if outcomes[row['id']]['archive_pending']]
        errors = [{'file': self.store.asset(row).path, 'reason':
                   checks.get(row['id'], {}).get('error', '既有归档尚未核验')}
                  for row in unresolved]
        return {'complete': bool(rows) and not pending and not cleanup, 'run_id': self.run_id,
                'files': len(archives), 'bytes': sum(self.store.asset(row).size for row in archives),
                'uploaded_files': sum(outcomes[row['id']]['archived'] for row in archives),
                'imported_files': len(imported), 'import_verified': bool(rows) and len(imported) == len(archives),
                'uploaded_bytes': sum(self.store.asset(row).size for row in archives
                                      if outcomes[row['id']]['archived']),
                'imported_bytes': sum(self.store.asset(row).size for row in imported),
                'file_ids': [row['id'] for row in rows],
                'archive_verified_ids': sorted(verified), 'archive_verification_pending': len(unresolved),
                'archive_errors': errors,
                'excluded_preview_files': len(self.preview_ids - self.archive_ids),
                'cleanup_pending': len(cleanup), 'reason': 'archive_verification' if unresolved else 'pending_files' if pending else
                'cache_cleanup' if cleanup else None}

    def cycle(self):
        self.boundary()
        with self.archive_checks_lock:
            self.archive_checks.clear()
            self._publish_checks()
        if not self.store.get_meta('cache_identity'):
            initialize_cache(self.store)
        require_cache(self.store)
        self.initialize_selection()
        self.recover_interrupted()
        self.clean_temporaries()
        from .pipeline import transfer
        transfer(self)
        self.boundary()
        result = self.result()
        if not result['complete'] and self.control and self.control.draining():
            self.control.finish_stop()
            self.control.boundary()
        if result['complete']:
            with self.store.db:
                self.store.db.execute('UPDATE transfer_runs SET finished_at=COALESCE(finished_at,?) WHERE id=?',
                                      (now(), self.run_id))
                from .completion import refresh
                refresh(self.store.db, [self.run_id])
            self.report('completed', '选定素材归档完成，相机原片保留', current_file=None,
                        current_file_id=None, current_bytes=0, current_total_bytes=0, issue=None)
        else:
            with self.store.db:
                self.store.db.execute('UPDATE transfer_runs SET finished_at=NULL WHERE id=?', (self.run_id,))
                from .completion import refresh
                refresh(self.store.db, [self.run_id])
        return result
