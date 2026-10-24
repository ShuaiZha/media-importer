"""Small Feishu forms. Only explicitly named, non-secret values enter jobs."""
import math

from .bot_cards import card, markdown, note, paragraph, plain
from .config import ConfigError, runtime_config

FORMS = {'media_settings', 'source_advanced', 'destination_advanced', 'livestream_settings', 'preview_settings', 'transfer_settings', 'notification_settings', 'notification_advanced', 'destination_settings', 'source_settings', 'advanced_settings'}
PAGES = FORMS | {'settings', 'media_settings', 'local_storage', 'preview_check', 'preview_check_details', 'preview_detect', 'camera_settings'}
LIMIT_FIELDS = {'upload_mib_per_second', 'max_cache_gib', 'min_free_disk_gib'}
INTERVAL_FIELDS = {'poll_seconds', 'retry_seconds', 'stability_seconds'}


def input_field(name, label, value, *, required=True, maximum=32):
    return {'tag': 'input', 'name': name, 'label': plain(label), 'label_position': 'top',
            'default_value': str(value), 'required': required, 'max_length': maximum}


def select(name, label, value, options):
    return {'tag': 'select_static', 'name': name, 'placeholder': plain(label),
            'initial_option': value, 'required': True,
            'options': [{'text': plain(text), 'value': key} for key, text in options]}


def form(bot, name, fields, revision, **extra):
    payload = {'settings_form': name, 'settings_version': 5, **extra}
    button = bot.button('检查并保存' if name in ('destination_settings', 'destination_advanced') else '检查并保存' if name == 'preview_settings' else '保存设置', payload, revision)
    button.update(name='save_' + name, action_type='form_submit', type='primary')
    return {'tag': 'form', 'name': name, 'elements': fields + [button]}


def panel(bot, page, item):
    values, revision = item['config'], item['revision']
    if page == 'settings':
        from .bot_routes import COMMANDS, MENUS
        return card('设置', [bot.buttons(bot.button(COMMANDS[key][0], COMMANDS[key][1]))
                            for key in MENUS[2][1]], shared=True)
    if page == 'camera_settings':
        return card('相机与画面', [bot.buttons(bot.button('相机连接', {'panel': 'source_settings'}),
                                              bot.button('观看入口', {'panel': 'preview_settings'}),
                                              bot.button('直播网络', {'panel': 'livestream_settings'}))], shared=True)
    if page == 'livestream_settings':
        live = values['livestream']
        secret = input_field('password', 'Wi-Fi 密码（留空沿用）', '', required=False, maximum=63)
        secret['input_type'] = 'password'
        return card('直播网络', [form(bot, page, [
            input_field('ssid', 'Wi-Fi 名称', live['ssid'], maximum=32), secret,
            input_field('port', 'RTMP 接收端口', live['port'], maximum=5)], revision),
            note('相机加入此 Wi-Fi，Mac 接收地址自动识别。Mac 热点需先在系统中开启。'),
            note('密码经飞书提交，仅保存至本机，不回显。')], shared=True)
    if page == 'media_settings':
        from .media import MEDIA_SCOPES
        backup = values['backup']
        fields = [select('media_scope', '素材类别', backup['media_scope'], list(MEDIA_SCOPES.items())),
                  select('archive_previews', '辅助文件', 'on' if backup['archive_previews'] else 'off',
                         [('off', 'LRV / THM：不归档'), ('on', 'LRV / THM：归档')]),
                  select('delete_source_after_import', '自动清理原片', 'on' if backup['delete_source_after_import'] else 'off',
                         [('off', '自动清理：关闭，保留原片'), ('on', '自动清理：完整导入后删除')])]
        return card('素材与清理', [form(bot, page, fields, revision),
            note('辅助文件仅指相机确认关联的 LRV / THM。开启自动清理需再次确认。')], shared=True)
    if page == 'transfer_settings':
        limits, backup = values['limits'], values['backup']
        return card('传输与缓存', [
            form(bot, page, [
                input_field('upload_mib_per_second', '上传限速（MiB/s，0 为不限速）', f"{limits['upload_mib_per_second']:g}"),
                input_field('max_cache_gib', '缓存额度（GiB，0 为不限额）', f"{limits['max_cache_gib']:g}"),
                input_field('min_free_disk_gib', '磁盘预留（GiB）', f"{limits['min_free_disk_gib']:g}"),
            ], revision),
            bot.buttons(bot.button('高级间隔', {'panel': 'advanced_settings'}),
                        bot.button('本机目录', {'panel': 'local_storage'}))], shared=True)
    if page == 'advanced_settings':
        backup = values['backup']
        return card('运行间隔', [form(bot, page, [
            input_field('poll_seconds', '空闲扫描（秒，1–3600）', f"{backup['poll_seconds']:g}"),
            input_field('retry_seconds', '失败重试（秒，1–3600）', f"{backup['retry_seconds']:g}"),
            input_field('stability_seconds', '清单复查（秒，0–60）', f"{backup['stability_seconds']:g}"),
        ], revision)], shared=True)
    if page in ('notification_settings', 'notification_advanced'):
        notes = values['notifications']
        fields = ([select('enabled', '自动通知', 'on' if notes['enabled'] else 'off',
                          [('on', '自动通知：开启'), ('off', '自动通知：关闭')]),
                   input_field('progress_interval_minutes', '进度间隔（分钟，1–1440）', f"{notes['progress_interval_seconds'] / 60:g}")]
                  if page == 'notification_settings' else [
                   select('connection_events', '连接提醒', 'on' if notes['connection_events'] else 'off',
                          [('on', '连接与断开提醒：开启'), ('off', '连接与断开提醒：关闭')]),
                   input_field('keyword', '通知前缀（可留空）', notes['keyword'], required=False, maximum=100)])
        elements = [form(bot, page, fields, revision)]
        if page == 'notification_settings':
            elements.append(bot.buttons(bot.button('高级通知', {'panel': 'notification_advanced'})))
        return card('消息通知' if page == 'notification_settings' else '高级通知', elements, shared=True)
    if page in ('destination_settings', 'destination_advanced'):
        from .archive_settings import connection_revision
        target = values['destination']
        password = input_field('password', '新密码（留空沿用当前密码）', '', required=False, maximum=1000)
        password['input_type'] = 'password'
        if page == 'destination_advanced':
            return card('SMB 高级连接', [form(bot, page, [
                input_field('port', '端口（1–65535）', target['port'], maximum=5),
                input_field('domain', '认证域（可留空）', target['domain'], required=False, maximum=255),
            ], revision, connection_revision=connection_revision(values))], shared=True)
        return card('归档存储', [
            paragraph(location(target)),
            form(bot, page, [
                select('mode', '修改方式', 'connection',
                       [('connection', '修改原存储连接'), ('new', '切换到新归档位置')]),
                input_field('host', 'SMB 地址（IP 或域名）', target['host'], maximum=253),
                input_field('share', '共享名', target['share'], maximum=255),
                input_field('directory', '共享内子目录（留空为根目录）', target['directory'], required=False, maximum=1000),
                input_field('name', '存储显示名称', target['name'], maximum=100),
                input_field('username', 'SMB 账号', target['username'], maximum=255),
                password,
            ], revision, connection_revision=connection_revision(values)),
            bot.buttons(bot.button('高级连接', {'panel': 'destination_advanced'})),
            note('停止备份后修改；检查会创建并清理测试文件。'),
            note('密码经飞书提交，保存到本机，不回显。')], shared=True)
    if page in ('source_settings', 'source_advanced'):
        source = item.get('source_hint', values['source'])
        if page == 'source_advanced':
            return card('相机请求设置', [form(bot, page, [
                input_field('timeout_seconds', '请求超时（秒，1–300）', source['timeout_seconds']),
                input_field('request_attempts', '尝试次数（1–5）', source['request_attempts']),
            ], revision)], shared=True)
        return card('相机连接', [form(bot, page, [
            select('source_transport', '连接方式', values['backup']['source_transport'],
                   [('auto', '自动选择，优先 USB'), ('usb', '仅 USB'), ('wifi', 'Wi-Fi')]),
            input_field('url', 'USB 探测地址（留空自动发现）', source['url'], required=False, maximum=1000),
            input_field('serial', '序列号提示（可留空）', source['serial'], required=False, maximum=100),
        ], revision), bot.buttons(bot.button('请求设置', {'panel': 'source_advanced'})),
            note('停止备份后修改。新发现的相机仍需确认。')], shared=True)
    if page in ('preview_settings', 'preview_check', 'preview_check_details', 'preview_detect'):
        from .preview_access import configuration
        from .preview_settings import discover, inspect
        config = configuration(values)
        public = config['public']
        elements = []
        if page == 'preview_detect':
            try:
                public = discover(config['port'])
                elements.append(paragraph('已读取本机 Tailscale 入口，尚未保存。'))
            except ConfigError as exc:
                elements.append(paragraph(str(exc)))
        elif page in ('preview_check', 'preview_check_details'):
            report = inspect(public, config['port'])
            checks = report['checks'] if page == 'preview_check_details' else [c for c in report['checks'] if not c['ok']]
            elements = [paragraph(c['label'] + '：' + c['detail']) for c in checks]
            actions = [bot.button('修改入口', {'panel': 'preview_settings'})]
            if page == 'preview_check': actions.append(bot.button('查看详情', {'panel': 'preview_check_details'}))
            return card('连接检查通过' if report['ok'] else '连接检查未通过', elements + [bot.buttons(*actions)],
                        'green' if report['ok'] else 'orange', shared=True)
        elements += [
            paragraph('观看范围：' + ('Tailscale' if config['public'] else '仅本机')),
            form(bot, 'preview_settings', [
                input_field('public_url', 'Tailscale HTTPS 地址（留空仅本机）', public, required=False, maximum=512)
            ], revision),
            bot.buttons(bot.button('读取本机入口', {'panel': 'preview_detect'}),
                        bot.button('检查当前连接', {'panel': 'preview_check'})),
            note('结束预览后修改。远程观看设备需连接 Tailscale。')]
        return card('观看入口', elements, shared=True)
    if page == 'local_storage':
        import os
        from .deployment import cache_path
        return card('本机目录', [
            paragraph('配置：' + os.environ.get('MEDIA_IMPORTER_CONFIG_HOST_DIR', str(bot.settings.path.parent)) +
                      '\n任务：' + os.environ.get('MEDIA_IMPORTER_DATA_HOST_DIR', str(bot.directory)) +
                      '\n缓存：' + os.environ.get('MEDIA_IMPORTER_CACHE_HOST_DIR', str(cache_path(bot.directory)))),
            note('目录迁移需在本机执行。'),
            bot.buttons({'tag': 'button', 'text': plain('查看迁移步骤'), 'type': 'default',
                         'url': 'https://github.com/ShuaiZha/media-importer/blob/main/docs/recovery.md'})], shared=True)
    raise ConfigError('设置页面不存在')


def location(target):
    return f"smb://{target['host']}:{target['port']}/{target['share']}" + (
        '/' + target['directory'] if target['directory'] else '')


def parse(name, fields, version=2):
    expected = {
        'livestream_settings': {'ssid', 'password', 'port'},
        'preview_settings': {'public_url'},
        'media_settings': {'media_scope', 'archive_previews', 'delete_source_after_import'},
        'transfer_settings': LIMIT_FIELDS,
        'notification_settings': {'enabled', 'connection_events', 'progress_interval_seconds', 'keyword'},
        'notification_advanced': {'connection_events', 'keyword'},
        'destination_settings': {'host', 'port', 'share', 'directory', 'mode'},
        'destination_advanced': {'port', 'domain'},
        'source_settings': {'url', 'serial', 'timeout_seconds', 'request_attempts'},
        'source_advanced': {'timeout_seconds', 'request_attempts'},
        'advanced_settings': INTERVAL_FIELDS,
    }[name].copy()
    if name == 'notification_settings' and version in (4, 5):
        expected = {'enabled', 'progress_interval_minutes' if version == 5 else 'progress_interval_seconds'}
    if name == 'transfer_settings' and version == 3:
        expected |= INTERVAL_FIELDS
    if name == 'destination_settings' and version in (2, 5):
        expected |= {'name', 'username', 'domain', 'password'}
        if version == 5: expected -= {'port', 'domain'}
    if name == 'source_settings' and version == 5:
        expected = {'url', 'serial', 'source_transport'}
    if not isinstance(fields, dict) or set(fields) != expected:
        raise ConfigError('表单内容不完整，请重新打开设置')
    if any(not isinstance(v, str) or len(v) > 1024 or any(ord(c) < 32 for c in v) for v in fields.values()):
        raise ConfigError('表单格式无效，请检查输入内容')
    fields = {k: v if k in ('password', 'ssid') else v.strip() for k, v in fields.items()}

    def number(key, label, low, high=None, integer=False):
        try:
            value = float(fields[key])
            if not math.isfinite(value) or value < low or high is not None and value > high or integer and not value.is_integer():
                raise ValueError()
            return int(value) if integer else value
        except (ValueError, OverflowError):
            bounds = f'{low:g}–{high:g}' if high is not None else f'不小于 {low:g}'
            raise ConfigError(f'{label}需为{bounds}的' + ('整数' if integer else '数值')) from None

    def toggle(key):
        if fields[key] not in ('on', 'off'): raise ConfigError('开关值无效，请重新打开设置')
        return fields[key] == 'on'

    if name == 'livestream_settings':
        from .livestream_settings import validate
        fields['port'] = number('port', 'RTMP 接收端口', 1, 65535, True)
        validate({k: v for k, v in {**fields, 'credential_id': ''}.items() if k != 'password'})
        return fields
    if name == 'preview_settings':
        from .preview_settings import normalize_url
        return {'preview': {'public_url': normalize_url(fields['public_url'])}}
    if name == 'media_settings':
        from .media import MEDIA_SCOPES
        if fields['media_scope'] not in MEDIA_SCOPES: raise ConfigError('素材类别无效')
        return {'backup': {'media_scope': fields['media_scope'], 'archive_previews': toggle('archive_previews'),
                           'delete_source_after_import': toggle('delete_source_after_import')}}
    if name == 'transfer_settings':
        labels = {'upload_mib_per_second': '上传限速', 'max_cache_gib': '缓存额度', 'min_free_disk_gib': '磁盘预留'}
        result = {'limits': {k: number(k, labels[k], 0) for k in LIMIT_FIELDS}}
        if version == 3: result.update(parse('advanced_settings', {k: fields[k] for k in INTERVAL_FIELDS}, 2))
        return result
    if name in ('notification_settings', 'notification_advanced'):
        if 'progress_interval_minutes' in fields:
            seconds = number('progress_interval_minutes', '进度间隔（分钟）', 1, 1440) * 60
            if abs(seconds - round(seconds)) > .00001: raise ConfigError('进度间隔需能换算成整数秒')
            fields.pop('progress_interval_minutes')
            fields['progress_interval_seconds'] = str(round(seconds))
        for key in ('enabled', 'connection_events'):
            if key in fields: fields[key] = toggle(key)
        if 'progress_interval_seconds' in fields:
            fields['progress_interval_seconds'] = number('progress_interval_seconds', '进度间隔（秒）', 60, 86400, True)
        from .config import DEFAULTS
        runtime_config({**DEFAULTS, 'notifications': {**DEFAULTS['notifications'], **fields}})
        return {'notifications': fields}
    if name in ('source_settings', 'source_advanced'):
        if 'source_transport' in fields:
            mode = fields.pop('source_transport')
            if mode not in ('auto', 'usb', 'wifi'): raise ConfigError('连接方式无效')
            return {'source_options': fields, 'backup': {'source_transport': mode}}
        fields['timeout_seconds'] = number('timeout_seconds', '请求超时（秒）', 1, 300)
        fields['request_attempts'] = number('request_attempts', '尝试次数', 1, 5, True)
        return {'source_options': fields}
    if name == 'advanced_settings':
        return {'backup': {'poll_seconds': number('poll_seconds', '空闲扫描（秒）', 1, 3600),
                           'retry_seconds': number('retry_seconds', '失败重试（秒）', 1, 3600),
                           'stability_seconds': number('stability_seconds', '清单复查（秒）', 0, 60)}}
    if 'mode' in fields and fields['mode'] not in ('connection', 'new'):
        raise ConfigError('修改方式无效')
    if 'port' in fields: fields['port'] = number('port', 'SMB 端口', 1, 65535, True)
    return fields


def confirm_archive(bot, item, fields, mode, base, change_id):
    return card('确认归档位置', [
        markdown('**检查通过，尚未保存**'),
        paragraph('当前：' + location(item['config']['destination']) + '\n修改为：' + location(fields)),
        paragraph('保留任务和存储身份，仅更新连接。' if mode == 'connection' else
                  '后续素材归档到新位置；已有归档不移动。相机中保留的素材可能再次归档。'),
        bot.buttons(bot.button('返回修改', {'panel': 'destination_settings'}),
                    bot.button('确认保存', {'archive_change': fields, 'mode': mode, 'settings_version': 2,
                        'connection_revision': base, 'change_id': change_id, 'page': 'destination_settings'},
                               item['revision']))
    ], 'orange', shared=True)


def supported_v5(payload):
    if 'settings_form' in payload:
        return payload['settings_form'] in FORMS
    changes = payload.get('changes', payload.get('changes_applied', payload.get('media_draft')))
    if not isinstance(changes, dict): return False
    shapes = [
        {'backup': {'media_scope', 'archive_previews', 'delete_source_after_import'}},
        {'backup': {'source_transport'}, 'source_options': {'url', 'serial'}},
        {'source_options': {'timeout_seconds', 'request_attempts'}},
        {'limits': LIMIT_FIELDS}, {'backup': INTERVAL_FIELDS},
        {'notifications': {'enabled', 'progress_interval_seconds'}},
        {'notifications': {'connection_events', 'keyword'}},
        {'livestream': {'ssid', 'credential_id', 'port'}}, {'preview': {'public_url'}},
    ]
    return any(set(changes) == set(shape) and all(isinstance(changes[k], dict) and set(changes[k]) == shape[k]
                                                for k in shape) for shape in shapes)


def confirm_media(bot, changes, revision):
    return card('确认启用自动清理', [
        paragraph('所选素材完整导入后删除相机原片。'),
        markdown('**远端归档完成前，本地完整缓存可能是唯一副本。**'),
        bot.buttons(bot.button('返回修改', {'panel': 'media_settings'}),
                    bot.button('确认保存并启用', {'changes': changes, 'settings_version': 5, 'page': 'media_settings'},
                               revision, danger=True)),
    ], 'orange', shared=True)
