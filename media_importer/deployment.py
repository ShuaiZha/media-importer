"""Deployment-only values. Secrets never enter runtime JSON or status snapshots."""
import os
from pathlib import Path

from .model import ConfigError


def cache_path(directory):
    return Path(os.environ.get('MEDIA_IMPORTER_CACHE_DIR') or Path(directory) / 'cache')


def assert_runtime(directory):
    """A persistent choice survives stopped services and host restarts."""
    import json
    marker = Path(directory) / 'deployment.json'
    if marker.exists():
        mode = os.environ.get('MEDIA_IMPORTER_RUNTIME', 'docker')
        if json.loads(marker.read_text())['mode'] != mode:
            raise ConfigError('此数据目录已由另一种部署接管；先停止并卸载该部署，再切换运行方式')


def environment():
    path = os.environ.get("MEDIA_IMPORTER_ENV_FILE")
    if path:
        from dotenv import dotenv_values
        if not Path(path).is_file():
            raise ConfigError("无法读取 .env，请检查 MEDIA_IMPORTER_ENV_FILE 和只读挂载")
        # Passwords containing $ are literal; Compose only interpolates mount paths.
        return dotenv_values(path, interpolate=False)
    return os.environ


def text(values, key, default="", required=False):
    value = values.get(key, default)
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00") or (required and not value):
        raise ConfigError(f".env 中 {key} 缺失或格式无效")
    return value


def number(values, key, default, integer=False):
    try:
        return int(text(values, key, str(default))) if integer else float(text(values, key, str(default)))
    except ValueError:
        raise ConfigError(f".env 中 {key} 必须是{'整数' if integer else '数值'}") from None


def destination_password(destination):
    if destination and destination.get("credential_file"):
        from .credentials import read
        return read(destination["credential_file"])
    raise ConfigError("缺少本机 SMB 凭据引用，请通过飞书归档存储重新填写密码")


def notification_credentials():
    from .adapters.feishu import validate_credentials
    env = environment()
    return validate_credentials({
        "app_id": text(env, "FEISHU_APP_ID", required=True),
        "app_secret": text(env, "FEISHU_APP_SECRET", required=True),
        "receive_id_type": "open_id",
        "receive_id": text(env, "FEISHU_OWNER_OPEN_ID", required=True),
    })
