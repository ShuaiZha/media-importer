"""Small, explicit adapter registry. The transfer engine only consumes the returned objects."""
from importlib import import_module

SOURCES = {"gopro": ".gopro"}
DESTINATIONS = {"smb": ".smb"}
NOTIFICATIONS = {"feishu": ".feishu"}


def _module(registry, kind, label):
    from ..config import ConfigError
    if not isinstance(kind, str) or kind not in registry:
        raise ConfigError(f"不支持的{label}类型: {kind!r}")
    return import_module(registry[kind], __name__)


def source_defaults(kind=None):
    return _module(SOURCES, next(iter(SOURCES)) if kind is None else kind, "来源").defaults()


def destination_defaults(kind=None):
    return _module(DESTINATIONS, next(iter(DESTINATIONS)) if kind is None else kind, "目的地").defaults()


def validate_source(values):
    _module(SOURCES, values["type"], "来源").validate(values)


def validate_destination(values):
    _module(DESTINATIONS, values["type"], "目的地").validate(values)


def destination_identity(values):
    return _module(DESTINATIONS, values["type"], "目的地").identity(values)


def create_source(config, stop=None):
    return _module(SOURCES, config.source["type"], "来源").create(config, stop)


def create_destination(config, stop=None):
    return _module(DESTINATIONS, config.destination["type"], "目的地").create(config, stop)


def probe_source(kind, url):
    return _module(SOURCES, kind, "来源").probe(url)


def discover_source(kind, hints, stop, *, scan=False):
    return _module(SOURCES, kind, "来源").discover(hints, stop, scan=scan)


def source_media_category(asset):
    if asset.source_type not in SOURCES:
        return None
    classify = getattr(_module(SOURCES, asset.source_type, "来源"), "media_category", None)
    return classify(asset) if classify else None


def source_selection_groups(source, assets):
    """Optional, file-level selection; never fall back to deletion boundaries."""
    choose = getattr(source, 'selection_groups', None)
    if choose:
        return choose(assets)
    return [{'id': asset.identity, 'members': [asset], 'preview_ids': [],
             'safe': False, 'reason': '未提供文件关联信息'}
            for asset in assets]


def notification_adapter(kind):
    return _module(NOTIFICATIONS, kind, "通知")


def source_link(kind, action, serial, stop=None):
    module = _module(SOURCES, kind, "来源")
    method = getattr(module, 'link', None)
    if method is None:
        from ..model import SourceConnectionError
        raise SourceConnectionError('此适配器尚未提供无线连接')
    return method(action, serial, stop)


def source_controls(device):
    if device.get('type') not in SOURCES:
        return {'commands': [], 'reason': '尚未识别可控制的相机'}
    method = getattr(_module(SOURCES, device['type'], '来源'), 'controls', None)
    return method(device) if method else {'commands': [], 'reason': '此适配器尚未提供拍摄控制'}


def source_preview_support(device):
    if device.get('type') in SOURCES:
        method = getattr(_module(SOURCES, device['type'], '来源'), 'preview_support', None)
        if method:
            return method(device)
    return {'supported': False, 'reason': '此设备尚未适配实时预览'}
