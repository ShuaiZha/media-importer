"""Owner-only Feishu controls, durable requests, and small settings cards."""
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .adapters import feishu
from .bot_cards import card, columns, feedback, markdown, note, paragraph, plain, settings_result, status_content
from .config import ConfigError
from .model import now
from .status import snapshot, age
from .store import Store


class Bot:
    def __init__(self, settings, directory, sender=None):
        self.settings, self.directory = settings, Path(directory)
        self.credentials = settings.notification_credentials()
        if self.credentials["receive_id_type"] != "open_id":
            raise ConfigError("飞书交互需要在 .env 中配置 FEISHU_OWNER_OPEN_ID")
        self.owner = self.credentials["receive_id"]
        self.scope = self.credentials["app_id"] + ":" + self.owner
        self.sender = sender or feishu.send_card
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bot_actions (
                    token TEXT PRIMARY KEY, scope TEXT NOT NULL, payload TEXT NOT NULL,
                    revision TEXT, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_jobs (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, payload TEXT NOT NULL,
                    created REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    response TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, error TEXT
                );
            """)
            db.execute("DELETE FROM bot_actions WHERE created < ?", (time.time() - 86400,))
            db.execute("DELETE FROM bot_jobs WHERE state IN ('done','error') AND created < ?", (time.time() - 7 * 86400,))

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.directory / "state.sqlite3", timeout=2)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def action(self, payload, revision=None):
        token = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO bot_actions VALUES (?,?,?,?,?)",
                       (token, self.scope, json.dumps(payload, ensure_ascii=False), revision, time.time()))
        return {"token": token}

    def button(self, label, payload, revision=None, danger=False):
        return {"tag": "button", "text": plain(label), "type": "danger" if danger else "default",
                "value": self.action(payload, revision)}

    def buttons(self, *buttons):
        return {"tag": "action", "actions": list(buttons)}

    def panel(self, page="status", notice=None):
        item = self.settings.read()
        if item["error"]:
            return card("配置需要处理", [markdown("**当前配置无法读取，控制操作暂不可用。**"),
                                       paragraph(item["error"]), note("请在本机检查 .env 和 settings.json，再从底部菜单重试。")], "red")
        values, rev = item["config"], item["revision"]
        elements = feedback(notice) if notice else []
        title = {"transfer": "传输设置", "notifications": "通知设置", "cleanup": "原片与缓存清理",
                 "confirm_delete": "确认开启原片清理", "help": "使用指南"}.get(page, "传输状态")
        color = "orange" if page == "confirm_delete" else "blue"
        if page == "transfer":
            limits = values["limits"]
            fields = [("upload_mib_per_second", "上传限速 · MiB/s", limits["upload_mib_per_second"]),
                      ("max_cache_gib", "缓存额度 · GiB", limits["max_cache_gib"]),
                      ("min_free_disk_gib", "磁盘预留 · GiB", limits["min_free_disk_gib"])]
            elements += [markdown("**控制上传速度与缓存占用**"),
                         note("上传限速、缓存额度填 0 表示不限；磁盘预留用于保留可用空间。"),
                         self.form("transfer", fields, rev), {"tag": "hr"},
                         note("保存后后台自动应用。传输中的文件可能会中断并从头重传，已完整导入的缓存保留。")]
        elif page == "notifications":
            opts = values["notifications"]
            elements += [markdown("**自动通知已" + ("开启**" if opts["enabled"] else "关闭**")),
                         note("进度更新同一张卡片；相机导入完成、归档完成和失败单独提醒。关闭后仍可使用菜单。"),
                         self.buttons(self.button(("关闭" if opts["enabled"] else "开启") + "自动通知",
                                                  {"changes": {"notifications": {"enabled": not opts["enabled"]}}, "page": page}, rev)),
                         {"tag": "hr"},
                         note("进度更新间隔 60–86400 秒；300 秒即 5 分钟。完成和失败不受此间隔限制。"),
                         self.form("notifications", [("progress_interval_seconds", "进度更新间隔 · 秒", opts["progress_interval_seconds"]),
                                                      ("keyword", "消息前缀 · 可留空", opts["keyword"])], rev)]
        elif page == "cleanup":
            b = values["backup"]
            elements += [columns("**相机原片**\n" + ("完整导入后删除" if b["delete_source_after_import"] else "保留，不自动删除"),
                                 "**本地完整缓存**\n正式归档后自动清理"), {"tag": "hr"},
                         paragraph("原片删除以整组素材为单位：视频和附属文件完整导入缓存、核对通过后才执行。"),
                         note("同步关闭时不删除原片；未归档的完整缓存始终保留。")]
            action = {"changes": {"backup": {"delete_source_after_import": False}}, "page": page} if b["delete_source_after_import"] else {"panel": "confirm_delete"}
            elements += [self.buttons(self.button("改为保留原片" if b["delete_source_after_import"] else "查看开启删除的确认说明", action, rev))]
        elif page == "confirm_delete":
            elements += [markdown("**远端归档完成前，缓存盘可能是唯一副本。**"),
                         paragraph("开启后，同步期间素材组完整导入并核对通过就会删除相机原片，不等待上传。此规则也适用于已有的合格完整缓存。"),
                         note("上传失败仍保留完整缓存。关联不明、身份变化或核对失败时，保留相机原片。"),
                         {"tag": "hr"},
                         self.buttons(self.button("取消，保持原设置", {"panel": "cleanup"}),
                                      self.button("确认开启导入后删除", {"changes": {"backup": {"delete_source_after_import": True}}, "page": "cleanup"}, rev, danger=True))]
        elif page == "help":
            elements += [markdown("**点击底部菜单即可操作，无需记住命令。**"),
                         paragraph("状态　查看连接、传输进度和等待原因\n同步　开启或停止两路传输\n设置　调整传输、原片清理与通知"),
                         note("参数在卡片中填写并保存；开启原片删除需要再次确认。")]
        else:
            view = snapshot(self.directory, self.settings)
            title, color, content = status_content(view)
            elements += content
        if isinstance(notice, dict) and notice["level"] == "error":
            color = "red"
            title += " · 未保存"
        elif isinstance(notice, dict) and notice["level"] == "warning":
            color = "orange"
        return card(title, elements, color)

    def form(self, kind, fields, revision):
        inputs = [{"tag": "input", "name": key, "label": plain(label), "default_value": str(int(value)) if isinstance(value, float) and value.is_integer() else str(value),
                   "placeholder": plain(label), "required": key != "keyword"} for key, label, value in fields]
        submit = self.button("保存设置", {"form": kind}, revision)
        submit.update(name="save_" + kind, action_type="form_submit", type="primary")
        return {"tag": "form", "name": kind, "elements": inputs + [submit]}

    def enqueue(self, key, payload):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO bot_jobs (id,scope,payload,created) VALUES (?,?,?,?)",
                       (key, self.scope, json.dumps(payload, ensure_ascii=False), time.time()))

    def receive_message(self, data):
        header, event = data.get("header", {}), data.get("event", {})
        sender, message = event.get("sender", {}), event.get("message", {})
        if (header.get("app_id") != self.credentials["app_id"] or sender.get("sender_type") != "user" or
                sender.get("sender_id", {}).get("open_id") != self.owner or message.get("chat_type") != "p2p"):
            return False
        if message.get("message_type") != "text" or not header.get("event_id"):
            return False
        try:
            value = json.loads(message.get("content", "{}"))
            command = value.get("text", "").strip()
        except (ValueError, AttributeError):
            return False
        # Keep no arbitrary chat text, media, or credentials in the task database.
        commands = {"状态": {"panel": "status"}, "进度": {"panel": "status"}, "帮助": {"panel": "help"},
                    "设置": {"panel": "transfer"}, "传输设置": {"panel": "transfer"},
                    "清理设置": {"panel": "cleanup"}, "通知设置": {"panel": "notifications"},
                    "开启同步": {"changes": {"backup": {"enabled": True}}},
                    "停止同步": {"changes": {"backup": {"enabled": False}}},
                    "关闭同步": {"changes": {"backup": {"enabled": False}}}}
        action = dict(commands.get(command, {"panel": "help", "notice": "未识别这条消息，可直接使用底部菜单。"}))
        if "changes" in action:
            action["revision"] = self.settings.read()["revision"]
            try:
                if abs(time.time() - int(message["create_time"]) / 1000) > 300:
                    action = {"panel": "status", "notice": {"title": "指令已过期，未执行", "level": "warning",
                                                             "detail": "请从底部「同步」菜单重新操作。"}}
            except (KeyError, ValueError, TypeError):
                return False
        self.enqueue("message:" + header["event_id"], action)
        return True

    def receive_card(self, data):
        header, event = data.get("header", {}), data.get("event", {})
        if header.get("app_id") != self.credentials["app_id"] or event.get("operator", {}).get("open_id") != self.owner:
            return {"toast": {"type": "error", "content": "仅配置的本人可以操作"}}
        token = event.get("action", {}).get("value", {}).get("token")
        event_id = header.get("event_id") or event.get("token")
        if not isinstance(token, str) or not isinstance(event_id, str):
            return {"toast": {"type": "error", "content": "无效操作，请从底部菜单获取新卡片"}}
        with self.db() as db:
            row = db.execute("SELECT * FROM bot_actions WHERE token=? AND scope=?", (token, self.scope)).fetchone()
        if row is None or time.time() - row["created"] > 86400:
            return {"toast": {"type": "error", "content": "卡片已过期，请从底部菜单获取新卡片"}}
        payload = json.loads(row["payload"])
        payload["revision"] = row["revision"]
        if "form" in payload:
            payload["values"] = event.get("action", {}).get("form_value", {})
            if len(json.dumps(payload["values"], ensure_ascii=False)) > 2048:
                return {"toast": {"type": "error", "content": "输入内容过长"}}
        self.enqueue("card:" + event_id, payload)
        return {"toast": {"type": "info", "content": "已收到，处理结果将发送到单聊"}}

    def process_one(self):
        with self.db() as db:
            job = db.execute("SELECT * FROM bot_jobs WHERE scope=? AND state IN ('queued','applied') AND retry_at<=? ORDER BY created LIMIT 1",
                             (self.scope, time.time())).fetchone()
        if job is None:
            return False
        try:
            if job["state"] == "queued":
                payload = json.loads(job["payload"])
                page, notice = payload.get("panel", payload.get("page", "status")), payload.get("notice")
                try:
                    changes = payload.get("changes")
                    if "form" in payload:
                        page = payload["form"]
                        values = payload.get("values")
                        expected = {"transfer": {"upload_mib_per_second", "max_cache_gib", "min_free_disk_gib"},
                                    "notifications": {"progress_interval_seconds", "keyword"}}[payload["form"]]
                        if not isinstance(values, dict) or set(values) - expected or expected - {"keyword"} - set(values):
                            raise ConfigError("表单内容不完整，请重新打开设置")
                        try:
                            parsed = {k: (str(v or "") if k == "keyword" else int(v) if k == "progress_interval_seconds" else float(v)) for k, v in values.items()}
                            if payload["form"] == "notifications":
                                parsed.setdefault("keyword", "")
                        except (ValueError, TypeError, OverflowError):
                            raise ConfigError("请使用有效的数字填写设置") from None
                        changes = {"limits" if page == "transfer" else "notifications": parsed}
                    stop_request = changes == {"backup": {"enabled": False}}
                    if changes and not stop_request and time.time() - job["created"] > 300:
                        raise ConfigError("控制请求已经过期，未执行；请重新操作")
                    if changes:
                        item = self.settings.update_preferences(changes, None if stop_request else payload.get("revision"))
                        applied, stopping = False, False
                        until = time.monotonic() + 4
                        while time.monotonic() < until:
                            view = snapshot(self.directory, self.settings)
                            if view["running"] and view["configuration"].get("applied_revision") == item["revision"] and not view["configuration"].get("pending"):
                                applied = True
                                stopping = any(c["state"] == "pausing" for c in view["controls"].values())
                                break
                            time.sleep(.2)
                        notice = settings_result(changes, applied, stopping)
                except (ConfigError, ValueError) as exc:
                    notice = {"title": "本次修改未保存", "detail": str(exc), "level": "error"}
                response = self.panel(page, notice)
                with self.db() as db:
                    db.execute("UPDATE bot_jobs SET state='applied',response=? WHERE id=?", (json.dumps(response, ensure_ascii=False), job["id"]))
            else:
                response = json.loads(job["response"])
            self.sender(self.credentials, response, event_id="control:" + job["id"])
            with self.db() as db:
                db.execute("UPDATE bot_jobs SET state='done',error=NULL WHERE id=?", (job["id"],))
            return True
        except Exception:
            # Never log raw event bodies, credentials, URLs, or SDK exceptions.
            with self.db() as db:
                db.execute("UPDATE bot_jobs SET attempts=attempts+1,retry_at=?,error=? WHERE id=?",
                           (time.time() + min(300, 5 * 2 ** min(job["attempts"], 6)), "控制回复暂时失败，等待重试", job["id"]))
            logging.warning("飞书控制回复暂时失败，已保留请求等待重试")
            return False


def healthy(directory):
    try:
        con = sqlite3.connect(Path(directory).resolve().as_uri() + "/state.sqlite3?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='bot'").fetchone()
            state = json.loads(row[0]) if row else {}
        finally:
            con.close()
        return bool(state.get("connected") and age(state.get("heartbeat_at")) < 20)
    except (OSError, sqlite3.Error, ValueError):
        return False


def serve(settings, directory):
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    bot = Bot(settings, directory)
    def message(event):
        bot.receive_message(json.loads(lark.JSON.marshal(event)))
    def callback(event):
        return P2CardActionTriggerResponse(bot.receive_card(json.loads(lark.JSON.marshal(event))))
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(message).register_p2_card_action_trigger(callback).build()
    client = lark.ws.Client(bot.credentials["app_id"], bot.credentials["app_secret"], event_handler=handler, log_level=lark.LogLevel.ERROR)
    # SDK log text can contain signed websocket URLs. Expose our own sanitized status.
    from lark_oapi.core.log import logger
    logger.disabled = True
    def worker():
        db = Store(directory)
        try:
            while True:
                db.set_meta("bot", {"heartbeat_at": now(), "connected": client._conn is not None})
                bot.process_one()
                time.sleep(.25)
        finally:
            db.close()
    threading.Thread(target=worker, daemon=True, name="bot-controls").start()
    logging.info("飞书控制服务启动；只接受 .env 指定的本人操作")
    try:
        client.start()
    except Exception:
        raise ConfigError("飞书长连接启动失败，请检查 .env、应用状态和网络；详细凭据未写入日志") from None
