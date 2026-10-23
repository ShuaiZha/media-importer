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
                        settings_result, status_content, detail_content, cleanup_text, format_time)
from .config import ConfigError
from .model import now
from .status import snapshot, age
from .store import Store


STATUS_PANELS = {'status': status_content, 'details': detail_content}


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
            from .camera_operations import initialize
            initialize(db)
            from .selected_imports import initialize as initialize_selected
            initialize_selected(db)
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
        """Accept only the explicitly supported, versioned settings controls."""
        if payload.get('form') or payload.get('operation'):
            return False
        if 'live_preview' in payload:
            from .preview_cards import supported
            return supported(payload['live_preview']) and not payload.get('changes')
        if 'capture' in payload:
            from .capture_cards import supported
            return supported(payload['capture']) and not payload.get('changes')
        if 'camera' in payload:
            from .camera_cards import supported
            return supported(payload['camera']) and not payload.get('changes')
        if 'selected_import' in payload:
            from .selected_cards import supported
            return supported(payload['selected_import']) and not payload.get('changes')
        from .bot_settings import FORMS, PAGES, LIMIT_FIELDS, INTERVAL_FIELDS
        modern = payload.get('settings_version') in (1, 2)
        if payload.get('settings_version') == 4:
            if 'settings_form' in payload:
                return payload['settings_form'] in ('notification_settings', 'notification_advanced')
            changes = payload.get('changes', payload.get('changes_applied'))
            return (isinstance(changes, dict) and set(changes) == {'notifications'} and
                    isinstance(changes['notifications'], dict) and set(changes['notifications']) in
                    ({'enabled', 'progress_interval_seconds'}, {'connection_events', 'keyword'}))
        if 'settings_form' in payload:
            return ((modern and payload['settings_form'] in FORMS) or
                    (payload.get('settings_version') == 3 and payload['settings_form'] == 'transfer_settings'))
        if 'archive_change' in payload or 'archive_draft' in payload:
            return modern and payload.get('mode') in ('connection', 'new')
        if payload.get('panel', 'status') not in ('status', 'details', 'stop_options', 'event_details', 'cleanup', 'confirm_delete', 'source_confirmation', 'source_offline', 'source_online',
                                                  'notifications', 'previews', 'confirm_previews', 'scope', *PAGES):
            return False
        if 'source_decision' in payload:
            return (type(payload['source_decision']) is bool and
                    isinstance(payload.get('session_id'), str) and
                    re.fullmatch(r'[0-9a-f]{32}', payload['session_id']) is not None and not payload.get('changes'))
        changes = payload.get('changes', payload.get('changes_applied'))
        if changes is None:
            return True
        if payload.get('settings_version') == 3:
            return (isinstance(changes, dict) and set(changes) == {'limits', 'backup'} and
                    isinstance(changes['limits'], dict) and set(changes['limits']) == LIMIT_FIELDS and
                    isinstance(changes['backup'], dict) and set(changes['backup']) == INTERVAL_FIELDS)
        if modern:
            if not isinstance(changes, dict) or len(changes) != 1:
                return False
            group, updates = next(iter(changes.items()))
            if group == 'backup' and isinstance(updates, dict) and set(updates) == {'stop_after_current'}:
                return payload['settings_version'] == 2 and type(updates['stop_after_current']) is bool
            allowed = {'livestream': {'ssid', 'credential_id', 'port'}, 'preview': {'public_url'}, 'limits': {'upload_mib_per_second', 'max_cache_gib', 'min_free_disk_gib'},
                       'notifications': {'enabled', 'connection_events', 'progress_interval_seconds', 'keyword'},
                       'source_options': {'url', 'serial', 'timeout_seconds', 'request_attempts'},
                       'backup': {'poll_seconds', 'retry_seconds', 'stability_seconds'}}
            if group == 'archive':
                from .archive_settings import FIELDS, EXTRA_FIELDS
                return (isinstance(updates, dict) and set(FIELDS).issubset(updates)
                        and not set(updates) - set(FIELDS) - set(EXTRA_FIELDS))
            return group in allowed and isinstance(updates, dict) and set(updates) == allowed[group]
        if not isinstance(changes, dict) or len(changes) != 1:
            return False
        group, updates = next(iter(changes.items()))
        if not isinstance(updates, dict) or len(updates) != 1:
            return False
        key, value = next(iter(updates.items()))
        if group == 'backup' and key in ('enabled', 'delete_source_after_import', 'archive_previews'):
            return type(value) is bool
        if group == 'backup' and key == 'source_transport':
            return value in ('auto', 'usb', 'wifi')
        if group == 'backup' and key == 'media_scope':
            return value in ('all', 'video', 'photo')
        return group == 'notifications' and key == 'enabled' and type(value) is bool

    def panel(self, page="status", notice=None):
        item = self.settings.read()
        if item["error"]:
            return card("配置异常", [paragraph(item["error"]),
                         note("请在本机检查 .env 和 settings.json，然后刷新状态。")], "red")
        if notice:
            if isinstance(notice, str):
                notice = {'title': '操作说明', 'detail': notice, 'level': 'info'}
            color = {'error': 'red', 'warning': 'orange', 'success': 'green', 'info': 'blue'}[notice['level']]
            elements = [paragraph(notice['detail'])] if notice.get('detail') else []
            if notice['title'] in ('同步已停止', '正在停止同步'):
                view = snapshot(self.directory, self.settings)
                pending = view.get('work', {}).get('pending_files', 0)
                if pending: elements.append(paragraph(f'剩余 {pending} 个文件'))
            if notice['level'] == 'error':
                elements.append(self.buttons(self.button('重新打开', {'panel': page})))
            return card(notice['title'], elements, color, shared=True)
        values, rev = item["config"], item["revision"]
        from .bot_settings import PAGES, panel as settings_panel
        if page in PAGES:
            return settings_panel(self, page, item)
        if page in ('source_confirmation', 'source_offline', 'source_online'):
            return self.source_panel(values, page)
        if page == 'stop_options':
            return card('停止同步', [self.buttons(
                self.button('立即停止', {'changes': {'backup': {'enabled': False}}}),
                self.button('当前文件完成后停止', {'changes': {'backup': {'stop_after_current': True}}, 'settings_version': 2}, rev))], shared=True)
        if page == 'confirm_delete':
            return card('启用自动删除原片', [
                markdown('**原片将在本地导入与核验完成后删除，不等待远端归档。**'),
                paragraph('远端归档完成前，本地缓存可能是唯一副本。'),
                self.buttons(self.button('取消', {'panel': 'cleanup'}), self.button('确认启用',
                    {'changes': {'backup': {'delete_source_after_import': True}}, 'page': 'cleanup'}, rev, danger=True))
            ], 'orange', shared=True)
        if page == 'notifications':
            enabled = values['notifications']['enabled']
            return card('自动通知', [
                markdown('**自动通知：' + ('已开启' if enabled else '已关闭') + '**'),
                self.buttons(self.button('关闭自动通知' if enabled else '开启自动通知',
                    {'changes': {'notifications': {'enabled': not enabled}}, 'page': 'notifications'}, rev))
            ], 'blue', shared=True)
        if page == 'confirm_previews':
            page = 'previews'  # Old confirmation cards reopen the ordinary setting.
        if page == 'previews':
            enabled = values['backup']['archive_previews']
            action = {'changes': {'backup': {'archive_previews': not enabled}}, 'page': 'previews'}
            return card('预览文件', [
                markdown('**归档预览：' + ('已开启' if enabled else '已关闭') + '**'),
                note('关联 LRV、THM；修改在下轮生效。'),
                self.buttons(self.button('改为不归档预览' if enabled else '归档预览文件', action, rev))
            ], 'blue', shared=True)
        if page == 'scope':
            from .media import MEDIA_SCOPES
            current = values['backup']['media_scope']
            return card('素材范围', [
                markdown('**当前范围：' + MEDIA_SCOPES[current] + '**'),
                self.buttons(*(self.button(label, {'changes': {'backup': {'media_scope': key}}, 'page': 'scope'}, rev)
                               for key, label in MEDIA_SCOPES.items()))
            ], 'blue', shared=True)
        if page == 'cleanup':
            enabled = values['backup']['delete_source_after_import']
            from .source_connection import read, deletion_enabled
            from .config import Config
            effective = deletion_enabled(Config.from_values(values), read(self.directory).get('device') or read(self.directory, 'source_selection'))
            action = {'changes': {'backup': {'delete_source_after_import': False}}, 'page': 'cleanup'} if enabled else {'panel': 'confirm_delete'}
            return card('原片处理', [
                markdown('**自动删除：' + ('已启用' if enabled else '已停用') + '**'),
                paragraph('当前设备未通过删除验收，保留原片；配置开关保持不变。' if enabled and not effective else
                          '整组素材完成本地导入与核验后，自动删除相机原片。' if enabled else '导入完成后保留相机原片。'),
                self.buttons(self.button('停用自动删除' if enabled else '启用自动删除', action, rev))
            ], 'blue', shared=True)
        view = snapshot(self.directory, self.settings)
        title, color, elements = STATUS_PANELS.get(page, status_content)(view)
        connection = view.get('source_connection', {})
        if connection.get('state') in ('pending', 'declined'):
            elements.append(self.buttons(self.button('确认相机', {'panel': 'source_confirmation'})))
        elements += self.progress_actions(view, detail=page == 'details')
        return card(title, elements, color, shared=True)

    def source_panel(self, values, page='source_confirmation'):
        from .source_connection import read, fresh
        state = read(self.directory)
        device = state.get('device', {})
        if page == 'source_offline':
            return card('相机已断开', [], 'grey')
        if not fresh(state):
            return card('相机未连接', [paragraph('请连接并开启相机。')], 'blue')
        if page == 'source_online':
            return card('相机已连接', [paragraph(device['model_name'] + ' · ' +
                {'usb': 'USB', 'wifi': 'Wi-Fi'}.get(device.get('transport'), '未知'))], 'green')
        support = device['compatibility']
        elements = [paragraph(device['model_name'] + '\n序列号：' + device['serial_number'])]
        if not support.get('delete_verified'):
            elements.append(paragraph(support['reason']))
        if support['import_supported'] and state.get('state') != 'approved':
            buttons = [self.button('暂不确认', {'source_decision': False, 'session_id': state['id']}),
                       self.button('确认相机', {'source_decision': True, 'session_id': state['id']})]
            buttons[-1]['type'] = 'primary'
            elements.append(self.buttons(*buttons))
        return card('相机已确认' if state.get('state') == 'approved' else
                    '确认相机' if support['import_supported'] else '相机尚未适配', elements, 'blue', shared=True)

    def announce_source(self):
        from .source_connection import read, fresh
        state = read(self.directory)
        event = (state.get('id'), state.get('state'), state.get('device', {}).get('http_ready', True))
        if not state.get('id') or event == self._source_event:
            return
        if state.get('state') == 'pending' and fresh(state):
            self.enqueue('source:confirm:' + state['id'], {'panel': 'source_confirmation', 'session_id': state['id']})
        elif state.get('state') == 'approved' and fresh(state) and state.get('device', {}).get('http_ready', True) and (self.settings.read().get('config') or {}).get('notifications', {}).get('connection_events'):
            self.enqueue('source:online:' + state['id'], {'panel': 'source_online', 'session_id': state['id']})
        elif state.get('state') == 'offline' and (self.settings.read().get('config') or {}).get('notifications', {}).get('connection_events'):
            self.enqueue('source:offline:' + state['id'], {'panel': 'source_offline', 'session_id': state['id']})
        self._source_event = event

    def progress_actions(self, view, *, detail=False):
        actions = [self.button('返回摘要' if detail else '查看详情', {'panel': 'status' if detail else 'details'}),
                   self.button('刷新', {'panel': 'details' if detail else 'status', 'refresh': True})]
        if view.get('transfer_enabled', view.get('configuration', {}).get('sync_enabled')):
            actions.append(self.button('停止同步', {'panel': 'stop_options'}))
        return [self.buttons(*actions)]

    def active_progress_message(self):
        from .notifications import _digest
        recipient = _digest([self.credentials['app_id'], self.credentials['receive_id_type'], self.credentials['receive_id']])
        with self.db() as db:
            values = [json.loads(r[0]) for r in db.execute("SELECT value FROM meta WHERE key LIKE 'notification_progress_card:%'")]
        candidates = [p for p in values if p.get('recipient') == recipient and p.get('message_id') and not p.get('finished')]
        return max(candidates, key=lambda p: p.get('updated_at', 0))['message_id'] if candidates else None

    def result_actions(self, result, locations):
        # Bounded snapshot in an expiring action token; no historical task table.
        return [self.buttons(self.button('查看详情', {'panel': 'event_details',
            'result_detail': {'result': result or {}, 'locations': (locations or [])[:8]}}))]

    def problem_actions(self, view, revision):
        actions = []
        codes = {(stage.get('issue') or {}).get('code') for stage in view.get('stages', {}).values()}
        if codes & {'authentication', 'permission', 'destination_path', 'destination_identity', 'destination_full'}:
            actions.append(self.button('归档存储', {'panel': 'destination_settings'}))
        elif codes & {'cache_unavailable', 'cache_verification', 'cache_cleanup'}:
            actions.append(self.button('本机目录', {'panel': 'local_storage'}))
        actions.append(self.button('查看详情', {'panel': 'details'}))
        return [self.buttons(*actions)]

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
        from .bot_routes import TEXT_ACTIONS as commands, HELP
        action = dict(commands.get(command, {'notice': {'title': '未识别的指令', 'detail': HELP, 'level': 'info'}}))
        if "changes" in action or 'live_preview' in action:
            action["revision"] = self.settings.read()["revision"]
            try:
                if abs(time.time() - int(message["create_time"]) / 1000) > 300:
                    action = {"panel": "status", "notice": {"title": "指令已过期，未执行", "level": "warning",
                                                             "detail": "请通过「同步」菜单重新提交指令。"}}
            except (KeyError, ValueError, TypeError):
                return False
        self.enqueue("message:" + header["event_id"], action)
        return True

    def receive_menu(self, data):
        from .bot_routes import MENU_ACTIONS
        header, event = data.get('header', {}), data.get('event', {})
        operator = event.get('operator', {}).get('operator_id', {})
        action = MENU_ACTIONS.get(event.get('event_key'))
        if (header.get('app_id') != self.credentials['app_id'] or operator.get('open_id') != self.owner or
                not isinstance(header.get('event_id'), str) or not action):
            return False
        try:
            stamp = float(event.get('timestamp') or header.get('create_time'))
            if stamp > 1e14: stamp /= 1e6
            elif stamp > 1e11: stamp /= 1e3
            if not abs(time.time() - stamp) <= 300: return False
        except (TypeError, ValueError):
            return False
        payload = json.loads(json.dumps(action))
        if 'changes' in payload or 'live_preview' in payload:
            payload['revision'] = self.settings.read()['revision']
        self.enqueue('menu:' + header['event_id'], payload)
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
        if payload.get('capture', {}).get('action') in ('apply_preset', 'apply_parameter'):
            from .capture_forms import parse
            try:
                payload['capture_parameters'] = parse(payload['capture'], event.get('action', {}).get('form_value'))
            except ConfigError as exc:
                return {'toast': {'type': 'error', 'content': str(exc)}}
        if payload.get('selected_import', {}).get('action') == 'choose':
            from .selected_cards import parse_choices
            try:
                payload['selected_indices'] = parse_choices(
                    event.get('action', {}).get('form_value'), payload['selected_import']['page'])
            except ConfigError as exc:
                return {'toast': {'type': 'error', 'content': str(exc)}}
        if payload.get('settings_form'):
            from .bot_settings import parse
            try:
                name = payload.pop('settings_form')
                parsed = parse(name, event.get('action', {}).get('form_value'), payload['settings_version'])
                payload['page'] = name
                if name == 'livestream_settings':
                    from .livestream_settings import draft
                    identifier = uuid.uuid5(uuid.NAMESPACE_URL, self.scope + ':live-network:' + event_id).hex
                    payload['changes'] = {'livestream': draft(self.settings, parsed, identifier, row['revision'])}
                elif name == 'destination_settings':
                    # Passwords bypass SQLite entirely. The durable request contains
                    # only a random local-file reference, checked again before commit.
                    password = parsed.pop('password', '')
                    change_id = uuid.uuid5(uuid.NAMESPACE_URL, self.scope + ':card:' + event_id).hex
                    with self.settings.lock():
                        item = self.settings.read()
                        from .archive_settings import _stopped, candidate, connection_revision
                        from .settings import Conflict
                        if (item['error'] or item['revision'] != row['revision'] or
                                connection_revision(item['config']) != payload['connection_revision']):
                            raise Conflict('设置已经变化，请重新打开归档存储')
                        with self.db() as db:
                            if db.execute('SELECT 1 FROM bot_jobs WHERE id=?', ('card:' + event_id,)).fetchone():
                                return {'toast': {'type': 'info', 'content': '操作已提交，请等待原回执'}}
                        store = Store(self.directory)
                        try:
                            _stopped(store, item)
                        finally:
                            store.close()
                        candidate(item['config'], {k: v for k, v in parsed.items() if k != 'mode'}, self.settings.path.parent)
                        if password:
                            from .credentials import stage
                            parsed['credential_id'] = stage(self.settings.path.parent, change_id, password)
                    payload.update(archive_draft={k: v for k, v in parsed.items() if k != 'mode'},
                                   mode=parsed['mode'], change_id=change_id)
                else:
                    payload['changes'] = parsed
            except ConfigError as exc:
                return {'toast': {'type': 'error', 'content': str(exc)}}
            except OSError:
                return {'toast': {'type': 'error', 'content': '本机配置目录不可写，设置未保存，请检查磁盘'}}
        context_id = event.get('context', {}).get('open_message_id')
        if isinstance(context_id, str) and re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}', context_id):
            payload['response_message_id'] = context_id
        if payload.get('refresh'):
            message_id = event.get('context', {}).get('open_message_id')
            if (payload.get('panel') not in STATUS_PANELS or not isinstance(message_id, str) or
                    not re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}', message_id)):
                return {'toast': {'type': 'error', 'content': '原消息不可用，请从底部菜单重新打开'}}
            payload['message_id'] = message_id
        mutating = bool(payload.get('changes') or 'source_decision' in payload or
                        payload.get('capture', {}).get('action') not in (None, 'panel', 'settings', 'parameter') or
                        payload.get('camera', {}).get('action') in ('confirm', 'cancel') or
                        payload.get('selected_import', {}).get('action') in ('confirm', 'cancel', 'drain', 'resume') or
                        payload.get('live_preview', {}).get('action') in ('open', 'stop'))
        # Form revisions/drafts have their own guarded submissions; do not persist secrets.
        payload['action_token'] = token if mutating and not event.get('action', {}).get('form_value') else None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if payload['action_token'] and db.execute("SELECT 1 FROM bot_jobs WHERE scope=? AND json_extract(payload,'$.action_token')=?", (self.scope, token)).fetchone():
                return {'toast': {'type': 'info', 'content': '操作已提交，请查看原回执'}}
            db.execute('INSERT OR IGNORE INTO bot_jobs (id,scope,payload,created) VALUES (?,?,?,?)',
                       ('card:' + event_id, self.scope, json.dumps(payload, ensure_ascii=False), time.time()))
        return {"toast": {"type": "info", "content": "正在更新状态" if payload.get('refresh') else "操作已提交，正在处理"}}

    def process_one(self):
        with self.db() as db:
            job = db.execute("SELECT * FROM bot_jobs WHERE scope=? AND state IN ('queued','applied','waiting') AND retry_at<=? ORDER BY created LIMIT 1",
                             (self.scope, time.time())).fetchone()
        if job is None:
            return False
        try:
            payload = json.loads(job['payload'])
            if 'live_preview' in payload and self.supported(payload):
                from .preview_cards import process_job
                return process_job(self, job, payload)
            # The existing stop menu also applies to the independent one-shot task.
            changes = payload.get('changes')
            if job['state'] == 'queued' and changes in (
                    {'backup': {'enabled': False}}, {'backup': {'stop_after_current': True}}):
                from .selected_imports import active
                with self.db() as db:
                    selected = active(db, self.scope)
                    if (selected and selected['state'] in ('queued_transfer', 'running', 'waiting')
                            and not self.settings.read()['config']['backup']['enabled']):
                        from .camera_operations import cancel
                        cancel(db, owner=self.scope)
                        payload = {**{k: v for k, v in payload.items() if k in ('response_message_id', 'message_id', 'action_token')},
                            'selected_import': {'version': 1, 'id': selected['id'],
                            'action': 'drain' if changes['backup'].get('stop_after_current') else 'cancel'}}
                        db.execute('UPDATE bot_jobs SET payload=? WHERE id=?',
                                   (json.dumps(payload), job['id']))
            if 'selected_import' in payload and self.supported(payload):
                from .selected_cards import process_job
                return process_job(self, job, payload)
            if 'capture' in payload and self.supported(payload):
                from .capture_cards import process_job
                return process_job(self, job, payload)
            if 'camera' in payload and self.supported(payload):
                from .camera_cards import process_job
                return process_job(self, job, payload)
            if payload.get('panel') in ('source_confirmation', 'source_offline', 'source_online') and payload.get('session_id'):
                from .source_connection import read, fresh
                connection = read(self.directory)
                expected = {'source_confirmation': 'pending', 'source_offline': 'offline', 'source_online': 'approved'}[payload['panel']]
                if (connection.get('id') != payload['session_id'] or connection.get('state') != expected or
                        expected != 'offline' and not fresh(connection) or
                        expected == 'approved' and not connection.get('device', {}).get('http_ready', True)):
                    with self.db() as db:
                        db.execute("UPDATE bot_jobs SET state='done' WHERE id=?", (job['id'],))
                    return True
            if not self.supported(payload):
                response = self.panel(notice={'title': '此操作已停用', 'detail': '请通过「设置」重新打开卡片。', 'level': 'info'})
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
                stopping = any(c['state'] == 'pausing' for c in view['controls'].values()) or view.get('source_operation_running', False)
                item = self.settings.read()
                # Completing a drain saves enabled=False, which naturally changes
                # the revision. Acknowledge the actual stopped state once applied.
                drained = (changes == {'backup': {'stop_after_current': True}} and
                           not item.get('error') and not item['config']['backup']['enabled'])
                expected = item['revision'] if drained else revision
                obsolete = bool(item.get('error') or (not drained and item['revision'] != revision))
                ready = view['running'] and not view['configuration'].get('pending') and view['configuration'].get('applied_revision') == expected and not stopping
                draining = changes == {'backup': {'stop_after_current': True}} and not drained
                if (not ready or draining) and not obsolete:
                    with self.db() as db:
                        db.execute('UPDATE bot_jobs SET retry_at=? WHERE id=?', (time.time() + 1, job['id']))
                    return False
                notice = {'title': '设置已变更', 'detail': '本次回执已失效，请查看当前状态。', 'level': 'info'} if obsolete else settings_result(
                    {'backup': {'enabled': False}} if drained else changes, True, False)
                response = self.panel(payload.get('page', payload.get('form', 'status')), notice)
                payload['wait_for_apply'] = False
                with self.db() as db:
                    db.execute("UPDATE bot_jobs SET state='applied',response=?,payload=? WHERE id=?", (json.dumps(response, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), job['id']))
            if job["state"] == "queued":
                page, notice = payload.get("panel", payload.get("page", "status")), payload.get("notice")
                custom_response = None
                applied = False
                try:
                    changes = payload.get("changes")
                    if changes in ({'backup': {'enabled': False}}, {'backup': {'stop_after_current': True}}):
                        from .camera_operations import cancel
                        from .selected_imports import cancel as cancel_selected
                        with self.db() as db:
                            cancel(db, owner=self.scope)
                            cancel_selected(db, owner=self.scope)
                    archive_change = payload.get('archive_change')
                    if payload.get('archive_draft') or archive_change:
                        from .archive_settings import check
                        from .bot_settings import confirm_archive
                        if time.time() - job['created'] > 300:
                            raise ConfigError('归档位置请求已过期，请重新检查')
                        checked = check(self.settings, payload.get('archive_draft') or archive_change,
                                        payload['mode'], payload['revision'], payload['connection_revision'],
                                        save=bool(archive_change), change_id=payload['change_id'])
                        if archive_change:
                            changes = {'archive': archive_change}
                            item = checked
                        else:
                            custom_response = confirm_archive(self, self.settings.read(), checked, payload['mode'],
                                                              payload['connection_revision'], payload['change_id'])
                    if 'source_decision' in payload:
                        from .source_connection import decide
                        if time.time() - job['created'] > 300:
                            raise ConfigError('确认请求已过期，请重新获取相机确认卡')
                        store = Store(self.directory)
                        try:
                            changed = decide(store, self.settings, payload['session_id'], payload['source_decision'])
                        finally:
                            store.close()
                        notice = {'title': '相机已确认' if payload['source_decision'] else '暂未确认相机',
                                  'detail': ('同步已开启' if self.settings.read()['config']['backup']['enabled'] else '同步已关闭') if payload['source_decision'] else '', 'level': 'success' if payload['source_decision'] else 'info'}
                    # Enabling arms discovery even before the camera is reachable.
                    # Source IO still requires a fresh, approved identity; a new
                    # camera receives its own confirmation without blocking SMB.
                    stop_request = changes == {"backup": {"enabled": False}}
                    if changes and not stop_request and time.time() - job["created"] > 300:
                        raise ConfigError("控制请求已经过期，未执行；请重新操作")
                    if changes:
                        if not archive_change:
                            item = self.settings.update_preferences(changes, None if stop_request else payload.get("revision"))
                        applied, stopping = False, False
                        until = time.monotonic() + 4
                        while time.monotonic() < until:
                            view = snapshot(self.directory, self.settings)
                            if view["running"] and view["configuration"].get("applied_revision") == item["revision"] and not view["configuration"].get("pending"):
                                applied = True
                                stopping = any(c["state"] == "pausing" for c in view["controls"].values()) or view.get('source_operation_running', False)
                                break
                            time.sleep(.2)
                        notice = settings_result(changes, applied, stopping)
                        waiting = not applied or stopping or changes == {'backup': {'stop_after_current': True}}
                        payload.update(changes_applied=changes, applied_revision=item['revision'], wait_for_apply=waiting)
                        if changes.get('backup', {}).get('enabled') is True:
                            # The notification worker owns the single live progress card.
                            from .notifications import NotificationWorker, _database, _digest
                            from .config import Config
                            config = Config.from_values(item['config'])
                            with _database(self.directory) as outgoing, outgoing:
                                from .destination_binding import identity
                                scope = _digest([identity(outgoing, config), config.source['type'], config.source['serial']])
                                NotificationWorker(self.directory, self.settings, threading.Event())._enqueue(
                                    outgoing, 'started:control:' + self.scope + ':' + job['id'], 'requested_progress', scope, '传输进度', '', time.time())
                            with self.db() as db:
                                db.execute("UPDATE bot_jobs SET state='done',error=NULL WHERE id=?", (job['id'],))
                            return True
                except (ConfigError, ValueError) as exc:
                    notice = {"title": "相机确认未完成" if 'source_decision' in payload else "本次修改未保存", "detail": str(exc), "level": "error"}
                if changes and changes.get('backup', {}).get('enabled') is False and not payload.get('response_message_id'):
                    message = self.active_progress_message()
                    if message: payload['response_message_id'] = message
                if payload.get('result_detail'):
                    detail = payload['result_detail']
                    result = detail.get('result', {})
                    from .notifications import _size
                    elements = [paragraph(f"{result.get('file_count', 0)} 个文件 · {_size(result.get('byte_count'))}")]
                    elements += [paragraph(path) for path in detail.get('locations', [])]
                    text = cleanup_text(result.get('cleanup', {}), result.get('ignored_preview_files', 0))
                    if text: elements.append(paragraph(text))
                    custom_response = card('归档详情', elements, shared=True)
                elif changes and changes.get('backup', {}).get('enabled') is True and applied:
                    custom_response = self.panel('status')
                response = custom_response or self.panel(page, notice)
                with self.db() as db:
                    db.execute("UPDATE bot_jobs SET state='applied',response=?,payload=? WHERE id=?", (json.dumps(response, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), job["id"]))
            elif job['state'] != 'waiting':
                response = json.loads(job["response"])
                waiting = payload.get('wait_for_apply', False)
            return self.reply(job, payload, response, waiting)
        except Exception:
            # Never log raw event bodies, credentials, URLs, or SDK exceptions.
            with self.db() as db:
                db.execute("UPDATE bot_jobs SET attempts=attempts+1,retry_at=?,error=? WHERE id=?",
                           (time.time() + min(300, 5 * 2 ** min(job["attempts"], 6)), "控制回复暂时失败，等待重试", job["id"]))
            logging.warning("飞书控制回复暂时失败，已保留请求等待重试")
            return False

    def reply(self, job, payload, response, waiting=False):
        receipt_key = 'bot_reply:' + self.scope + ':' + job['id']
        with self.db() as db:
            saved = db.execute('SELECT value FROM meta WHERE key=?', (receipt_key,)).fetchone()
        receipt = json.loads(saved[0]) if saved else {}
        message_id = receipt.get('id') or payload.get('message_id') or payload.get('response_message_id')
        with self.db() as db:
            newer = message_id and db.execute('''SELECT 1 FROM bot_jobs
                WHERE scope=? AND created>? AND json_extract(payload,'$.response_message_id')=? LIMIT 1''',
                (self.scope, job['created'], message_id)).fetchone()
        if newer:
            # Continue monitoring the underlying operation (including error
            # alerts), but never overwrite a newer view of this shared card.
            pass
        elif message_id:
            self.updater(self.credentials, message_id, response)
        else:
            message_id = self.sender(self.credentials, response, event_id="control:" + self.scope + ":" + job["id"])
        with self.db() as db:
            if isinstance(message_id, str):
                db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (receipt_key, json.dumps({'id': message_id})))
            db.execute("UPDATE bot_jobs SET state=?,error=NULL,retry_at=? WHERE id=?",
                       ('waiting' if waiting and isinstance(message_id, str) else 'done', time.time() + 1, job['id']))
        return True


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
    from .credentials import prune
    item = settings.read()
    if not item['error']:
        try:
            prune(settings.path.parent, [item['config'].get('archive', {}).get('credential_id'),
                                        item['config'].get('livestream', {}).get('credential_id')])
        except (ConfigError, OSError):
            logging.warning('过期凭据草稿暂未清理，当前配置保持不变')
    def message(event):
        bot.receive_message(json.loads(lark.JSON.marshal(event)))
    def callback(event):
        return P2CardActionTriggerResponse(bot.receive_card(json.loads(lark.JSON.marshal(event))))
    def menu(event):
        bot.receive_menu(json.loads(lark.JSON.marshal(event)))
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(message).register_p2_card_action_trigger(callback).register_p2_application_bot_menu_v6(menu).build()
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
