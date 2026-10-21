"""Small Feishu forms for one camera-side change at a time."""
import re
import time
from datetime import datetime

from .bot_cards import card, paragraph, note, plain
from .model import ConfigError


def read_catalog(store, session):
    value = store.get_meta('camera_catalog', {})
    try:
        valid = 0 <= time.time() - datetime.fromisoformat(value['observed_at']).timestamp() < 300
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid or value.get('session_id') != session:
        raise ConfigError('拍摄设置已过期，请重新读取')
    return value


def parse(action, fields):
    if not isinstance(fields, dict) or set(fields) != {'choice'}:
        raise ConfigError('请选择一个选项后再提交')
    text = fields['choice']
    if not isinstance(text, str) or not re.fullmatch(r'0|[1-9][0-9]{0,9}', text):
        raise ConfigError('拍摄选项无效')
    value = int(text)
    if action['action'] == 'apply_preset':
        return {'preset': value}
    return {'key': action['key'], 'value': value, 'expected_preset': action['expected_preset']}


def form(bot, label, options, action, session, **extra):
    from .capture_cards import button
    submit = button(bot, '应用', action, session, **extra)
    submit.update(type='primary', name='apply', action_type='form_submit')
    field = {'tag': 'select_static', 'name': 'choice', 'required': True,
             'placeholder': plain(label), 'options': [{'text': plain(o['label']), 'value': str(o['value'])} for o in options]}
    return {'tag': 'form', 'name': 'camera_setting', 'elements': [field, submit]}


def settings_panel(bot, store):
    from .capture_cards import button
    connection = store.get_meta('source_connection', {})
    session = connection.get('id')
    catalog = read_catalog(store, session)
    presets = catalog.get('presets', [])
    elements = [paragraph('当前预设：' + catalog.get('preset_label', '未知') +
                          (' · ' + catalog['specification'] if catalog.get('specification') else ''))]
    if presets:
        modes = {'video': '视频', 'photo': '照片', 'timelapse': '延时'}
        options = [{'value': p['id'], 'label': (modes[p['mode']] + ' · ' + p['label'] +
                    (' · ' + p['specification'] if p.get('specification') else ''))[:120]} for p in presets]
        elements += [form(bot, '选择相机预设', options, 'apply_preset', session)]
    parameters = catalog.get('parameters', [])
    if parameters:
        elements.append(paragraph('\n'.join(p['label'] + '：' + p['value_label'] for p in parameters)))
        for offset in range(0, len(parameters), 3):
            elements.append(bot.buttons(*(button(bot, p['label'], 'parameter', session, key=p['key'])
                                          for p in parameters[offset:offset + 3])))
    elements += [note('每次修改一项。可用选项由相机即时提供；修改后重新读取，确认其他参数是否联动变化。'),
                 bot.buttons(button(bot, '重新读取', 'settings'), button(bot, '返回拍摄控制', 'panel'))]
    return card('拍摄设置', elements, shared=True)


def parameter_panel(bot, store, action):
    from .capture_cards import button
    value = read_catalog(store, action['session_id'])
    parameter = next((p for p in value.get('parameters', []) if p['key'] == action['key']), None)
    if not parameter: raise ConfigError('当前模式不支持此参数，请重新读取拍摄设置')
    elements = [paragraph('当前值：' + parameter['value_label']),
        form(bot, '选择' + parameter['label'], parameter['options'], 'apply_parameter', action['session_id'],
             key=parameter['key'], expected_preset=value['preset'])]
    if parameter['key'] in ('duration', 'lapse_duration'):
        elements.append(note('时长由相机执行，断开电脑后仍按相机设置结束。不限时需手动停止；此设置保存在当前相机预设中。'))
    elements += [bot.buttons(button(bot, '返回拍摄设置', 'settings'))]
    return card(parameter['label'], elements, shared=True)
