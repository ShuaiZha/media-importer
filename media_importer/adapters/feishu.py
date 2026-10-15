"""Outgoing application-bot direct messages; never expose credentials or API bodies."""
from collections import OrderedDict
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from ..model import BackupError, ConfigError


TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 65536
MAX_REQUEST_BYTES = 150 * 1024

MESSAGES = {
    "notification_network": "飞书通知服务暂时不可达，将自动重试。",
    "notification_rate_limited": "飞书限制发送频率，将稍后重试。",
    "notification_authentication": "应用认证失败，请检查 App ID、App Secret 和应用是否启用。",
    "notification_permission": "应用缺少发送消息权限，请启用 im:message:send_as_bot 并发布版本。",
    "notification_recipient": "接收人不可用，请检查邮箱或 Open ID、已发布应用的可用范围，并确认未禁止机器人单聊。",
    "notification_bot_unavailable": "应用机器人不可用，请启用机器人能力、发布应用并确认应用已启用。",
    "notification_ip_restricted": "应用 IP 白名单拒绝请求，请检查白名单与当前出口地址。",
    "notification_quota": "飞书应用本月调用额度已用完，请在飞书管理后台检查额度。",
    "notification_rejected": "飞书拒绝了通知，请检查应用设置和接收人配置。",
    "notification_response": "飞书返回了无法确认成功的响应，将稍后重试。",
    "notification_card_unavailable": "原进度卡已撤回或超过可更新期限，下次进度通知将使用新卡片。",
}

# Common API errors and im/v1 errors from the official API reference. Only invalid
# tenant tokens trigger an immediate refresh; permission failures never do.
_TOKEN_ERRORS = {20013, 99991663, 99991665}
_ERROR_GROUPS = {
    "notification_card_unavailable": {230011, 230031},
    "notification_authentication": {10005, 10015, 20002, 50003, 99991543, 99991661,
                                    99991664, 99991668, 99991671, 99991677} | _TOKEN_ERRORS,
    "notification_permission": {11223, 230027, 99991672, 99991676, 99991679},
    "notification_recipient": {10004, 10010, 10034, 11101, 11102, 11225, 20008, 20010,
                              20021, 20022, 20023, 230013, 230034, 230053, 99992351, 99992352,
                              99992353, 99992361},
    "notification_bot_unavailable": {10014, 11205, 11207, 11209, 11210, 20009,
                                    230006, 99991662, 99991673, 1000007},
    "notification_ip_restricted": {99991401},
    "notification_quota": {99991403},
    "notification_rate_limited": {11232, 11247, 230020, 99991400, 1000004, 1000005},
}
_TOKEN_CACHE = OrderedDict()
_TOKEN_LOCK = threading.Lock()


class NotificationDeliveryError(BackupError):
    def __init__(self, code, *, token_expired=False):
        self.code = code if code in MESSAGES else "notification_response"
        self.token_expired = token_expired
        super().__init__(MESSAGES[self.code])


def validate_credentials(value):
    fields = {"app_id", "app_secret", "receive_id_type", "receive_id"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ConfigError("请填写应用 App ID、App Secret、接收人类型和接收人")
    if any(not isinstance(value[key], str) for key in fields):
        raise ConfigError("应用通知凭据必须使用文本填写")
    result = {key: value[key].strip() for key in fields}
    if not re.fullmatch(r"cli_[A-Za-z0-9]{8,64}", result["app_id"]):
        raise ConfigError("App ID 应是飞书应用凭证页面中以 cli_ 开头的标识")
    secret = result["app_secret"]
    if not secret or len(secret) > 512 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in secret):
        raise ConfigError("请填写有效的 App Secret")
    kind, recipient = result["receive_id_type"], result["receive_id"]
    if kind not in {"email", "open_id"}:
        raise ConfigError("接收人类型只能为邮箱或 Open ID；仅支持单聊通知")
    if kind == "open_id":
        if not re.fullmatch(r"ou_[A-Za-z0-9_-]{8,128}", recipient):
            raise ConfigError("请填写当前应用中本人以 ou_ 开头的 Open ID")
    else:
        # Exactly one plain mailbox: reject display names, lists and URI forms.
        local, separator, domain = recipient.rpartition("@")
        if (not separator or len(recipient) > 254 or len(local) > 64 or
                not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local) or
                local.startswith(".") or local.endswith(".") or ".." in local or
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain) or
                "." not in domain or any(not label or len(label) > 63 or
                                        label.startswith("-") or label.endswith("-")
                                        for label in domain.split("."))):
            raise ConfigError("请填写本人在当前飞书组织中的一个有效邮箱")
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _api_error(code):
    for name, codes in _ERROR_GROUPS.items():
        if code in codes:
            return NotificationDeliveryError(name, token_expired=code in _TOKEN_ERRORS)
    return NotificationDeliveryError("notification_rejected")


def _decode(body):
    if len(body) > MAX_RESPONSE_BYTES:
        raise NotificationDeliveryError("notification_response")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise NotificationDeliveryError("notification_response") from None
    if not isinstance(result, dict) or type(result.get("code")) is not int:
        raise NotificationDeliveryError("notification_response")
    if result["code"] != 0:
        raise _api_error(result["code"])
    return result


def _request(url, payload, token=None, *, method="POST"):
    # URLs are selected solely by this adapter, never by configuration or a response.
    allowed = {TOKEN_URL, MESSAGE_URL + "?receive_id_type=email", MESSAGE_URL + "?receive_id_type=open_id"}
    updating = method == "PATCH" and re.fullmatch(re.escape(MESSAGE_URL) + r"/om_[A-Za-z0-9_-]{1,128}", url)
    if not updating and not (method == "POST" and url in allowed):
        raise NotificationDeliveryError("notification_rejected")
    try:
        data = json.dumps(payload, ensure_ascii=False).encode()
    except (ValueError, TypeError, UnicodeError):
        raise NotificationDeliveryError("notification_rejected") from None
    if len(data) > MAX_REQUEST_BYTES:
        raise NotificationDeliveryError("notification_rejected")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return _decode(response.read(MAX_RESPONSE_BYTES + 1))
    except NotificationDeliveryError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            # Feishu commonly returns useful numeric business codes with HTTP 400.
            body = exc.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            body = b""
        finally:
            exc.close()
        if status == 429:
            raise NotificationDeliveryError("notification_rate_limited") from None
        if 300 <= status < 400:
            raise NotificationDeliveryError("notification_rejected") from None
        if status >= 500:
            raise NotificationDeliveryError("notification_network") from None
        try:
            _decode(body)
        except NotificationDeliveryError as error:
            if error.code != "notification_response":
                raise error from None
        if status == 401:
            raise NotificationDeliveryError("notification_authentication", token_expired=token is not None) from None
        if status == 403:
            raise NotificationDeliveryError("notification_permission") from None
        raise NotificationDeliveryError("notification_rejected") from None
    except Exception:
        # urllib exceptions can contain an access token or server-provided text.
        raise NotificationDeliveryError("notification_network") from None


def _cache_key(credentials):
    return hashlib.sha256((credentials["app_id"] + "\0" + credentials["app_secret"]).encode()).digest()


def _access_token(credentials, rejected=None):
    key = _cache_key(credentials)
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(key)
        if cached and cached[1] > time.monotonic() and cached[0] != rejected:
            _TOKEN_CACHE.move_to_end(key)
            return cached[0]
        _TOKEN_CACHE.pop(key, None)
        started = time.monotonic()
        response = _request(TOKEN_URL, {"app_id": credentials["app_id"], "app_secret": credentials["app_secret"]})
        token, expires = response.get("tenant_access_token"), response.get("expire")
        if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._-]{8,4096}", token) or
                type(expires) is not int or not 0 < expires <= 86400):
            raise NotificationDeliveryError("notification_response")
        # Use monotonic time and count network time against the remaining lifetime.
        lifetime = expires - min(60, max(1, expires / 10))
        _TOKEN_CACHE[key] = (token, started + lifetime)
        while len(_TOKEN_CACHE) > 8:
            _TOKEN_CACHE.popitem(last=False)
        return token


def check_credentials(credentials):
    """Authenticate without sending a message or returning a bearer token."""
    _access_token(validate_credentials(credentials))
    return {"authenticated": True}


def send(credentials, title, text, keyword="", *, event_id=None):
    """Send one direct message; the caller supplies a persistent unique event ID.

    Feishu deduplicates a UUID for one hour. Retry with the same event_id after an
    uncertain response; distinct test clicks should omit it to create a new UUID.
    """
    credentials = validate_credentials(credentials)
    content = "\n".join(part for part in (keyword.strip(), title, text) if part)
    return send_content(credentials, "text", {"text": content}, event_id=event_id)


def send_card(credentials, card, *, event_id=None):
    return send_content(credentials, "interactive", card, event_id=event_id)


def update_card(credentials, message_id, card):
    """Replace our shared card in place. PATCH can safely be retried after a timeout."""
    credentials = validate_credentials(credentials)
    if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9_-]{1,128}", message_id):
        raise NotificationDeliveryError("notification_rejected")
    payload = {"content": json.dumps(card, ensure_ascii=False)}
    token = _access_token(credentials)
    for attempt in range(2):
        try:
            _request(MESSAGE_URL + "/" + message_id, payload, token, method="PATCH")
            return message_id
        except NotificationDeliveryError as error:
            if not error.token_expired or attempt:
                raise
            token = _access_token(credentials, rejected=token)


def send_content(credentials, kind, content, *, event_id=None):
    credentials = validate_credentials(credentials)
    identity = json.dumps([credentials["app_id"], credentials["receive_id_type"],
                           credentials["receive_id"], str(event_id)], ensure_ascii=False)
    message_uuid = str(uuid.uuid4() if event_id is None else uuid.uuid5(uuid.NAMESPACE_URL, identity))
    payload = {"receive_id": credentials["receive_id"], "msg_type": kind,
               "content": json.dumps(content, ensure_ascii=False), "uuid": message_uuid}
    url = MESSAGE_URL + "?receive_id_type=" + credentials["receive_id_type"]
    token = _access_token(credentials)
    for attempt in range(2):
        try:
            response = _request(url, payload, token)
            message = response.get("data")
            if not isinstance(message, dict) or not isinstance(message.get("message_id"), str) or not message["message_id"]:
                raise NotificationDeliveryError("notification_response")
            return message["message_id"]
        except NotificationDeliveryError as error:
            if not error.token_expired or attempt:
                raise
            token = _access_token(credentials, rejected=token)
