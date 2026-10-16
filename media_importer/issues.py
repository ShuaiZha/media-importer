"""Small, shared error vocabulary for the queues and bot status."""
from .model import CacheUnavailable, SourceBlocked, SourceError, SourceConnectionError, DestinationError


def describe(exc, stage=None):
    message = str(exc)
    lower = message.lower()
    code, action, scope = "file", "程序会重试此文件，其他可处理的文件继续。", "file"
    if isinstance(exc, CacheUnavailable) or isinstance(exc, OSError) and not isinstance(exc, DestinationError):
        code, action, scope = "cache_unavailable", "连接原缓存盘，检查挂载与读写权限后重试。", "cache"
    elif isinstance(exc, SourceConnectionError):
        code, action, scope = "source_network", "检查相机连接；无线模式请在 Mac 连接相机 Wi-Fi。程序会重试，完整缓存继续上传。", "source"
    elif isinstance(exc, SourceBlocked):
        code, action, scope = "source_state", "等待相机结束录像、恢复就绪或解除温度告警，已有完整缓存继续上传。", "source"
    elif isinstance(exc, DestinationError):
        if any(word in lower for word in ("不存在", "not found", "bad_network_name")):
            code, action, scope = "destination_path", "检查 目的地目录是否存在。", "destination"
        elif any(word in lower for word in ("logon_failure", "logon failure", "authentication", "password", "credential", "凭据", "认证")):
            code, action, scope = "authentication", "检查 目的地账号和 .env 凭据；修改后重新测试连接。", "destination"
        elif any(word in lower for word in ("access_denied", "access denied", "permission", "权限")):
            code, action, scope = "permission", "检查共享目录的访问、写入和改名权限。", "destination"
        elif any(word in lower for word in ("disk_full", "disk full", "quota", "no space", "空间不足")):
            code, action, scope = "destination_full", "在目的地释放空间或调整共享配额，完整缓存会保留。", "destination"
        elif any(word in lower for word in ("timeout", "timed out", "connection", "network", "unreachable", "disconnect", "离线", "超时", "断网", "连接", "unavailable")):
            code, action, scope = "network", "检查目的地网络或 Tailscale；程序会自动重试，相机仍可导入缓存。", "destination"
    elif isinstance(exc, SourceError):
        code, action = "source_file", "检查相机及原存储卡；此文件保留待重试，已有完整缓存继续上传。"
    if isinstance(exc, DestinationError) and exc.code:
        code, scope = exc.code, "destination"
        action = {
            "destination_path": "检查目的地目录是否存在。",
            "authentication": "检查目的地账号和 .env 凭据；修改后重新测试连接。",
            "permission": "检查目的地的访问、写入和改名权限。",
            "destination_full": "在目的地释放空间或调整配额，完整缓存会保留。",
            "network": "检查目的地网络；程序会自动重试，完整缓存会保留。",
        }.get(code, action)
    return {"code": code, "message": message, "action": action, "scope": scope, "stage": stage}
