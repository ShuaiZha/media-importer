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
                        settings_result, status_content, format_time)
from .config import ConfigError
from .model import now
from .status import snapshot, age
from .store import Store


STATUS_PANELS = {'status': status_content}


class Bot:
    def __init__(self, settings, directory, sender=None, updater=None):
        self.settings, self.directory = settings, Path(directory)
        self.settings.directory = self.directory
        self.credentials = settings.notification_credentials()
        if self.credentials["receive_id_type"] != "open_id":
            raise ConfigError("飞书交互需要在 .env 中配置 FEISHU_OWNER_OPEN_ID")
        self.owner = self.credentials["receive_id"]
        self.scope = self.credentials["app_id"] + ":" + self.owner
        self.sender = sender or feishu.send_card
        self.updater = updater or feishu.update_card
        self._source_event = None
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
        if payload.get('panel', 'status') not in ('status', 'cleanup', 'confirm_delete', 'source_confirmation', 'source_offline'):
            return False
        if 'source_decision' in payload:
            return (type(payload['source_decision']) is bool and
                    isinstance(payload.get('session_id'), str) and
                    re.fullmatch(r'[0-9a-f]{32}', payload['session_id']) is not None and not payload.get('changes'))
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
            return card("配置异常", [paragraph(item["error"]),
                         note("请在本机检查 .env 和 settings.json，然后刷新状态。")], "red")
        if notice:
            if isinstance(notice, str):
                notice = {'title': '操作说明', 'detail': notice, 'level': 'info'}
            color = {'error': 'red', 'warning': 'orange', 'success': 'green', 'info': 'blue'}[notice['level']]
            return card(notice['title'], ([paragraph(notice['detail'])] if notice.get('detail') else []) + [note(format_time(now()))] +
                        [self.buttons(self.button('查看状态', {'panel': 'status'}))], color, shared=True)
        values, rev = item["config"], item["revision"]
        if page in ('source_confirmation', 'source_offline'):
            return self.source_panel(values, page)
        if page == 'confirm_delete':
            return card('启用自动删除原片', [
                markdown('**原片将在本地导入与核验完成后删除，不等待远端归档。**'),
                paragraph('远端归档完成前，本地缓存可能是唯一副本。此规则也适用于已有的合格缓存。'),
                note('关联预览文件同时删除；范围外或身份无法确认的素材保留。停止同步将停止删除操作。'),
                self.buttons(self.button('取消', {'panel': 'cleanup'}), self.button('确认启用',
                    {'changes': {'backup': {'delete_source_after_import': True}}, 'page': 'cleanup'}, rev, danger=True))
            ], 'orange', shared=True)
        if page == 'cleanup':
            enabled = values['backup']['delete_source_after_import']
            from .source_connection import read, deletion_enabled
            from .config import Config
            effective = deletion_enabled(Config.from_values(values), read(self.directory, 'source_selection'))
            action = {'changes': {'backup': {'delete_source_after_import': False}}, 'page': 'cleanup'} if enabled else {'panel': 'confirm_delete'}
            return card('原片处理', [
                markdown('**自动删除：' + ('已启用' if enabled else '已停用') + '**'),
                paragraph('当前设备未通过删除验收，保留原片；配置开关保持不变。' if enabled and not effective else
                          '整组素材完成本地导入与核验后，自动删除相机原片。' if enabled else '导入完成后保留相机原片。'),
                note('删除不等待远端归档；停止同步也停止删除。' if enabled else '如需自动释放相机空间，可启用导入后删除。'),
                self.buttons(self.button('停用自动删除' if enabled else '启用自动删除', action, rev))
            ], 'blue', shared=True)
        view = snapshot(self.directory, self.settings)
        title, color, elements = status_content(view)
        connection = view.get('source_connection', {})
        if connection.get('state') in ('pending', 'declined'):
            elements.append(self.buttons(self.button('确认相机', {'panel': 'source_confirmation'})))
        elements.append(self.buttons(self.button('刷新状态', {'panel': 'status', 'refresh': True})))
        return card(title, elements, color, shared=True)

    def source_panel(self, values, page='source_confirmation'):
        from .source_connection import read, fresh
        state = read(self.directory)
        device = state.get('device', {})
        if page == 'source_offline':
            return card('相机连接不可用', [paragraph('USB 相机暂时无法访问。完整缓存按当前同步设置继续归档；重新连接后需要再次确认。'),
                                         note(format_time(now()))], 'grey')
        if not fresh(state):
            return card('等待相机连接', [paragraph('请连接并开启 USB 相机。识别成功后将发送确认卡，确认前不导入或删除素材。')], 'blue')
        support = device['compatibility']
        elements = [paragraph(device['model_name'] + '\n序列号：' + device['serial_number'] +
                              '\n固件：' + (device.get('firmware_version') or '未知')),
                    markdown('**兼容状态：' + support['validation'] + '**'), paragraph(support['reason'])]
        deleting = values['backup']['delete_source_after_import'] and support['delete_verified']
        elements.append(note('本次原片策略：' + ('完整导入后自动删除，不等待远端归档。' if deleting else '保留相机原片。')))
        if support['import_supported'] and state.get('state') != 'approved':
            buttons = [self.button('暂不备份', {'source_decision': False, 'session_id': state['id']}),
                       self.button('确认并备份', {'source_decision': True, 'session_id': state['id']})]
            buttons[-1]['type'] = 'primary'
            elements.append(self.buttons(*buttons))
        elements.append(note('本次确认仅适用于当前连接；检测到断开或服务重启后需重新确认。'))
        return card('相机已确认' if state.get('state') == 'approved' else
                    '相机待确认' if support['import_supported'] else '相机尚未适配', elements, 'blue', shared=True)

    def announce_source(self):
        from .source_connection import read, fresh
        state = read(self.directory)
        event = (state.get('id'), state.get('state'))
        if not state.get('id') or event == self._source_event:
            return
        if state.get('state') == 'pending' and fresh(state):
            self.enqueue('source:confirm:' + state['id'], {'panel': 'source_confirmation', 'session_id': state['id']})
        elif state.get('state') == 'offline' and (self.settings.read().get('config') or {}).get('notifications', {}).get('connection_events'):
            self.enqueue('source:offline:' + state['id'], {'panel': 'source_offline', 'session_id': state['id']})
        self._source_event = event

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
            "请选择底部「状态」「同步」或「原片处理」菜单。其他设置请在本机配置文件中修改。"}))
        if "changes" in action:
            action["revision"] = self.settings.read()["revision"]
            try:
                if abs(time.time() - int(message["create_time"]) / 1000) > 300:
                    action = {"panel": "status", "notice": {"title": "指令已过期，未执行", "level": "warning",
                                                             "detail": "请通过「同步」菜单重新提交指令。"}}
            except (KeyError, ValueError, TypeError):
                return False
        self.enqueue("message:" + header["event_id"], action)
        return True

    def receive_card(self, data):
        header, event = data.get("header", {}), data.get("event", {})
        if header.get("app_id") != self.credentials["app_id"] or event.get("operator", {}).get("open_id") != self.owner:
            return {"toast": {"type": "error", "content": "仅授权账号可执行此操作"}}
        token = event.get("action", {}).get("value", {}).get("token")
        event_id = header.get("event_id") or event.get("token")
        if not isinstance(token, str) or not isinstance(event_id, str):
            return {"toast": {"type": "error", "content": "操作无效，请通过菜单重新打开卡片"}}
        with self.db() as db:
            row = db.execute("SELECT * FROM bot_actions WHERE token=? AND scope=?", (token, self.scope)).fetchone()
        if row is None or time.time() - row["created"] > 86400:
            return {"toast": {"type": "error", "content": "卡片已过期，请通过菜单重新打开"}}
        payload = json.loads(row["payload"])
        if not self.supported(payload):
            return {"toast": {"type": "error", "content": "此操作已停用，请通过菜单重新打开卡片"}}
        payload["revision"] = row["revision"]
        context_id = event.get('context', {}).get('open_message_id')
        if isinstance(context_id, str) and re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}', context_id):
            payload['response_message_id'] = context_id
        if payload.get('refresh'):
            message_id = event.get('context', {}).get('open_message_id')
            if (payload.get('panel') not in STATUS_PANELS or not isinstance(message_id, str) or
                    not re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}', message_id)):
                return {'toast': {'type': 'error', 'content': '原消息不可用，请通过状态菜单重新打开'}}
            payload['message_id'] = message_id
        self.enqueue("card:" + event_id, payload)
        return {"toast": {"type": "info", "content": "正在更新状态" if payload.get('refresh') else "操作已提交，正在处理"}}

    def process_one(self):
        with self.db() as db:
            job = db.execute("SELECT * FROM bot_jobs WHERE scope=? AND state IN ('queued','applied','waiting') AND retry_at<=? ORDER BY created LIMIT 1",
                             (self.scope, time.time())).fetchone()
        if job is None:
            return False
        try:
            payload = json.loads(job['payload'])
            if payload.get('panel') in ('source_confirmation', 'source_offline') and payload.get('session_id'):
                from .source_connection import read
                connection = read(self.directory)
                expected = 'pending' if payload['panel'] == 'source_confirmation' else 'offline'
                if connection.get('id') != payload['session_id'] or connection.get('state') != expected:
                    with self.db() as db:
                        db.execute("UPDATE bot_jobs SET state='done' WHERE id=?", (job['id'],))
                    return True
            if not self.supported(payload):
                response = self.panel(notice={'title': '此操作已停用', 'detail': '请通过菜单重新打开卡片；其他设置在本机配置文件中修改。', 'level': 'info'})
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
                notice = {'title': '设置已变更', 'detail': '本次回执已失效，请查看当前状态。', 'level': 'info'} if obsolete else settings_result(changes, True, False)
                response = self.panel(payload.get('page', payload.get('form', 'status')), notice)
                payload['wait_for_apply'] = False
                with self.db() as db:
                    db.execute("UPDATE bot_jobs SET state='applied',response=?,payload=? WHERE id=?", (json.dumps(response, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), job['id']))
            if job["state"] == "queued":
                page, notice = payload.get("panel", payload.get("page", "status")), payload.get("notice")
                try:
                    changes = payload.get("changes")
                    if 'source_decision' in payload:
                        from .source_connection import decide
                        if time.time() - job['created'] > 300:
                            raise ConfigError('确认请求已过期，请重新获取相机确认卡')
                        store = Store(self.directory)
                        try:
                            changed = decide(store, self.settings, payload['session_id'], payload['source_decision'])
                        finally:
                            store.close()
                        notice = {'title': '相机已确认' if payload['source_decision'] else '本次连接暂不备份',
                                  'detail': '同步已开启，等待服务核对当前设备后开始导入。' if payload['source_decision'] and changed else
                                            '本次连接已确认；同步仍按当前开关设置运行。' if payload['source_decision'] else
                                            '相机素材保留，可通过状态菜单重新确认。', 'level': 'success' if payload['source_decision'] else 'info'}
                    if changes == {'backup': {'enabled': True}}:
                        from .source_connection import read, fresh
                        connection = read(self.directory)
                        if connection and (connection.get('state') != 'approved' or not fresh(connection)):
                            page, changes = 'source_confirmation', None
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
                    notice = {"title": "相机确认未完成" if 'source_decision' in payload else "本次修改未保存", "detail": str(exc), "level": "error"}
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
        heartbeat_at, connected, source_check_at = 0, None, 0
        try:
            while not stop.is_set():
                online = client._conn is not None
                if time.monotonic() - heartbeat_at >= 5 or connected != online:
                    db.set_meta("bot", {"heartbeat_at": now(), "connected": online})
                    heartbeat_at, connected = time.monotonic(), online
                bot.process_one()
                if time.monotonic() - source_check_at >= 2:
                    bot.announce_source()
                    source_check_at = time.monotonic()
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
