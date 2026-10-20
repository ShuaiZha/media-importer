"""Checked archive changes; publish JSON intent only after connection verification."""
import json
import uuid

from .adapters import create_destination, destination_identity
from .config import Config, ConfigError, preferences, runtime_config
from .destination_binding import assert_settled, bind, read as binding, verify_connection
from .model import BackupError
from .settings import Conflict, atomic_write
from .store import Store

FIELDS = ('host', 'port', 'share', 'directory')
EXTRA_FIELDS = ('name', 'username', 'domain', 'credential_id')


def connection_revision(values):
    from .settings import revision
    return revision(json.dumps(values['destination'], sort_keys=True))


def candidate(values, fields, config_directory=None):
    if (not isinstance(fields, dict) or not set(FIELDS).issubset(fields)
            or set(fields) - set(FIELDS) - set(EXTRA_FIELDS)):
        raise ConfigError('归档位置必须包含地址、端口、共享名和子目录')
    for key in set(fields) - {'port'}:
        if (not isinstance(fields[key], str) or len(fields[key]) > 1024 or
                any(ord(c) < 32 for c in fields[key])):
            raise ConfigError('归档位置格式无效')
    destination = {**values['destination'], **fields}
    if fields.get('credential_id'):
        from .credentials import credential_path, read
        path = credential_path(config_directory, fields['credential_id'])
        read(path)
        destination['credential_file'] = str(path)
    destination.pop('credential_id', None)
    destination.pop('archive_id', None)
    return Config.from_values({**values, 'destination': destination})


def _stopped(store, item):
    if item['config']['backup']['enabled']:
        raise ConfigError('请先停止同步，待两路传输结束后再修改连接设置')
    state = store.get_meta('status', {})
    if (state.get('source_work') or
            any(s.get('phase') in ('downloading', 'uploading', 'publishing', 'verifying', 'scanning')
                for s in state.get('stages', {}).values())):
        raise ConfigError('传输尚未停止，请稍后重新检查归档位置')


def check(settings, fields, mode, expected, base, *, save=False, change_id=None):
    """Check before preview and again at confirmation. No old work is rerouted."""
    if mode not in ('connection', 'new'):
        raise ConfigError('请选择修改原连接或切换到新位置')
    if save:
        try:
            uuid.UUID(hex=change_id)
        except (ValueError, AttributeError, TypeError):
            raise ConfigError('归档位置确认无效，请重新检查') from None
    item = settings.read()
    if item['error']:
        raise ConfigError(item['error'])
    # A crash after the atomic save but before the reply must not turn success
    # into a failed mutation or allocate a second archive.
    saved = item['config'].get('archive', {})
    if (save and saved.get('change_id') == change_id and
            all(saved.get(k) == v for k, v in fields.items())):
        return item
    if item['revision'] != expected or connection_revision(item['config']) != base:
        raise Conflict('设置已经变化，请重新打开归档位置卡片')
    config = candidate(item['config'], fields, settings.path.parent)
    from .deployment import cache_path
    if not (settings.directory / 'state.sqlite3').is_file() or not cache_path(settings.directory).is_dir():
        raise ConfigError('本机任务或缓存目录不可访问，请恢复原目录后再修改归档位置')
    store = Store(settings.directory)
    remote = create_destination(config)
    try:
        _stopped(store, item)
        bind(store, Config.from_values(item['config']))
        current = binding(store.db)
        if mode == 'new':
            assert_settled(store)
            if destination_identity(config.destination) == destination_identity(item['config']['destination']):
                raise ConfigError('新位置与当前位置相同；无需切换')
        else:
            verify_connection(store, config, remote, accept=False, require_proof=True)
        remote.check_write()
        if not save:
            return {key: (fields[key] if key == 'credential_id' else config.destination[key]) for key in fields}
        # Recheck after network IO so a start, another form, or a local edit wins
        # over an old confirmation. The worker adopts the durable JSON intent.
        with settings.lock():
            latest = settings.read()
            if (latest['error'] or latest['revision'] != expected or
                    connection_revision(latest['config']) != base):
                raise Conflict('设置已经变化，请重新检查归档位置')
            _stopped(store, latest)
            if binding(store.db) != current:
                raise Conflict('存储身份已更新，请重新检查归档位置')
            if mode == 'new':
                assert_settled(store)
            archive_id = ('archive:' + change_id if mode == 'new' else
                          current['id'] if current else Config.from_values(latest['config']).destination_id)
            values = preferences(latest['config'])
            values['archive'] = {**values.get('archive', {}), **fields,
                                 **{k: config.destination[k] for k in FIELDS},
                                 'id': archive_id, 'change_id': change_id}
            atomic_write(settings.path, json.dumps(runtime_config(values), ensure_ascii=False, indent=2) + '\n')
            return settings.read()
    except BackupError as exc:
        raise ConfigError(remote.redact(str(exc)) if hasattr(remote, 'redact') else str(exc)) from None
    finally:
        remote.close()
        store.close()
