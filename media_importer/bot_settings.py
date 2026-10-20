"""Small Feishu forms. Only explicitly named, non-secret values enter jobs."""
import math

from .bot_cards import card, markdown, note, paragraph, plain
from .config import ConfigError, runtime_config

FORMS = {'transfer_settings', 'notification_settings', 'destination_settings', 'source_settings', 'advanced_settings'}
PAGES = FORMS | {'settings', 'media_settings', 'local_storage'}
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
    payload = {'settings_form': name, 'settings_version': 3 if name == 'transfer_settings' else 2, **extra}
    button = bot.button('检查归档位置' if name == 'destination_settings' else '保存设置', payload, revision)
    button.update(name='save_' + name, action_type='form_submit', type='primary')
    return {'tag': 'form', 'name': name, 'elements': fields + [button]}


def panel(bot, page, item):
    values, revision = item['config'], item['revision']
    if page == 'advanced_settings':
        page = 'transfer_settings'  # Existing cards open the consolidated form.
    if page == 'settings':
        return card('菜单使用说明', [
            markdown('**状态**：运行状态、相机状态、本机目录\n'
                     '**同步**：开启同步、立即停止、当前文件完成后停止、选择素材、清理原片\n'
                     '**设置**：素材与清理、传输与缓存、消息通知、相机连接、归档存储'),
            note('从底部菜单打开功能，卡片中填写参数并保存。扫描与重试间隔位于「传输与缓存」。')], shared=True)
    if page == 'media_settings':
        from .media import MEDIA_SCOPES
        backup = values['backup']
        return card('素材与清理', [
            paragraph('素材范围：' + MEDIA_SCOPES[backup['media_scope']] +
                      '\n预览文件：' + ('归档' if backup['archive_previews'] else '不归档已确认关联的 LRV、THM') +
                      '\n原片处理：' + ('完整导入后删除' if backup['delete_source_after_import'] else '保留')),
            bot.buttons(bot.button('素材范围', {'panel': 'scope'}), bot.button('预览文件', {'panel': 'previews'}),
                        bot.button('原片处理', {'panel': 'cleanup'}))], shared=True)
    if page == 'transfer_settings':
        limits, backup = values['limits'], values['backup']
        return card('传输与缓存', [
            form(bot, page, [
                input_field('upload_mib_per_second', '上传限速（MiB/s，0 为不限速）', f"{limits['upload_mib_per_second']:g}"),
                input_field('max_cache_gib', '缓存额度（GiB，0 为不限额）', f"{limits['max_cache_gib']:g}"),
                input_field('min_free_disk_gib', '磁盘预留（GiB）', f"{limits['min_free_disk_gib']:g}"),
                input_field('poll_seconds', '空闲扫描（秒，1–3600）', f"{backup['poll_seconds']:g}"),
                input_field('retry_seconds', '失败重试（秒，1–3600）', f"{backup['retry_seconds']:g}"),
                input_field('stability_seconds', '清单复查（秒，0–60）', f"{backup['stability_seconds']:g}"),
            ], revision),
            note('当前文件完成后应用。降低缓存额度不删除已有缓存；较短间隔会增加设备与存储请求。')], shared=True)
    if page == 'notification_settings':
        notes = values['notifications']
        return card('消息通知', [
            form(bot, page, [
                select('enabled', '自动通知', 'on' if notes['enabled'] else 'off',
                       [('on', '自动通知：开启'), ('off', '自动通知：关闭')]),
                select('connection_events', '连接提醒', 'on' if notes['connection_events'] else 'off',
                       [('on', '连接与断开提醒：开启'), ('off', '连接与断开提醒：关闭')]),
                input_field('progress_interval_seconds', '进度更新间隔（秒，60–86400）', notes['progress_interval_seconds']),
                input_field('keyword', '通知前缀（可留空）', notes['keyword'], required=False, maximum=100),
            ], revision),
            note('进度更新同一张卡片，完成与异常单独提醒。设备确认和控制回执不受通知开关影响。')], shared=True)
    if page == 'destination_settings':
        from .archive_settings import connection_revision
        target = values['destination']
        password = input_field('password', '新密码（留空沿用当前密码）', '', required=False, maximum=1000)
        password['input_type'] = 'password'
        return card('归档存储', [
            paragraph(location(target)),
            form(bot, page, [
                select('mode', '修改方式', 'connection',
                       [('connection', '修改原存储连接'), ('new', '切换到新归档位置')]),
                input_field('host', 'SMB 地址（IP 或域名）', target['host'], maximum=253),
                input_field('port', '端口', target['port'], maximum=5),
                input_field('share', '共享名', target['share'], maximum=255),
                input_field('directory', '共享内子目录（留空为根目录）', target['directory'], required=False, maximum=1000),
                input_field('name', '存储显示名称', target['name'], maximum=100),
                input_field('username', 'SMB 账号', target['username'], maximum=255),
                input_field('domain', '认证域（可留空）', target['domain'], required=False, maximum=255),
                password,
            ], revision, connection_revision=connection_revision(values)),
            note('先停止同步。检查会写入并清理一个小测试文件，成功后仍需确认保存。'),
            note('密码通过飞书提交，仅保存到本机凭据文件，不回显、不写入任务和日志。'),
            note('已有归档不移动。新位置须等待旧任务与缓存收尾。')], shared=True)
    if page == 'source_settings':
        source = item.get('source_hint', values['source'])
        return card('相机连接', [
            paragraph('当前适配器：' + source['type'] + ' · USB'),
            form(bot, page, [
                input_field('url', '优先探测地址（留空自动发现，含协议与端口）', source['url'], required=False, maximum=1000),
                input_field('serial', '序列号提示（可留空）', source['serial'], required=False, maximum=100),
                input_field('timeout_seconds', '相机请求超时（秒，1–300）', source['timeout_seconds']),
                input_field('request_attempts', '请求尝试次数（1–5）', source['request_attempts']),
            ], revision),
            note('先停止同步。地址与序列号是发现提示；识别到其他相机仍需确认，任务和缓存保留。')], shared=True)
    if page == 'local_storage':
        import os
        from .deployment import cache_path
        return card('本机目录', [
            paragraph('配置：' + os.environ.get('MEDIA_IMPORTER_CONFIG_HOST_DIR', str(bot.settings.path.parent)) +
                      '\n任务：' + os.environ.get('MEDIA_IMPORTER_DATA_HOST_DIR', str(bot.directory)) +
                      '\n缓存：' + os.environ.get('MEDIA_IMPORTER_CACHE_HOST_DIR', str(cache_path(bot.directory)))),
            note('换盘需要迁移未归档素材和任务，并重新登记挂载；此处不直接切换路径。飞书首次连接凭据由本机安装向导管理。'),
            bot.buttons({'tag': 'button', 'text': plain('查看迁移步骤'), 'type': 'default',
                         'url': 'https://github.com/ShuaiZha/media-importer/blob/main/docs/recovery.md'})], shared=True)
    raise ConfigError('设置页面不存在')


def location(target):
    return f"smb://{target['host']}:{target['port']}/{target['share']}" + (
        '/' + target['directory'] if target['directory'] else '')


def parse(name, fields, version=2):
    expected = {
        'transfer_settings': LIMIT_FIELDS,
        'notification_settings': {'enabled', 'connection_events', 'progress_interval_seconds', 'keyword'},
        'destination_settings': {'host', 'port', 'share', 'directory', 'mode'},
        'source_settings': {'url', 'serial', 'timeout_seconds', 'request_attempts'},
        'advanced_settings': INTERVAL_FIELDS,
    }[name].copy()
    if name == 'transfer_settings' and version == 3:
        expected |= INTERVAL_FIELDS
    if name == 'destination_settings' and version == 2:
        expected |= {'name', 'username', 'domain', 'password'}
    if not isinstance(fields, dict) or set(fields) != expected:
        raise ConfigError('表单内容不完整，请重新打开设置')
    if any(not isinstance(v, str) or len(v) > 1024 or any(ord(c) < 32 for c in v) for v in fields.values()):
        raise ConfigError('表单格式无效，请检查输入内容')
    fields = {k: v if k == 'password' else v.strip() for k, v in fields.items()}
    try:
        if name == 'transfer_settings':
            numbers = {k: float(fields[k]) for k in LIMIT_FIELDS}
            if any(not math.isfinite(v) or v < 0 for v in numbers.values()):
                raise ValueError()
            result = {'limits': numbers}
            if version == 3:
                result.update(parse('advanced_settings', {k: fields[k] for k in INTERVAL_FIELDS}, 2))
            return result
        if name == 'notification_settings':
            if fields['enabled'] not in ('on', 'off') or fields['connection_events'] not in ('on', 'off'):
                raise ValueError()
            result = {'notifications': {**fields, 'enabled': fields['enabled'] == 'on',
                                       'connection_events': fields['connection_events'] == 'on',
                                       'progress_interval_seconds': int(fields['progress_interval_seconds'])}}
            from .config import DEFAULTS
            runtime_config({**DEFAULTS, **result})
            return result
        if name == 'source_settings':
            fields['timeout_seconds'] = float(fields['timeout_seconds'])
            fields['request_attempts'] = int(fields['request_attempts'])
            if not 1 <= fields['timeout_seconds'] <= 300 or not 1 <= fields['request_attempts'] <= 5:
                raise ConfigError('相机超时需为 1–300 秒，请求尝试次数需为 1–5 的整数')
            return {'source_options': fields}
        if name == 'advanced_settings':
            fields = {key: float(value) for key, value in fields.items()}
            if (any(not math.isfinite(v) for v in fields.values()) or
                    not 1 <= fields['poll_seconds'] <= 3600 or not 1 <= fields['retry_seconds'] <= 3600
                    or not 0 <= fields['stability_seconds'] <= 60):
                raise ConfigError('扫描和重试间隔需为 1–3600 秒，清单复查间隔需为 0–60 秒')
            return {'backup': fields}
        if fields['mode'] not in ('connection', 'new'):
            raise ValueError()
        fields['port'] = int(fields['port'])
        return fields
    except (ValueError, OverflowError):
        raise ConfigError('请输入有效数值；进度间隔为 60–86400 的整数秒，端口为 1–65535') from None


def confirm_archive(bot, item, fields, mode, base, change_id):
    return card('确认归档位置', [
        markdown('**连接、写入与改名检查通过，尚未保存。**'),
        paragraph('当前：' + location(item['config']['destination']) + '\n修改为：' + location(fields)),
        paragraph('保留任务和存储身份，仅更新连接。' if mode == 'connection' else
                  '后续素材归档到新位置；已有归档不移动。相机中保留的素材可能再次归档。'),
        bot.buttons(bot.button('返回修改', {'panel': 'destination_settings'}),
                    bot.button('确认保存', {'archive_change': fields, 'mode': mode, 'settings_version': 2,
                        'connection_revision': base, 'change_id': change_id, 'page': 'destination_settings'},
                               item['revision']))
    ], 'orange', shared=True)
