"""Small file bridge: the Mac owns radios; Docker owns transfer decisions.

No credentials or commands cross this bridge. A stale lease cannot wake a camera.
"""
import json
import os
import time
import uuid
from pathlib import Path


def write_json(path, value):
    path = Path(path)
    # Only create the bridge inside an existing data directory. Never recreate
    # a missing external-disk mount or its data directory on the internal disk.
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        with open(path) as source:
            value = json.loads(source.read(16384))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def publish(directory, config, enabled, status):
    source = config.source if config else {}
    active = bool(enabled and source.get('transport') == 'wifi')
    write_json(Path(directory) / 'wireless/control.json', {
        'serial': source.get('serial'), 'url': source.get('wireless_url'),
        'enabled': active, 'needs_camera': active and (bool(status.get('source_work')) or
                            not status.get('inventory', {}).get('verified', False)),
        'reading': active and bool(status.get('source_work')), 'expires_at': time.time() + 8,
    })


def demand(directory, serial, url):
    value = read_json(Path(directory) / 'wireless/control.json')
    expiry = value.get('expires_at', 0)
    if (type(expiry) not in (int, float) or not time.time() < expiry <= time.time() + 15
            or value.get('serial') != serial or value.get('url') != url):
        return {}
    return value


def connection_status(directory, serial):
    value = read_json(Path(directory) / 'wireless/status.json')
    if (value.get('serial') != serial or type(value.get('updated_at')) not in (int, float)
            or not 0 <= time.time() - value['updated_at'] < 15):
        return {'state': 'unavailable', 'message': 'Mac 蓝牙助手未运行或状态已过期；已手动连接相机 Wi-Fi 时仍可导入'}
    return {key: value[key] for key in ('state', 'message', 'attempt', 'updated_at', 'via') if key in value}
