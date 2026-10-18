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


def notification_adapter(kind):
    return _module(NOTIFICATIONS, kind, "通知")
