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
from .bot_cards import (archive_content, card, columns, device_content, feedback, format_time, markdown,
                        note, paragraph, plain, settings_result, status_content)
from .config import ConfigError
from .model import now
from .status import snapshot, age
from .store import Store
from .media import MEDIA_SCOPES


STATUS_PANELS = {'status': status_content, 'devices': device_content, 'archives': archive_content,
                 'device_details': lambda view: device_content(view, details=True),
                 'archive_details': lambda view: archive_content(view, details=True)}


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
            from .operations import initialize
            initialize(db)
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

    def panel(self, page="status", notice=None, operation_id=None, offset=0):
        if page == 'maintenance_result':
            return self.maintenance_panel(operation_id)
        item = self.settings.read()
        if item["error"]:
            return card("配置需要处理", [markdown("**当前配置无法读取，控制操作暂不可用。**"),
                                       paragraph(item["error"]), note("请在本机检查 .env 和 settings.json，再从底部菜单重试。")], "red")
        if notice and page not in ('transfer', 'notifications', 'cleanup'):
            if isinstance(notice, str):
                notice = {'title': '操作提示', 'detail': notice, 'level': 'info'}
            color = {'error': 'red', 'warning': 'orange', 'success': 'green', 'info': 'blue'}[notice['level']]
            return card(notice['title'], ([paragraph(notice['detail'])] if notice.get('detail') else []) +
                        [self.buttons(self.button('查看状态', {'panel': 'status'}))], color, shared=True)
        values, rev = item["config"], item["revision"]
        elements = feedback(notice) if notice else []
        title = {"transfer": "传输设置", "notifications": "通知设置", "cleanup": "原片处理",
                 "confirm_delete": "确认开启原片清理", "help": "使用指南"}.get(page, "运行总览")
        color = "orange" if page == "confirm_delete" else "blue"
        if page == "transfer":
            limits = values["limits"]
            scope = values["backup"]["media_scope"]
            transport = values["source"]["transport"]
            modes = {"usb": "USB 直连", "wifi": "手动 Wi-Fi"}
            fields = [("upload_mib_per_second", "上传限速 · MiB/s", limits["upload_mib_per_second"]),
                      ("max_cache_gib", "缓存额度 · GiB", limits["max_cache_gib"]),
                      ("min_free_disk_gib", "磁盘预留 · GiB", limits["min_free_disk_gib"])]
            elements += [markdown("🔌 **连接方式 · " + modes[transport] + "**"),
                         self.buttons(*(self.button("切换为" + label,
                             {"changes": {"backup": {"source_transport": key}}, "page": page}, rev)
                             for key, label in modes.items() if key != transport)),
                         note("切换前先停止同步。USB 插线开机；Wi-Fi 需在电脑手动连接相机热点。"),
                         {"tag": "hr"}, markdown("🎞️ **同步内容 · " + MEDIA_SCOPES[scope] + "**"),
                         self.buttons(*(self.button(label, {"changes": {"backup": {"media_scope": key}}, "page": page}, rev)
                                        for key, label in MEDIA_SCOPES.items() if key != scope)),
                         note("照片包含 RAW；独立音频等随关联素材导入。其他文件请选择「全部素材」。"),
                         note("已有完整缓存继续上传；未选类别原片保留。"),
                         {"tag": "hr"}, markdown("**归档预览文件 · " + ("开启**" if values['backup']['archive_previews'] else "关闭**")),
                         self.buttons(self.button(("关闭" if values['backup']['archive_previews'] else "开启") + "归档预览文件",
                             {"changes": {"backup": {"archive_previews": not values['backup']['archive_previews']}}, "page": page}, rev)),
                         note("控制已确认关联的 LRV、THM；孤立或未知文件仍保留。新一轮生效，已登记任务按原规则完成。"),
                         {"tag": "hr"}, markdown("**控制上传速度与缓存占用**"),
                         note("上传限速、缓存额度填 0 表示不限；磁盘预留用于保留可用空间。"),
                         self.form("transfer", fields, rev), {"tag": "hr"},
                         note("保存后自动应用；中断文件从头重传，完整缓存保留。")]
        elif page == "notifications":
            opts = values["notifications"]
            elements += [markdown("📷 **连接提醒已" + ("开启**" if opts["connection_events"] else "关闭**")),
                         note("连接变化时提醒，不受同步开关影响。"),
                         self.buttons(self.button(("关闭" if opts["connection_events"] else "开启") + "连接提醒",
                             {"changes": {"notifications": {"connection_events": not opts["connection_events"]}}, "page": page}, rev)),
                         {"tag": "hr"}, markdown("**传输通知已" + ("开启**" if opts["enabled"] else "关闭**")),
                         note("进度更新同一张卡；可拔相机、归档完成、持续故障和缓存积压单独提醒。"),
                         self.buttons(self.button(("关闭" if opts["enabled"] else "开启") + "传输通知",
                                                  {"changes": {"notifications": {"enabled": not opts["enabled"]}}, "page": page}, rev)),
                         {"tag": "hr"},
                         note("进度间隔 60–86400 秒；300 秒为 5 分钟。完成和故障即时提醒。"),
                         self.form("notifications", [("progress_interval_seconds", "进度更新间隔 · 秒", opts["progress_interval_seconds"]),
                                                      ("keyword", "消息前缀 · 可留空", opts["keyword"])], rev)]
        elif page == "cleanup":
            b = values["backup"]
            elements += [columns("**相机原片**\n" + ("完整导入后删除" if b["delete_source_after_import"] else "保留，不自动删除"),
                                 "**本地完整缓存**\n正式归档后自动清理"), {"tag": "hr"},
                         paragraph("整组需要归档的素材完整导入、核对后才删除；忽略的预览文件会随原片清理。未选类别原片保留。"),
                         note("同步关闭时停止删除；未归档的完整缓存保留。")]
            action = {"changes": {"backup": {"delete_source_after_import": False}}, "page": page} if b["delete_source_after_import"] else {"panel": "confirm_delete"}
            elements += [self.buttons(self.button("改为保留原片" if b["delete_source_after_import"] else "开启导入后删除", action, rev))]
        elif page == "confirm_delete":
            elements += [markdown("**远端归档完成前，缓存盘可能是唯一副本。**"),
                         paragraph("所选素材完整导入并核对后立即删除原片，不等待远端上传；已有合格缓存也适用。未选类别原片保留。"),
                         note("已选择不归档的预览文件会随原片清理，远端不会保留这些预览副本。"),
                         note("上传失败保留缓存；素材关联或身份不确定时保留原片。"),
                         {"tag": "hr"},
                         self.buttons(self.button("取消，保持原设置", {"panel": "cleanup"}),
                                      self.button("确认开启导入后删除", {"changes": {"backup": {"delete_source_after_import": True}}, "page": "cleanup"}, rev, danger=True))]
        elif page == "help":
            elements += [paragraph("状态：进度、设备与存储、归档结果\n同步：开启或停止传输\n设置：连接方式、同步类别、原片处理与通知"),
                         note("菜单即可操作，无需记住命令。停止同步会保留任务和完整缓存。")]
        elif page == 'issues':
            view = snapshot(self.directory, self.settings)
            entries = list(view['issues']) + [{'path': '未完成临时文件', 'error': x.get('error') or '等待清理'} for x in view['temporary_cleanup']]
            offset = max(0, min(int(offset), max(0, (len(entries) - 1) // 5 * 5)))
            title = '异常与待处理事项'
            elements += [markdown(f"**共 {len(entries)} 项** · 第 {offset // 5 + 1} 页")]
            for entry in entries[offset:offset + 5]:
                reason = entry.get('cache_reason') if entry.get('cache_state') in ('unverified', 'missing') else (
                    entry.get('error') or entry.get('source_cleanup_error') or entry.get('cache_cleanup_error'))
                elements += [paragraph(entry['path']), paragraph(str(reason or '等待重新检查')[:600]), {'tag': 'hr'}]
            navigation = []
            for label, target in (('上一页', offset - 5), ('下一页', offset + 5)):
                if 0 <= target < len(entries):
                    navigation.append(self.button(label, {'panel': 'issues', 'offset': target}))
            if navigation:
                elements.append(self.buttons(*navigation))
            elements += self.problem_actions(view, rev)
        else:
            page = page if page in STATUS_PANELS else 'status'
            view = snapshot(self.directory, self.settings)
            title, color, content = STATUS_PANELS[page](view)
            elements += content
            actions = []
            if page == 'status' and values['backup']['enabled']:
                if values['backup'].get('stop_after_current'):
                    actions += [self.button('继续同步', {'changes': {'backup': {'enabled': True}}}, rev),
                                self.button('立即停止', {'changes': {'backup': {'enabled': False}}}, rev)]
                else:
                    actions.append(self.button('传完当前文件再停', {'changes': {'backup': {'stop_after_current': True}}}, rev))
            if page == 'devices':
                actions += [self.button('连接详情', {'panel': 'device_details'}), self.button('检查连接', {'operation': 'check'}, rev)]
            elif page == 'device_details':
                actions += [self.button('返回概览', {'panel': 'devices'}), self.button('检查连接', {'operation': 'check'}, rev)]
            elif page == 'archives':
                actions.append(self.button('文件详情', {'panel': 'archive_details'}))
            elif page == 'archive_details':
                actions.append(self.button('返回结果', {'panel': 'archives'}))
            if len(actions) < 2 and (view['attention']['required'] or view['issues'] or view['temporary_cleanup']):
                actions.append(self.button('查看待处理', {'panel': 'issues'}))
            actions.append(self.button('刷新', {'panel': page, 'refresh': True}))
            elements.append(self.buttons(*actions))
        if isinstance(notice, dict) and notice["level"] == "error":
            color = "red"
            title += " · 未执行" if notice.get('operation') else " · 未保存"
        elif isinstance(notice, dict) and notice["level"] == "warning":
            color = "orange"
        return card(title, elements, color, shared=True)

    def problem_actions(self, view, revision):
        actions = [self.button('重新检查连接', {'operation': 'check'}, revision)]
        if view.get('issues') or view.get('temporary_cleanup'):
            actions.append(self.button('查看待处理', {'panel': 'issues'}))
        if any(f.get('cache_state') == 'unverified' for f in view.get('files', [])):
            actions.append(self.button('核验缓存', {'operation': 'verify_cache'}, revision))
        return [self.buttons(*actions)]

    def maintenance_panel(self, identifier):
        with self.db() as db:
            row = db.execute('SELECT * FROM maintenance_requests WHERE id=?', (identifier,)).fetchone()
        if row is None:
            return card('检查记录不可用', [paragraph('请从设备与存储重新发起检查。')], 'orange')
        result = json.loads(row['result'] or '{}')
        labels = {'configuration': '配置', 'database': '任务记录', 'cache': '缓存盘',
                  'source': '相机', 'destination': 'SMB', 'notification': '飞书'}
        states = {'ok': '通过', 'waiting': '等待处理', 'error': '需要处理'}
        elements = []
        checks = result.get('checks', {})
        passed = sum(x['state'] == 'ok' for x in checks.values())
        if checks:
            elements.append(paragraph(f"{passed} / {len(checks)} 项通过"))
        if result.get('message'):
            elements.append(paragraph(result['message']))
        for name, check in checks.items():
            if check['state'] != 'ok':
                elements += [markdown('**' + labels.get(name, name) + ' · ' + states.get(check['state'], '未知') + '**'), paragraph(check['message'])]
        if 'verified' in result:
            elements.append(paragraph(f"已核验 {len(result['verified'])} 个 · 待处理 {len(result['pending'])} 个"))
            for item in result['pending'][:3]:
                elements.append(paragraph(item['path'] + '\n' + item['reason']))
        actions = [self.button('查看当前状态', {'panel': 'status'})]
        if result.get('pending'):
            actions.append(self.button('查看待处理', {'panel': 'issues'}))
        elements += [note('检查结束 · ' + format_time(row['updated_at'])), self.buttons(*actions)]
        return card(('缓存核验结果' if row['kind'] == 'verify_cache' else '连接检查结果') + ' · ' + states.get(result.get('state'), '检查中'), elements,
                    'green' if result.get('state') == 'ok' else 'orange', shared=True)

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
        commands = {"状态": {"panel": "status"}, "进度": {"panel": "status"}, "运行总览": {"panel": "status"},
                    "设备与存储": {"panel": "devices"}, "归档结果": {"panel": "archives"},
                    "帮助": {"panel": "help"}, "使用帮助": {"panel": "help"},
                    "设置": {"panel": "transfer"}, "传输设置": {"panel": "transfer"},
                    "清理设置": {"panel": "cleanup"}, "原片处理": {"panel": "cleanup"}, "通知设置": {"panel": "notifications"},
                    "开启同步": {"changes": {"backup": {"enabled": True}}},
                    "停止同步": {"changes": {"backup": {"enabled": False}}},
                    "传完再停": {"changes": {"backup": {"stop_after_current": True}}},
                    "关闭同步": {"changes": {"backup": {"enabled": False}}}}
        action = dict(commands.get(command, {"panel": "help", "notice": "请使用底部的「状态」「同步」「设置」菜单。"}))
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
        context_id = event.get('context', {}).get('open_message_id')
        if isinstance(context_id, str) and re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}', context_id):
            payload['response_message_id'] = context_id
        if payload.get('refresh'):
            message_id = event.get('context', {}).get('open_message_id')
            if (payload.get('panel') not in STATUS_PANELS or not isinstance(message_id, str) or
                    not re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}', message_id)):
                return {'toast': {'type': 'error', 'content': '无法定位原消息，请从状态菜单重新获取卡片'}}
            payload['message_id'] = message_id
        if "form" in payload:
            payload["values"] = event.get("action", {}).get("form_value", {})
            if len(json.dumps(payload["values"], ensure_ascii=False)) > 2048:
                return {"toast": {"type": "error", "content": "输入内容过长"}}
        self.enqueue("card:" + event_id, payload)
        return {"toast": {"type": "info", "content": "正在刷新此卡片" if payload.get('refresh') else "已收到，正在处理"}}

    def process_one(self):
        with self.db() as db:
            for result in db.execute("SELECT id FROM maintenance_requests WHERE state='done'").fetchall():
                saved = db.execute('SELECT value FROM meta WHERE key=?', ('bot_reply:' + self.scope + ':' + result['id'],)).fetchone()
                receipt = json.loads(saved[0]) if saved else {}
                db.execute("INSERT OR IGNORE INTO bot_jobs (id,scope,payload,created) VALUES (?,?,?,?)",
                           ('maintenance-result:' + result['id'], self.scope,
                            json.dumps({'panel': 'maintenance_result', 'operation_id': result['id'],
                                        'response_message_id': receipt.get('id')}), time.time()))
                db.execute("UPDATE maintenance_requests SET state='reported' WHERE id=?", (result['id'],))
            job = db.execute("SELECT * FROM bot_jobs WHERE scope=? AND state IN ('queued','applied','waiting') AND retry_at<=? ORDER BY created LIMIT 1",
                             (self.scope, time.time())).fetchone()
        if job is None:
            return False
        try:
            payload = json.loads(job['payload'])
            waiting = False
            if job['state'] == 'waiting':
                view = snapshot(self.directory, self.settings)
                changes = payload['changes_applied']
                revision = payload['applied_revision']
                stopping = any(c['state'] == 'pausing' for c in view['controls'].values())
                item = self.settings.read()
                drained = changes.get('backup', {}).get('stop_after_current') and item.get('config') and not item['config']['backup']['enabled']
                if drained:
                    changes = {'backup': {'enabled': False}}
                    revision = item['revision']
                obsolete = bool(item.get('error') or item['revision'] != revision)
                ready = view['running'] and not view['configuration'].get('pending') and view['configuration'].get('applied_revision') == revision and not stopping
                if changes.get('backup', {}).get('stop_after_current') and not obsolete:
                    ready = False
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
                    if payload.get('operation'):
                        if time.time() - job['created'] > 300:
                            raise ConfigError('检查请求已经过期，请重新操作')
                        if payload['operation'] == 'verify_cache':
                            saved = self.settings.read()
                            if not saved.get('config') or saved['revision'] != payload.get('revision'):
                                raise ConfigError('配置已变化或无效，请重新获取状态卡片后核验')
                            if saved['config']['backup']['enabled']:
                                raise ConfigError('请先停止同步，等待两路停止后再核验缓存；文件与原片保留')
                        from .operations import enqueue
                        with self.db() as db:
                            enqueue(db, job['id'], payload['operation'], self.settings.token()[1])
                        notice = {'title': '正在核验缓存' if payload['operation'] == 'verify_cache' else '正在检查连接', 'detail': '完成后更新此消息。', 'level': 'info'}
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
                        waiting = not applied or stopping or bool(changes.get('backup', {}).get('stop_after_current'))
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
                    notice = {"title": "检查未执行" if payload.get('operation') else "本次修改未保存", "detail": str(exc),
                              "level": "error", "operation": bool(payload.get('operation'))}
                response = self.panel(page, notice, payload.get('operation_id'), payload.get('offset', 0))
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
