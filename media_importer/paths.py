from pathlib import PurePosixPath

from .model import ConfigError


def archive_location(destination, relative=""):
    """Display the exact storage address without credentials or network access."""
    host = destination.get('host', '')
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    port = destination.get('port', 445)
    root = PurePosixPath(destination.get('share', '')) / destination.get('directory', '')
    return f"smb://{host}:{port}/" + str(root / relative)


def safe_relative(value, allow_empty=False):
    if not value and allow_empty:
        return ""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ConfigError("路径必须是相对路径，不能包含 ..、反斜杠或冒号")
    if any(ord(c) < 32 for c in value):
        raise ConfigError("路径不能包含控制字符")
    return str(path)
