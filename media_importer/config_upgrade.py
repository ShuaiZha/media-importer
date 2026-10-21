"""One-time v4 migration. v5 runtime never overlays business fields from .env."""
import copy
import json
from pathlib import Path

from .deployment import environment, text, number
from .model import ConfigError

def connections(env):
    from .adapters import source_defaults
    source = source_defaults(text(env, "SOURCE_TYPE", source_defaults()['type']))
    return {
        "source": {
            "type": source["type"],
            "url": text(env, "SOURCE_URL", source["url"]),
            "serial": text(env, "SOURCE_SERIAL", source["serial"]),
            "timeout_seconds": number(env, "SOURCE_TIMEOUT_SECONDS", source["timeout_seconds"]),
            "transport": text(env, "SOURCE_TRANSPORT", source["transport"]),
            "request_attempts": number(env, "SOURCE_REQUEST_ATTEMPTS", source["request_attempts"], integer=True),
        },
        "destination": {
            "type": text(env, "DESTINATION_TYPE", "smb"),
            "name": text(env, "DESTINATION_NAME", "SMB 存储"),
            "host": text(env, "DESTINATION_HOST", required=True),
            "port": number(env, "DESTINATION_PORT", 445, integer=True),
            "share": text(env, "DESTINATION_SHARE", required=True),
            "directory": text(env, "DESTINATION_DIRECTORY", "media"),
            "username": text(env, "DESTINATION_USERNAME", required=True),
            "domain": text(env, "DESTINATION_DOMAIN", "WORKGROUP"),
        },
    }



def convert(raw, directory, env=None):
    """Return the effective old values and an optional credential; no filesystem writes."""
    from .config import DEFAULTS, Config, preferences
    from .credentials import read, credential_path
    env = environment() if env is None else env
    if raw.get('version') != 4:
        raise ConfigError('仅支持从 version: 4 迁移')
    allowed = (set(DEFAULTS) - {'preview'}) | {'archive', 'source_options'}
    if set(raw) - allowed:
        raise ConfigError('旧配置含未知字段，未执行迁移')
    values = {**copy.deepcopy(DEFAULTS), **raw, **connections(env), 'version': 5}
    old_archive = raw.get('archive', {})
    if not isinstance(old_archive, dict) or set(old_archive) - {
            'host','port','share','directory','id','change_id','name','username','domain','credential_id'}:
        raise ConfigError('旧归档设置无效，未执行迁移')
    if not isinstance(raw.get('source_options', {}), dict) or set(raw.get('source_options', {})) - {
            'url','serial','timeout_seconds','request_attempts'}:
        raise ConfigError('旧相机设置无效，未执行迁移')
    values['destination'].update({k: v for k, v in old_archive.items() if k in
        ('type', 'host', 'port', 'share', 'directory', 'name', 'username', 'domain')})
    if old_archive.get('id'):
        values['destination']['archive_id'] = old_archive['id']
    if old_archive.get('credential_id'):
        values['destination']['credential_file'] = str(credential_path(directory, old_archive['credential_id']))
    values['source'].update(raw.get('source_options', {}))
    # The old runtime gave an explicitly saved choice priority over the .env hint.
    values['source']['transport'] = raw.get('backup', {}).get('source_transport', values['source']['transport'])
    values['backup'] = {**DEFAULTS['backup'], **raw.get('backup', {}), 'source_transport': values['source']['transport']}
    values['preview'] = {'public_url': text(env, 'PREVIEW_PUBLIC_URL', '')}
    values.pop('archive', None); values.pop('source_options', None)
    checked = Config.from_values(values)
    document = preferences(checked.values)
    if old_archive.get('change_id'):
        document['archive']['change_id'] = old_archive['change_id']
    if old_archive.get('credential_id'):
        read(credential_path(directory, old_archive['credential_id']))
        credential = None
    else:
        credential = (document['archive']['credential_id'], text(env, 'DESTINATION_PASSWORD', required=True))
    Config.from_dict(document, directory)
    return document, credential


def prepare(path, data_directory=None):
    """Serialize migration between workers; a failed conversion leaves v4 untouched."""
    from .settings import Settings, atomic_write
    from .config import Config, _unique_object
    from .credentials import stage
    path = Path(path)
    with Settings(path).lock():
        old = path.read_text(encoding='utf-8')
        raw = json.loads(old, object_pairs_hook=_unique_object)
        if raw.get('version') == 5:
            Config.from_dict(raw, path.parent)
            return False
        document, credential = convert(raw, path.parent)
        if not raw.get('archive', {}).get('id'):
            # An old installation may already have a durable logical archive ID.
            # Keep that ID rather than treating canonicalization as a new target.
            from .source_connection import read as read_meta
            import os
            directory = data_directory or os.environ.get('MEDIA_IMPORTER_DATA_DIR', '/data')
            binding = read_meta(directory, 'destination_binding')
            if binding.get('id'):
                document['archive']['id'] = binding['id']

        backup = path.with_name(path.name + '.v4-backup')
        # Exclusive backup: never replace the original rollback evidence.
        if not backup.exists():
            atomic_write(backup, old)
        if credential:
            stage(path.parent, *credential)
        atomic_write(path, json.dumps(document, ensure_ascii=False, indent=2) + '\n')
        return True
