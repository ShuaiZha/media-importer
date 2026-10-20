"""Small Feishu forms. Only explicitly named, non-secret values enter jobs."""
import math

from .bot_cards import card, markdown, note, paragraph, plain
from .config import ConfigError, runtime_config

FORMS = {'transfer_settings', 'notification_settings', 'destination_settings'}
PAGES = FORMS | {'settings', 'media_settings'}


def input_field(name, label, value, *, required=True, maximum=32):
    return {'tag': 'input', 'name': name, 'label': plain(label), 'label_position': 'top',
            'default_value': str(value), 'required': required, 'max_length': maximum}


def select(name, label, value, options):
    return {'tag': 'select_static', 'name': name, 'placeholder': plain(label),
            'initial_option': value, 'required': True,
            'options': [{'text': plain(text), 'value': key} for key, text in options]}


def form(bot, name, fields, revision, **extra):
    payload = {'settings_form': name, 'settings_version': 1, **extra}
    button = bot.button('检查归档位置' if name == 'destination_settings' else '保存设置', payload, revision)
    button.update(name='save_' + name, action_type='form_submit', type='primary')
    return {'tag': 'form', 'name': name, 'elements': fields + [button]}


def panel(bot, page, item):
    values, revision = item['config'], item['revision']
    navigation = bot.buttons(bot.button('返回设置', {'panel': 'settings'}))
    if page == 'settings':
        return card('设置', [
            paragraph('选择要调整的项目，卡片会显示当前值。'),
            bot.buttons(bot.button('素材与清理', {'panel': 'media_settings'}),
                        bot.button('传输与缓存', {'panel': 'transfer_settings'})),
            bot.buttons(bot.button('消息通知', {'panel': 'notification_settings'}),
                        bot.button('归档位置', {'panel': 'destination_settings'})),
            note('缓存目录迁移和账号凭据在本机操作。')], shared=True)
    if page == 'media_settings':
        from .media import MEDIA_SCOPES
        backup = values['backup']
        return card('素材与清理', [
            paragraph('素材范围：' + MEDIA_SCOPES[backup['media_scope']] +
                      '\n预览文件：' + ('归档' if backup['archive_previews'] else '不归档已确认关联的 LRV、THM') +
                      '\n原片处理：' + ('完整导入后删除' if backup['delete_source_after_import'] else '保留')),
            bot.buttons(bot.button('素材范围', {'panel': 'scope'}), bot.button('预览文件', {'panel': 'previews'}),
                        bot.button('原片处理', {'panel': 'cleanup'})), navigation], shared=True)
    if page == 'transfer_settings':
        limits = values['limits']
        return card('传输与缓存', [
            form(bot, page, [
                input_field('upload_mib_per_second', '上传限速（MiB/s，0 为不限速）', f"{limits['upload_mib_per_second']:g}"),
                input_field('max_cache_gib', '缓存额度（GiB，0 为不限额）', f"{limits['max_cache_gib']:g}"),
                input_field('min_free_disk_gib', '磁盘预留（GiB）', f"{limits['min_free_disk_gib']:g}")
            ], revision),
            note('当前文件按原限制完成，随后应用新设置。降低缓存额度不会删除已有缓存。'), navigation], shared=True)
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
            note('进度更新同一张卡片，完成与异常单独提醒。设备确认和控制回执不受通知开关影响。'), navigation], shared=True)
    if page == 'destination_settings':
        from .archive_settings import connection_revision
        target = values['destination']
        return card('归档位置', [
            paragraph(location(target)),
            form(bot, page, [
                select('mode', '修改方式', 'connection',
                       [('connection', '修改原存储连接'), ('new', '切换到新归档位置')]),
                input_field('host', 'SMB 地址（IP 或域名）', target['host'], maximum=253),
                input_field('port', '端口', target['port'], maximum=5),
                input_field('share', '共享名', target['share'], maximum=255),
                input_field('directory', '共享内子目录（留空为根目录）', target['directory'], required=False, maximum=1000)
            ], revision, connection_revision=connection_revision(values)),
            note('先停止同步。检查会写入并清理一个小测试文件，成功后仍需确认保存。'),
            note('沿用本机 SMB 账号和密码；已有归档不移动。新位置须等待旧任务与缓存收尾。'), navigation], shared=True)
    raise ConfigError('设置页面不存在')


def location(target):
    return f"smb://{target['host']}:{target['port']}/{target['share']}" + (
        '/' + target['directory'] if target['directory'] else '')


def parse(name, fields):
    expected = {
        'transfer_settings': {'upload_mib_per_second', 'max_cache_gib', 'min_free_disk_gib'},
        'notification_settings': {'enabled', 'connection_events', 'progress_interval_seconds', 'keyword'},
        'destination_settings': {'host', 'port', 'share', 'directory', 'mode'},
    }[name]
    if not isinstance(fields, dict) or set(fields) != expected:
        raise ConfigError('表单内容不完整，请重新打开设置')
    if any(not isinstance(v, str) or len(v) > 1024 or any(ord(c) < 32 for c in v) for v in fields.values()):
        raise ConfigError('表单格式无效，请检查输入内容')
    fields = {k: v.strip() for k, v in fields.items()}
    try:
        if name == 'transfer_settings':
            numbers = {k: float(v) for k, v in fields.items()}
            if any(not math.isfinite(v) or v < 0 for v in numbers.values()):
                raise ValueError()
            return {'limits': numbers}
        if name == 'notification_settings':
            if fields['enabled'] not in ('on', 'off') or fields['connection_events'] not in ('on', 'off'):
                raise ValueError()
            result = {'notifications': {**fields, 'enabled': fields['enabled'] == 'on',
                                       'connection_events': fields['connection_events'] == 'on',
                                       'progress_interval_seconds': int(fields['progress_interval_seconds'])}}
            from .config import DEFAULTS
            runtime_config({**DEFAULTS, **result})
            return result
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
                    bot.button('确认保存', {'archive_change': fields, 'mode': mode, 'settings_version': 1,
                        'connection_revision': base, 'change_id': change_id, 'page': 'destination_settings'},
                               item['revision']))
    ], 'orange', shared=True)
