"""Live network preferences; the Wi-Fi password never enters JSON jobs or cards."""
import json

from .model import ConfigError


def validate(value):
    from .credentials import credential_path
    if not isinstance(value, dict) or set(value) != {'ssid', 'credential_id', 'port'}:
        raise ConfigError('直播网络配置格式无效')
    ssid = value['ssid']
    if not isinstance(ssid, str) or len(ssid.encode('utf-8')) > 32 or any(ord(c) < 32 for c in ssid):
        raise ConfigError('Wi-Fi 名称不能超过 32 字节或包含控制字符')
    if type(value['port']) is not int or not 1024 <= value['port'] <= 65535:
        raise ConfigError('RTMP 端口须为 1024–65535 的整数')
    if value['credential_id']:
        credential_path('.', value['credential_id'])
    return value


def password(settings, value):
    from .credentials import credential_path, read
    try:
        return read(credential_path(settings.path.parent, value['credential_id']))
    except ConfigError:
        raise ConfigError('直播 Wi-Fi 密码不可用，请在「相机与预览 → 直播网络」重新填写') from None


def draft(settings, fields, identifier, expected):
    """Handle secrets synchronously before persisting a control request."""
    from .credentials import stage
    from .settings import Conflict
    secret = fields.pop('password')
    with settings.lock():
        item = settings.read()
        if item['error'] or item['revision'] != expected:
            raise Conflict('设置已变化，请重新打开直播网络')
        old = item['config']['livestream']
        value = validate({**fields, 'credential_id': old['credential_id']})
        if not value['ssid']:
            raise ConfigError('请填写直播 Wi-Fi 名称')
        if secret:
            if not 8 <= len(secret.encode('utf-8')) <= 63:
                raise ConfigError('Wi-Fi 密码须为 8–63 字节')
            value['credential_id'] = stage(settings.path.parent, identifier, secret)
        elif value['ssid'] != old['ssid']:
            raise ConfigError('更换 Wi-Fi 时请重新填写密码')
        password(settings, value)
        return value


def save(settings, fields, expected):
    from .config import Config, preferences
    from .settings import Conflict, atomic_write
    from .store import Store
    from .live_preview import active
    fields = validate(fields)
    if not fields['ssid']:
        raise ConfigError('请填写直播 Wi-Fi 名称')
    password(settings, fields)
    with settings.lock():
        item = settings.read()
        if item['error'] or expected is not None and item['revision'] != expected:
            raise Conflict('设置已变化，请重新打开直播网络')
        store = Store(settings.directory)
        try:
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                if active(store):
                    raise ConfigError('请先结束预览或直播，确认停止后再修改网络')
                values = preferences(item['config'])
                values['livestream'] = fields
                Config.from_dict(values, settings.path.parent)
                atomic_write(settings.path, json.dumps(values, ensure_ascii=False, indent=2) + '\n')
        finally:
            store.close()
    return settings.read()
