import hashlib
import json
import re
import time
import uuid
import threading
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import PurePosixPath
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from ..paths import safe_relative
from ..model import Asset, SourceError, SourceConnectionError, SourceBlocked, Cancelled, now, device_directory
from .gopro_models import compatibility, model_key


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.links.extend(v for k, v in attrs if k.lower() == "href" and v is not None)



def make_asset(serial, path, size, created, modified, gumi, primary, parent_gumi="", device_name="GoPro"):
    # GUMI and the camera timestamps define the source's stable material identity.
    payload = {"serial": serial, "path": path, "size": size, "created": created,
               "modified": modified, "gumi": gumi, "primary": primary, "parent_gumi": parent_gumi}
    identity = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(payload, sort_keys=True)).hex
    return Asset(serial, path, size, created, modified, identity, device_name, "gopro",
                 {"gumi": gumi, "primary": primary, "parent_gumi": parent_gumi})


def defaults():
    return {"type": "gopro", "url": "", "serial": "", "timeout_seconds": 20,
            "transport": "usb", "request_attempts": 3}


def discover(hints, stop, *, scan=False):
    from .gopro_discovery import discover as discover_usb, usb_url
    matching = [hint for hint in hints if hint.get('type') == 'gopro']
    addresses = [hint.get('url') for hint in matching]
    addresses += [usb_url(hint.get('serial') or hint.get('serial_number', '')) for hint in reversed(matching)]
    return discover_usb(addresses, stop, scan=scan)


def media_category(asset):
    extension = PurePosixPath(asset.path).suffix.lower()
    if extension in ('.lrv', '.360'):
        return 'video'
    if extension == '.gpr':
        return 'photo'
    return None  # Standard formats use the common classification.


def validate(values):
    from ..config import ConfigError
    for key, value in defaults().items():
        values.setdefault(key, value)
    if values["transport"] != "usb":
        raise ConfigError("当前仅支持 USB；请将 SOURCE_TRANSPORT 设为 usb")
    for key in ("url",):
        if not values[key]:
            continue  # USB discovery supplies the address before confirmation.
        url = urlsplit(values[key])
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ConfigError(f"source.{key} 必须是包含协议和端口的相机地址，不含路径或凭据")
        values[key] = values[key].rstrip("/")
    if values["serial"] and not values["serial"].isalnum():
        raise ConfigError("source.serial 必须填写相机序列号")
    if values["timeout_seconds"] < 1:
        raise ConfigError("source.timeout_seconds 必须至少为 1")
    if type(values["request_attempts"]) is not int or not 1 <= values["request_attempts"] <= 5:
        raise ConfigError("SOURCE_REQUEST_ATTEMPTS 必须为 1 到 5")


class GoProSource:
    keep_alive_interval_seconds = 3

    def __init__(self, config, stop=None):
        self.config = config
        self.transport = config.source.get("transport", "usb")
        self.base = config.source["url"]
        self.serial = config.source["serial"]
        self.timeout = config.source["timeout_seconds"]
        self.stop = stop
        self.device_name = "Camera"
        self.compatibility = compatibility('')
        self.opener = build_opener(_NoRedirect())
        self.attempts = config.source.get("request_attempts", 3)

    def fork(self, stop=None, *, purpose='transfer'):
        client = type(self)(self.config, self.stop if stop is None else stop)
        if purpose in ('monitor', 'keep_alive'):
            client.timeout = min(client.timeout, 3 if purpose == 'monitor' else 2)
            client.attempts = 1
        return client

    @staticmethod
    def deletion_effects(asset, members):
        """A single-file delete may also remove this primary's associated sidecars."""
        folder = PurePosixPath(asset.path).parent
        gumi = asset.metadata.get('gumi') if asset.metadata.get('primary') else None
        return {asset.identity} | {member.identity for member in members
                                   if gumi and not member.metadata.get('primary')
                                   and member.metadata.get('parent_gumi') == gumi
                                   and member.serial == asset.serial and member.source_type == asset.source_type
                                   and PurePosixPath(member.path).parent == folder}

    @staticmethod
    def deletion_candidates(assets):
        result = {}
        for asset in assets:
            metadata = asset.metadata
            if not metadata.get("primary"):
                result[asset.identity] = "相机未联动删除此附属文件，保留文件"
            elif not metadata.get("gumi"):
                result[asset.identity] = "素材缺少 GUMI，未执行删除"
            else:
                result[asset.identity] = None
        return result

    @staticmethod
    def groups(assets):
        """Deletion boundary: a primary and its unambiguously associated sidecars."""
        primary = [a for a in assets if a.metadata.get("primary")]
        result = []
        assigned = set()
        for parent in primary:
            gumi = parent.metadata.get("gumi")
            folder = str(PurePosixPath(parent.path).parent)
            matches = [a for a in primary if a.metadata.get("gumi") == gumi
                       and str(PurePosixPath(a.path).parent) == folder]
            members = [parent]
            if gumi and len(matches) == 1:
                members += [a for a in assets if not a.metadata.get("primary")
                            and a.metadata.get("parent_gumi") == gumi and str(PurePosixPath(a.path).parent) == folder]
            assigned.update(a.identity for a in members)
            # Unassigned files with the same media number make the association uncertain.
            possible = [a for a in assets if str(PurePosixPath(a.path).parent) == folder
                        and PurePosixPath(a.path).stem[2:] == PurePosixPath(parent.path).stem[2:]
                        and a.identity not in {m.identity for m in members}]
            safe = bool(gumi and len(matches) == 1 and not possible)
            result.append({"id": parent.identity, "members": members, "safe": safe,
                           "reason": None if safe else "素材身份或附属关系不明确，保留相机文件"})
        for asset in assets:
            if asset.identity not in assigned:
                result.append({"id": asset.identity, "members": [asset], "safe": False,
                               "reason": "无法确认附属文件归属，已导入但不自动删除"})
        # HERO9's later chapters can lose /media/info after the first chapter
        # is deleted. Import the complete recording before removing any part,
        # and remove the highest chapter first so the first stays available.
        recordings, order = {}, []
        for group in result:
            parent = group["members"][0]
            match = re.fullmatch(r"(G[HX])(\d{2})(\d{4})\.MP4", PurePosixPath(parent.path).name, re.I)
            key = (parent.serial, str(PurePosixPath(parent.path).parent), match[1].upper(), match[3], parent.created) if (
                model_key(parent.device_name) == 'HERO9' and
                parent.metadata.get("primary") and match and int(match[2]) > 0 and parent.created) else (group["id"],)
            if key not in recordings:
                recordings[key] = []
                order.append(key)
            recordings[key].append((int(match[2]) if match else 0, group))
        combined = []
        for key in order:
            chapters = sorted(recordings[key], key=lambda item: item[0], reverse=True)
            if len(chapters) == 1:
                combined.append(chapters[0][1])
                continue
            safe = all(g["safe"] for _, g in chapters) and len({n for n, _ in chapters}) == len(chapters)
            combined.append({"id": chapters[-1][1]["id"],
                             "members": [a for _, g in chapters for a in g["members"]], "safe": safe,
                             "reason": None if safe else "视频分段或附属关系不明确，保留相机文件"})
        for group in combined:
            # Only this adapter identifies expendable previews. RAW, audio and
            # unknown companions remain required, even when not primary media.
            parents = {a.metadata.get('gumi') for a in group['members'] if a.metadata.get('primary')}
            group['preview_ids'] = [a.identity for a in group['members']
                                    if group['safe'] and not a.metadata.get('primary')
                                    and a.metadata.get('parent_gumi') in parents
                                    and PurePosixPath(a.path).suffix.lower() in ('.lrv', '.thm')]
        return combined

    @staticmethod
    def reconcile(assets, rows):
        """A sidecar left by our recorded primary deletion keeps its original identity."""
        from ..store import Store
        prior = {Store.asset(r).path: (Store.asset(r), r) for r in rows}
        current = {a.path: a for a in assets}
        deleted_parents = {Store.asset(r).metadata.get("gumi") for r in rows
                           if r["cleanup"] in ("deleting", "deleted", "absent")
                           and Store.asset(r).metadata.get("primary") and Store.asset(r).path not in current}
        result = []
        for asset in assets:
            old = prior.get(asset.path)
            if old and not asset.metadata.get("primary") and not asset.metadata.get("parent_gumi"):
                previous, row = old
                parent = previous.metadata.get("parent_gumi")
                if (parent and parent in deleted_parents and asset.serial == previous.serial
                        and asset.size == previous.size and asset.modified == previous.modified and asset.modified):
                    asset = previous
            result.append(asset)
        return result

    def _check_stop(self):
        if self.stop and self.stop.is_set():
            raise Cancelled("正在停止")

    def _open(self, path, headers=None):
        self._check_stop()
        try:
            response = self.opener.open(Request(self.base + path, headers=headers or {}), timeout=self.timeout)
            if getattr(self, 'on_activity', None):
                self.on_activity()
            return response
        except HTTPError as exc:
            error = SourceConnectionError if exc.code in (408, 429, 500, 502, 503, 504) else SourceError
            raise error(f"相机请求失败 {path.split('?')[0]}: HTTP {exc.code}") from exc
        except (URLError, OSError, HTTPException) as exc:
            raise SourceConnectionError(f"相机连接超时或中断 {path.split('?')[0]}") from exc

    def _read(self, path, limit, headers=None):
        # Retry only bounded, idempotent reads. In particular, NEVER retry delete here.
        for attempt in range(self.attempts):
            try:
                with self._open(path, headers) as response:
                    body = response.read(limit)
                    if getattr(self, 'on_activity', None):
                        self.on_activity()
                    return body, response.status, response.headers
            except (SourceConnectionError, OSError, HTTPException) as exc:
                if attempt + 1 == self.attempts:
                    raise SourceConnectionError("相机读取超时或中断，稍后自动重试") from exc
                if (self.stop or threading.Event()).wait(min(2 ** attempt, 4)):
                    self._check_stop()

    def _json(self, path):
        try:
            body, _, _ = self._read(path, 16 * 1024 * 1024)
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("不是 JSON 对象")
            return data
        except (ValueError, OSError, HTTPException) as exc:
            raise SourceError(f"相机返回的数据无效: {exc}") from exc

    def identify(self):
        if not self.base or not self.serial:
            raise SourceError('等待发现并确认 USB 相机')
        info = self.info()
        if info.get("serial_number") != self.serial:
            raise SourceError("相机序列号与配置不符，不处理这台设备")
        return info

    def info(self):
        info = self._json("/gp/gpControl/info")
        info = info.get("info", info)
        if (not isinstance(info, dict) or not isinstance(info.get("serial_number"), str)
                or not info["serial_number"].isalnum() or not isinstance(info.get("model_name"), str)):
            raise SourceError("相机身份响应无效")
        self.device_name = re.sub(r"\s+Black$", "", info["model_name"], flags=re.IGNORECASE).strip()
        self.compatibility = compatibility(info['model_name'], info.get('firmware_version', ''))
        return {**{key: info.get(key) for key in ("model_name", "serial_number", "firmware_version")},
                "archive_directory": device_directory(self.device_name, info["serial_number"]),
                "transport": self.transport, "compatibility": self.compatibility}

    @classmethod
    def probe(cls, url):
        from ..config import ConfigError
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ConfigError("请填写相机网络地址，包含 http:// 和端口，不含路径或凭据")
        config = SimpleNamespace(source={"url": url.rstrip("/"), "serial": "", "timeout_seconds": 10})
        return cls(config).info()

    @staticmethod
    def decode_status(state):
        if not isinstance(state, dict):
            raise SourceError("相机状态响应无效")
        def integer(key, minimum=0, maximum=None):
            value = state.get(str(key))
            return value if type(value) is int and value >= minimum and (maximum is None or value <= maximum) else None
        def flag(key):
            value = integer(key, 0, 1)
            return bool(value) if value is not None else None
        bars = integer(2, 0, 4)
        storage = integer(33, -1, 8)
        storage = storage if storage in (-1, 0, 1, 2, 3, 4, 8) else None
        values = {"battery_present": flag(1), "battery_percent": integer(70, 0, 100),
                  "charging": bars == 4 if bars is not None else None,
                  "overheating": flag(6), "cold": flag(85), "busy": flag(8), "recording": flag(10),
                  "ready": flag(82),
                  "storage_status": {0: "ready", 1: "full", 2: "missing", 3: "format_error", 4: "busy", 8: "error"}.get(storage, "unknown"),
                  "storage_remaining_bytes": integer(54) * 1024 if integer(54) is not None else None,
                  "temperature_c": None, "storage_capacity_bytes": None, "usb_connected": None}
        if values["battery_present"] is False:
            values.update(battery_percent=None, charging=None)
        return {**values, "observed_at": now(), "available": True,
                "capabilities": {key: value is not None for key, value in values.items()}}

    def telemetry(self):
        return self.decode_status(self._json("/gopro/camera/state").get("status"))

    @staticmethod
    def idle_reason(state):
        if state.get("overheating") is True:
            return "相机报告过热，等待温度告警解除；完整缓存可继续上传"
        if state.get("cold") is True:
            return "相机报告过冷，等待恢复"
        if state.get("recording") is True:
            return "相机正在录像，等待结束后导入"
        if state.get("busy") is True or state.get("ready") is False:
            return "相机忙碌或尚未就绪，等待恢复"
        if state.get("busy") is None or state.get("recording") is None:
            return "无法确认相机录制/忙碌状态，等待相机就绪"
        if state.get("storage_status") in ("missing", "format_error", "busy", "error"):
            return "存储卡未插入、忙碌或状态异常，等待相机恢复"
        # A full card still permits importing its existing footage.
        return None

    def ensure_idle(self):
        reason = self.idle_reason(self.telemetry())
        if reason:
            raise SourceBlocked(reason)

    def keep_alive(self):
        try:
            with self._open("/gopro/camera/keep_alive") as response:
                response.read(4096)
        except (OSError, HTTPException) as exc:
            raise SourceError("相机保活响应中断") from exc

    def media_list(self):
        data = self._json("/gopro/media/list")
        if not isinstance(data.get("media"), list):
            raise SourceError("相机媒体清单不完整")
        result = {}
        for directory in data["media"]:
            folder = safe_relative(directory["d"])
            for item in directory["fs"]:
                name = item["n"]
                names = [name]
                if "g" in item:
                    match = re.fullmatch(r"(G\d{3})(\d{4})(\.[A-Za-z0-9]+)", name)
                    if not match:
                        raise SourceError(f"无法展开分组素材: {name}")
                    begin, end = int(item["b"]), int(item["l"])
                    if not 0 <= begin <= end <= 9999:
                        raise SourceError(f"无效的分组范围: {name}")
                    missing = {int(x) for x in item.get("m", [])}
                    names = [f"{match[1]}{i:04d}{match[3]}" for i in range(begin, end + 1) if i not in missing]
                for member in names:
                    path = safe_relative(folder + "/" + member)
                    result[path] = item
        return result

    def paths(self):
        self.identify()
        pending = ["/videos/DCIM/"]
        visited, files = set(), set()
        while pending:
            directory = pending.pop()
            if directory in visited:
                continue
            visited.add(directory)
            if len(visited) > 4096:
                raise SourceError("相机目录数量异常")
            try:
                body, _, headers = self._read(quote(directory, safe="/"), 16 * 1024 * 1024)
                content_type = headers.get("Content-Type", "")
            except (OSError, HTTPException) as exc:
                raise SourceError("相机目录响应中断") from exc
            if "html" not in content_type.lower() or len(body) >= 16 * 1024 * 1024:
                raise SourceError("相机目录清单无效或过大")
            parser = _Links()
            parser.feed(body.decode("utf-8", "strict"))
            for href in parser.links:
                target = urlsplit(urljoin(self.base + quote(directory, safe="/"), href))
                if (target.scheme, target.netloc) != (urlsplit(self.base).scheme, urlsplit(self.base).netloc) or target.query or target.fragment:
                    continue
                path = unquote(target.path)
                if "/../" in path or path.endswith("/..") or "/./" in path:
                    continue
                if not path.startswith(directory) or path == directory:
                    continue
                relative = path[len("/videos/DCIM/"):]
                if not relative or len(PurePosixPath(relative).parts) > 8:
                    continue
                # HERO9 keeps mutable camera state at DCIM/leinfo.sav; it is not footage.
                if relative.lower() == "leinfo.sav":
                    continue
                safe_relative(relative.rstrip("/"))
                if path.endswith("/"):
                    pending.append(path)
                else:
                    files.add(relative)
                if len(files) > 100000:
                    raise SourceError("相机文件数量超过首版扫描上限")
        return files

    def file_stat(self, path):
        url = "/videos/DCIM/" + quote(safe_relative(path), safe="/")
        # HERO9 rejects HEAD; Range 0-0 returns the exact size with one byte.
        try:
            _, status, headers = self._read(url, 1, {"Range": "bytes=0-0"})
            if status == 206:
                match = re.fullmatch(r"bytes 0-0/(\d+)", headers.get("Content-Range", ""))
                if not match:
                    raise SourceError(f"相机返回的范围信息无效: {path}")
                size = int(match[1])
            elif status == 200:
                size = int(headers["Content-Length"])
            else:
                raise SourceError(f"无法读取文件信息: {path}")
            return size, headers.get("Last-Modified", "")
        except SourceError as exc:
            cause = exc.__cause__
            if isinstance(cause, HTTPError) and cause.code == 416 and cause.headers.get("Content-Range") == "bytes */0":
                return 0, cause.headers.get("Last-Modified", "")
            raise
        except (KeyError, ValueError, OSError, HTTPException) as exc:
            raise SourceError(f"无法读取准确文件大小: {path}") from exc

    def media_info(self, path):
        # HERO9's HTTP parser requires the path separator to remain unescaped.
        try:
            return self._json("/gopro/media/info?" + urlencode({"path": safe_relative(path)}, safe="/"))
        except SourceError as exc:
            if not (self.compatibility['chapter_metadata_fallback'] and
                    isinstance(exc.__cause__, HTTPError) and exc.__cause__.code == 404
                    and PurePosixPath(path).suffix.lower() == ".mp4"):
                raise
            return self._mp4_media_info(path)

    def _mp4_media_info(self, path):
        """Bounded MP4 identity reads when a remaining chapter has no API metadata."""
        size, modified = self.file_stat(path)
        consumed, count = 0, 0

        def read(offset, length):
            nonlocal consumed, count
            consumed += length
            count += 1
            if offset < 0 or length < 1 or offset + length > size or consumed > 65536 or count > 128:
                raise SourceError("MP4 元数据结构异常，保留素材")
            data, status, headers = self._read("/videos/DCIM/" + quote(safe_relative(path), safe="/"),
                                              length + 1, {"Range": f"bytes={offset}-{offset + length - 1}"})
            if status != 206 or headers.get("Content-Range") != f"bytes {offset}-{offset + length - 1}/{size}" or len(data) != length:
                raise SourceError("相机未返回准确的 MP4 元数据范围")
            return data

        def boxes(start, end):
            while start < end:
                if end - start < 8:
                    raise SourceError("MP4 元数据边界无效")
                header = read(start, 8)
                length, kind, width = int.from_bytes(header[:4], "big"), header[4:], 8
                if length == 1:
                    length, width = int.from_bytes(read(start + 8, 8), "big"), 16
                if length == 0:
                    length = end - start
                if length < width or start + length > end:
                    raise SourceError("MP4 元数据长度无效")
                yield kind, start + width, start + length
                start += length

        created, gumi = None, None
        for kind, start, end in boxes(0, size):
            if kind != b"moov":
                continue
            for child, begin, finish in boxes(start, end):
                if child == b"mvhd":
                    version = read(begin, 1)[0]
                    if version not in (0, 1) or finish - begin < (12 if version else 8) or created is not None:
                        raise SourceError("MP4 创建时间不可确认")
                    created = int.from_bytes(read(begin + 4, 8 if version else 4), "big") - 2082844800
                elif child == b"udta":
                    for tag, first, last in boxes(begin, finish):
                        if tag == b"GUMI":
                            if last - first != 16 or gumi is not None:
                                raise SourceError("MP4 素材身份不可确认")
                            gumi = read(first, 16).hex()
            break
        if created is None or created < 0 or not gumi or gumi == "0" * 32:
            raise SourceError("MP4 缺少可确认的创建时间或 GUMI，保留素材")
        if self.file_stat(path) != (size, modified):
            raise SourceError("读取元数据时相机素材已变化")
        return {"cre": str(created), "s": str(size), "gumi": gumi}

    @staticmethod
    def gumi(value):
        return value.lower() if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{32}", value) else ""

    def inventory(self):
        self.identify()
        self.ensure_idle()
        primary = self.media_list()
        paths = self.paths()
        if set(primary) - paths:
            raise SourceError("媒体接口与目录清单不一致，稍后重试")
        assets = []
        by_stem = {}
        for path in sorted(paths):
            size, modified = self.file_stat(path)
            metadata = self.media_info(path) if path in primary else {}
            if metadata.get("s") is not None and int(metadata["s"]) != size:
                raise SourceError(f"素材大小正在变化: {path}")
            item = primary.get(path, {})
            asset = make_asset(self.serial, path, size, str(metadata.get("cre", item.get("cre", ""))),
                          modified, self.gumi(metadata.get("gumi")), path in primary, device_name=self.device_name)
            assets.append(asset)
            if asset.metadata["primary"]:
                source = PurePosixPath(path)
                by_stem.setdefault((str(source.parent), source.stem[2:]), []).append(asset)
        resolved = []
        for asset in assets:
            if not asset.metadata["primary"]:
                source = PurePosixPath(asset.path)
                candidates = by_stem.get((str(source.parent), source.stem[2:]), [])
                if len(candidates) == 1:
                    parent = candidates[0]
                    asset = make_asset(asset.serial, asset.path, asset.size, parent.created,
                                  asset.modified, "", False, parent.metadata["gumi"], device_name=self.device_name)
            resolved.append(asset)
        self.ensure_idle()
        return resolved

    def snapshot(self):
        first = self.inventory()
        if self.stop:
            if self.stop.wait(self.config.backup["stability_seconds"]):
                raise Cancelled("正在停止")
        else:
            time.sleep(self.config.backup["stability_seconds"])
        second = self.inventory()
        if [a.identity for a in first] != [a.identity for a in second]:
            raise SourceError("素材清单正在变化，等待稳定后再备份")
        return second

    def verify_primary(self, asset):
        self.identify()
        self.ensure_idle()
        if not asset.metadata["primary"] or not asset.metadata["gumi"]:
            raise SourceError("缺少可确认的素材 ID，保留相机文件")
        size, modified = self.file_stat(asset.path)
        info = self.media_info(asset.path)
        actual = make_asset(asset.serial, asset.path, size, str(info.get("cre", "")), modified,
                       self.gumi(info.get("gumi")), True, device_name=self.device_name)
        if actual.identity != asset.identity:
            raise SourceError("相机素材身份已经变化，保留文件并重新扫描")

    def read_range(self, asset, offset, length):
        """Strict, bounded reads for recovering an existing cache identity."""
        self.identify()
        self.ensure_idle()
        if offset < 0 or not 0 < length <= 64 * 1024 or offset + length > asset.size:
            raise SourceError("缓存核验范围无效")
        if self.file_stat(asset.path) != (asset.size, asset.modified):
            raise SourceError("原片身份变化，停止缓存核验")
        end = offset + length - 1
        try:
            with self._open("/videos/DCIM/" + quote(asset.path, safe="/"),
                            {"Range": f"bytes={offset}-{end}"}) as response:
                if response.status != 206 or response.headers.get("Content-Range") != f"bytes {offset}-{end}/{asset.size}":
                    raise SourceError("相机未返回准确范围，未继续读取大文件")
                data = response.read(length + 1)
                if len(data) != length:
                    raise SourceError("相机核验范围长度不一致")
                return data
        except (OSError, HTTPException) as exc:
            raise SourceError("相机缓存核验读取失败") from exc

    def download(self, asset, partial_path, progress):
        self.identify()
        self.ensure_idle()
        size, modified = self.file_stat(asset.path)
        if (size, modified) != (asset.size, asset.modified):
            raise SourceError("相机素材已变化，等待重新扫描")
        if asset.metadata["primary"] and asset.metadata["gumi"]:
            self.verify_primary(asset)
        received = 0
        digest = hashlib.sha256()
        try:
            with self._open("/videos/DCIM/" + quote(asset.path, safe="/")) as response, open(partial_path, "wb") as output:
                if response.status != 200 or int(response.headers.get("Content-Length", -1)) != asset.size:
                    raise SourceError(f"下载长度与素材清单不一致: {asset.path}")
                while True:
                    self._check_stop()
                    try:
                        chunk = response.read(1024 * 1024)
                    except (OSError, HTTPException) as exc:
                        raise SourceConnectionError("相机下载连接中断；未完成文件将从头重试") from exc
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if received > asset.size:
                        raise SourceError("下载数据超过预期大小")
                    progress(received, asset.size)
                output.flush()
                import os
                os.fsync(output.fileno())
            if received != asset.size or self.file_stat(asset.path) != (asset.size, asset.modified):
                raise SourceError("下载未完整结束或原片已变化")
            if asset.metadata["primary"] and asset.metadata["gumi"]:
                self.verify_primary(asset)
            return digest.hexdigest()
        except HTTPException as exc:
            raise SourceConnectionError("相机下载连接中断；未完成文件将从头重试") from exc
        except ValueError as exc:
            raise SourceError("相机下载响应无效") from exc

    def delete(self, asset):
        self.verify_primary(asset)
        if not self.compatibility['delete_verified']:
            raise SourceError('此型号或固件尚未验证原片删除，保留相机文件')
        try:
            with self._open("/gopro/media/delete/file?" + urlencode({"path": asset.path}, safe="/")) as response:
                if response.status != 200:
                    raise SourceError("相机未确认删除请求")
                response.read(4096)
        except (OSError, HTTPException) as exc:
            raise SourceError("删除响应中断，需要重新列举确认结果") from exc


def create(config, stop=None):
    return GoProSource(config, stop)


def probe(url):
    return GoProSource.probe(url)
