"""Owner-only Feishu controls, durable requests, and small settings cards."""
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .adapters import feishu
from .bot_cards import (card, markdown, note, paragraph, plain,
                        settings_result, status_content)
from .config import ConfigError
from .model import now
from .status import snapshot, age
from .store import Store


STATUS_PANELS = {'status': status_content}


class Bot:
    def __init__(self, settings, directory, sender=None, updater=None):
        self.settings, self.directory = settings, Path(directory)
        self.credentials = settings.notification_credentials()
        if self.credentials["receive_id_type"] != "open_id":
            raise ConfigError("飞书交互需要在 .env 中配置 FEISHU_OWNER_OPEN_ID")
        self.owner = self.credentials["receive_id"]
        self.scope = self.credentials["app_id"] + ":" + self.owner
        self.sender = sender or feishu.send_card
        self.updater = updater or feishu.update_card
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

    @staticmethod
    def supported(payload):
        """Retired cards and queued requests cannot modify file-only settings."""
        if payload.get('form') or payload.get('operation'):
            return False
        if payload.get('panel', 'status') not in ('status', 'cleanup', 'confirm_delete'):
            return False
        changes = payload.get('changes', payload.get('changes_applied'))
        if changes is None:
            return True
        if not isinstance(changes, dict) or set(changes) != {'backup'}:
            return False
        backup = changes['backup']
        return (isinstance(backup, dict) and len(backup) == 1
                and set(backup) <= {'enabled', 'delete_source_after_import'}
                and all(type(value) is bool for value in backup.values()))

    def panel(self, page="status", notice=None):
        item = self.settings.read()
        if item["error"]:
            return card("配置需要处理", [paragraph(item["error"]),
                         note("请在本机检查 .env 和 settings.json，再从状态菜单重试。")], "red")
        if notice:
            if isinstance(notice, str):
                notice = {'title': '操作提示', 'detail': notice, 'level': 'info'}
            color = {'error': 'red', 'warning': 'orange', 'success': 'green', 'info': 'blue'}[notice['level']]
            return card(notice['title'], ([paragraph(notice['detail'])] if notice.get('detail') else []) +
                        [self.buttons(self.button('查看状态', {'panel': 'status'}))], color, shared=True)
        values, rev = item["config"], item["revision"]
        if page == 'confirm_delete':
            return card('确认开启原片删除', [
                markdown('**完整导入缓存后删除，不等待 SMB 上传。**'),
                paragraph('远端归档完成前，缓存盘可能是唯一副本。已有合格缓存也适用；关联或身份不确定时保留原片。'),
                note('不归档的关联预览文件也会删除；未选类别原片保留。'),
                self.buttons(self.button('取消', {'panel': 'cleanup'}), self.button('确认开启',
                    {'changes': {'backup': {'delete_source_after_import': True}}, 'page': 'cleanup'}, rev, danger=True))
            ], 'orange', shared=True)
        if page == 'cleanup':
            enabled = values['backup']['delete_source_after_import']
            action = {'changes': {'backup': {'delete_source_after_import': False}}, 'page': 'cleanup'} if enabled else {'panel': 'confirm_delete'}
            return card('原片处理 · ' + ('导入后删除' if enabled else '保留'), [
                paragraph('整组素材完整导入并核对后，才允许删除相机原片。'),
                note('停止同步也停止删除；未归档的完整缓存始终保留。'),
                self.buttons(self.button('关闭删除，保留原片' if enabled else '开启导入后删除', action, rev))
            ], 'orange' if enabled else 'blue', shared=True)
        view = snapshot(self.directory, self.settings)
        title, color, elements = status_content(view)
        elements.append(self.buttons(self.button('刷新', {'panel': 'status', 'refresh': True})))
        return card(title, elements, color, shared=True)

    def problem_actions(self, view, revision):
        return [self.buttons(self.button('查看状态', {'panel': 'status'}))]

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
        commands = {"状态": {"panel": "status"}, "进度": {"panel": "status"}, "查看状态": {"panel": "status"},
                    "运行总览": {"panel": "status"}, "设备与存储": {"panel": "status"},
                    "当前任务": {"panel": "status"}, "归档结果": {"panel": "status"},
                    "原片处理": {"panel": "cleanup"}, "删除原片": {"panel": "cleanup"}, "删除原片开关": {"panel": "cleanup"},
                    "清理设置": {"panel": "cleanup"},
                    "开启同步": {"changes": {"backup": {"enabled": True}}},
                    "停止同步": {"changes": {"backup": {"enabled": False}}},
                    "关闭同步": {"changes": {"backup": {"enabled": False}}}}
        action = dict(commands.get(command, {"panel": "status", "notice":
            "请使用「状态」「同步」「原片处理」菜单。传输限制、素材类别和通知请修改 settings.json；连接与凭据在 .env 中。"}))
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
        if not self.supported(payload):
            return {"toast": {"type": "error", "content": "旧操作已停用，请从菜单获取新卡片；低频设置请修改配置文件"}}
        payload["revision"] = row["revision"]
        context_id = event.get('context', {}).get('open_message_id')
        if isinstance(context_id, str) and re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}', context_id):
            payload['response_message_id'] = context_id
        if payload.get('refresh'):
            message_id = event.get('context', {}).get('open_message_id')
            if (payload.get('panel') not in STATUS_PANELS or not isinstance(message_id, str) or
                    not re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}', message_id)):
                return {'toast': {'type': 'error', 'content': '无法定位原消息，请从状态菜单重新获取卡片'}}
            payload['message_id'] = message_id
        self.enqueue("card:" + event_id, payload)
        return {"toast": {"type": "info", "content": "正在刷新此卡片" if payload.get('refresh') else "已收到，正在处理"}}

    def process_one(self):
        with self.db() as db:
            job = db.execute("SELECT * FROM bot_jobs WHERE scope=? AND state IN ('queued','applied','waiting') AND retry_at<=? ORDER BY created LIMIT 1",
                             (self.scope, time.time())).fetchone()
        if job is None:
            return False
        try:
            payload = json.loads(job['payload'])
            if not self.supported(payload):
                response = self.panel(notice={'title': '旧操作已停用', 'detail': '请从菜单获取新卡片；低频设置请修改配置文件。', 'level': 'info'})
                payload = {'panel': 'status'}
                job = dict(job, state='applied', response=json.dumps(response, ensure_ascii=False))
                with self.db() as db:
                    db.execute("UPDATE bot_jobs SET state='applied',payload=?,response=? WHERE id=?",
                               (json.dumps(payload), job['response'], job['id']))
            waiting = False
            if job['state'] == 'waiting':
                view = snapshot(self.directory, self.settings)
                changes = payload['changes_applied']
                revision = payload['applied_revision']
                stopping = any(c['state'] == 'pausing' for c in view['controls'].values())
                item = self.settings.read()
                obsolete = bool(item.get('error') or item['revision'] != revision)
                ready = view['running'] and not view['configuration'].get('pending') and view['configuration'].get('applied_revision') == revision and not stopping
                if not ready and not obsolete:
                    with self.db() as db:
                        db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + 1, job['id']))
                    return False
                notice = {'title': '设置已有更新', 'detail': '以当前设置为准。', 'level': 'info'} if obsolete else settings_result(changes, True, False)
                response = self.panel(payload.get('page', payload.get('form', 'status')), notice)
                payload['wait_for_apply'] = False
                with self.db() as db:
                    db.execute("UPDATE bot_jobs SET state='applied',response=?,payload=? WHERE id=?", (json.dumps(response, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), job['id']))
            if job["state"] == "queued":
                page, notice = payload.get("panel", payload.get("page", "status")), payload.get("notice")
                try:
                    changes = payload.get("changes")
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
                        waiting = not applied or stopping
                        payload.update(changes_applied=changes, applied_revision=item['revision'], wait_for_apply=waiting)
                        if applied and changes.get('backup', {}).get('enabled') is True and item['config']['notifications']['enabled']:
                            # The notification worker owns the single live progress card.
                            from .notifications import NotificationWorker, _database, _digest
                            from .config import Config
                            config = Config.from_values(item['config'])
                            with _database(self.directory) as outgoing, outgoing:
                                from .destination_binding import identity
                                scope = _digest([identity(outgoing, config), config.source['type'], config.source['serial']])
                                NotificationWorker(self.directory, self.settings, threading.Event())._enqueue(
                                    outgoing, 'started:control:' + self.scope + ':' + job['id'], 'started', scope, '传输进度', '', time.time())
                            with self.db() as db:
                                db.execute("UPDATE bot_jobs SET state='done',error=NULL WHERE id=?", (job['id'],))
                            return True
                except (ConfigError, ValueError) as exc:
                    notice = {"title": "本次修改未保存", "detail": str(exc), "level": "error"}
                response = self.panel(page, notice)
                with self.db() as db:
                    db.execute("UPDATE bot_jobs SET state='applied',response=?,payload=? WHERE id=?", (json.dumps(response, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), job["id"]))
            elif job['state'] != 'waiting':
                response = json.loads(job["response"])
                waiting = payload.get('wait_for_apply', False)
            receipt_key = 'bot_reply:' + self.scope + ':' + job['id']
            with self.db() as db:
                saved = db.execute('SELECT value FROM meta WHERE key=?', (receipt_key,)).fetchone()
            receipt = json.loads(saved[0]) if saved else {}
            message_id = receipt.get('id') or payload.get('message_id') or payload.get('response_message_id')
            if message_id:
                self.updater(self.credentials, message_id, response)
            else:
                message_id = self.sender(self.credentials, response, event_id="control:" + self.scope + ":" + job["id"])
            with self.db() as db:
                if isinstance(message_id, str):
                    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (receipt_key, json.dumps({'id': message_id})))
                db.execute("UPDATE bot_jobs SET state=?,error=NULL,retry_at=? WHERE id=?",
                           ('waiting' if waiting and isinstance(message_id, str) else 'done', time.time() + 1, job['id']))
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
    stop = threading.Event()
    from .workers import Workers
    import os
    # The official SDK owns the main event loop and offers no synchronous stop.
    # Persistent worker failure must terminate PID 1, not leave an idle bot alive.
    workers = Workers(stop, lambda: os._exit(1))
    def worker():
        db = Store(directory)
        heartbeat_at, connected = 0, None
        try:
            while not stop.is_set():
                online = client._conn is not None
                if time.monotonic() - heartbeat_at >= 5 or connected != online:
                    db.set_meta("bot", {"heartbeat_at": now(), "connected": online})
                    heartbeat_at, connected = time.monotonic(), online
                bot.process_one()
                stop.wait(.25)
        finally:
            db.close()
    worker_thread = workers.start('bot-controls', worker)
    logging.info("飞书控制服务启动；只接受 .env 指定的本人操作")
    try:
        client.start()
    except Exception:
        raise ConfigError("飞书长连接启动失败，请检查 .env、应用状态和网络；详细凭据未写入日志") from None
    finally:
        stop.set()
        worker_thread.join(timeout=3)
