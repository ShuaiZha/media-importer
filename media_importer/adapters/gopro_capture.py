"""GoPro shooting vocabulary and dynamic capabilities, kept out of bot/core code.

Wire IDs: Open GoPro BLE Query 0x32 (capabilities), Setting 0x74,
Command 0x40 (load preset), 0x18 (HiLight). Options are never inferred from
this display vocabulary: the camera must advertise them immediately before use.
"""
import asyncio

from ..model import SourceConnectionError, now

PARAMETERS = {
    'resolution': (2, '分辨率', {'video', 'timelapse'}),
    'fps': (3, '帧率', {'video', 'timelapse'}),
    'lens': (121, '视频视角', {'video', 'timelapse'}),
    'photo_lens': (122, '照片视角', {'photo'}),
    'lapse_lens': (123, '延时视角', {'timelapse'}),
    'stabilization': (135, '防抖', {'video'}),
    'duration': (156, '录像时长', {'video'}),
    'lapse_duration': (157, '延时拍摄时长', {'timelapse'}),
    'video_interval': (5, '延时视频间隔', {'timelapse'}),
    'photo_interval': (30, '延时照片间隔', {'timelapse'}),
    'white_balance': (115, '白平衡', {'video', 'photo', 'timelapse'}),
    'color': (116, '色彩', {'video', 'photo', 'timelapse'}),
    'sharpness': (117, '锐度', {'video', 'photo', 'timelapse'}),
    'ev': (118, '曝光补偿', {'video', 'photo', 'timelapse'}),
    'iso_min': (102, 'ISO 下限', {'video'}),
    'iso_max': (13, 'ISO 上限', {'video'}),
    'photo_iso_min': (75, 'ISO 下限', {'photo'}),
    'photo_iso_max': (24, 'ISO 上限', {'photo'}),
    'shutter': (145, '快门', {'video'}),
    'photo_shutter': (146, '快门', {'photo'}),
    'bitrate': (124, '码率', {'video'}),
    'photo_output': (125, '照片输出', {'photo'}),
}
# These legacy IDs aren't in the public Open GoPro setting subset. Only expose
# them when the connected camera's own /gp/gpControl vocabulary agrees, AND
# its BLE capabilities advertise a currently available option.
LEGACY = {115: 'White Balance', 116: 'Color', 117: 'Sharpness', 118: 'EV Comp',
          102: 'ISO Min', 13: 'ISO Max', 75: 'ISO Min', 24: 'ISO Max',
          145: 'Shutter', 146: 'Shutter', 124: 'Bit Rate'}
TRANSLATIONS = {'Auto': '自动', 'Native': '原生', 'Flat': '平坦', 'GoPro': 'GoPro 色彩',
                'Natural': '自然', 'Vibrant': '鲜艳', 'High': '高', 'Medium': '中',
                'Low': '低', 'Standard': '标准', 'ON': '开启', 'OFF': '关闭'}
OPTIONS = {
    2: {1: '4K', 4: '2.7K', 6: '2.7K 4:3', 7: '1440p', 9: '1080p', 12: '720p',
        18: '4K 4:3', 24: '5K', 25: '5K 4:3', 26: '5.3K 8:7', 27: '5.3K 4:3',
        28: '4K 8:7', 100: '5.3K'},
    3: {0: '240 fps', 1: '120 fps', 2: '100 fps', 3: '90 fps', 5: '60 fps',
        6: '50 fps', 8: '30 fps', 9: '25 fps', 10: '24 fps', 13: '200 fps'},
    121: {0: '广角', 2: '窄视角', 3: 'SuperView', 4: '线性', 7: 'Max SuperView',
          8: '线性＋水平校正', 9: 'HyperView', 10: '线性＋水平锁定'},
    122: {0: '广角 12 MP', 10: '线性 12 MP', 19: '窄视角', 27: '广角 23 MP', 28: '线性 23 MP',
          31: '广角 27 MP', 32: '线性 27 MP', 100: 'Max SuperView', 101: '广角', 102: '线性'},
    123: {19: '窄视角', 31: '广角 27 MP', 32: '线性 27 MP', 100: 'Max SuperView', 101: '广角', 102: '线性'},
    135: {0: '关闭', 1: '开启', 2: '高', 3: 'Boost', 4: '自动 Boost', 100: '标准'},
    156: {1: '15 秒', 2: '30 秒', 3: '1 分钟', 4: '5 分钟', 5: '15 分钟',
          6: '30 分钟', 7: '1 小时', 8: '2 小时', 9: '3 小时', 10: '5 秒', 100: '不限时'},
    5: {0: '0.5 秒', 1: '1 秒', 2: '2 秒', 3: '5 秒', 4: '10 秒', 5: '30 秒',
        6: '1 分钟', 7: '2 分钟', 8: '5 分钟', 9: '30 分钟', 10: '1 小时'},
    30: {100: '1 小时', 101: '30 分钟', 102: '5 分钟', 103: '2 分钟',
         104: '1 分钟', 105: '30 秒', 106: '10 秒', 107: '5 秒', 108: '2 秒', 109: '1 秒', 110: '0.5 秒'},
}
OPTIONS[157] = {0: '关闭', **OPTIONS[156]}
OPTIONS[125] = {0: '标准', 1: 'RAW', 2: 'HDR', 3: 'SuperPhoto'}
FLAT_MODES = {12: 'video', 16: 'photo', 17: 'photo', 13: 'timelapse', 20: 'timelapse',
              21: 'timelapse', 24: 'timelapse', 26: 'timelapse'}
MODE_NAMES = {'FLAT_MODE_VIDEO': 12, 'FLAT_MODE_PHOTO_SINGLE': 16, 'FLAT_MODE_PHOTO': 17,
              'FLAT_MODE_TIME_LAPSE_VIDEO': 13, 'FLAT_MODE_TIME_LAPSE_PHOTO': 20,
              'FLAT_MODE_NIGHT_LAPSE_PHOTO': 21, 'FLAT_MODE_TIME_WARP_VIDEO': 24,
              'FLAT_MODE_NIGHT_LAPSE_VIDEO': 26}
TITLES = {'ACTIVITY': '运动', 'STANDARD': '标准', 'CINEMATIC': '电影', 'PHOTO': '照片',
          'NIGHT': '夜景', 'TIME_WARP': '移动延时', 'TIME_LAPSE': '延时摄影',
          'NIGHT_LAPSE': '夜景延时', 'VIDEO': '视频', 'CUSTOM': '自定义'}
TITLE_IDS = {0: 'ACTIVITY', 1: 'STANDARD', 2: 'CINEMATIC', 3: 'PHOTO', 6: 'NIGHT',
             7: 'TIME_WARP', 8: 'TIME_LAPSE', 9: 'NIGHT_LAPSE', 10: 'VIDEO', 18: 'CUSTOM'}


def option_label(identifier, value):
    return OPTIONS.get(identifier, {}).get(value, '选项 ' + str(value))


def presets(raw):
    result, seen = [], set()
    for group in raw.get('presetGroupArray', []):
        for item in group.get('presetArray', []):
            identifier = item.get('id')
            flat = MODE_NAMES.get(item.get('mode'), item.get('mode'))
            if type(identifier) is not int or not 0 <= identifier <= 0xffffffff or flat not in FLAT_MODES:
                continue
            if identifier in seen:
                raise SourceConnectionError('相机返回重复预设身份，未继续操作')
            seen.add(identifier)
            title = item.get('titleId', '')
            title = TITLE_IDS.get(title, title) if type(title) is int else str(title).removeprefix('PRESET_TITLE_')
            label = str(item.get('customName') or TITLES.get(title) or title or '预设')[:60]
            number = item.get('titleNumber')
            if type(number) is int and number > 0: label += ' ' + str(number)
            values = {str(s['id']): s.get('value') for s in item.get('settingArray', []) if type(s.get('id')) is int}
            specs = [option_label(i, values[str(i)]) for i in (2, 3, 121) if str(i) in values]
            result.append({'id': identifier, 'label': label, 'mode': FLAT_MODES[flat],
                           'flat_mode': flat, 'specification': ' · '.join(specs), 'values': values})
    return result


def decode_options(body, setting):
    """Capabilities repeat TLVs for each option; an empty TLV means unavailable."""
    options = []
    while body:
        if len(body) < 2 or body[0] != setting or body[1] > 4 or len(body) < body[1] + 2:
            raise SourceConnectionError('相机参数能力响应无效')
        length = body[1]
        if length:
            value = int.from_bytes(body[2:2 + length], 'big')
            if value in options: raise SourceConnectionError('相机参数选项重复')
            options.append(value)
        body = body[2 + length:]
    return options


async def choices(camera, identifier):
    return decode_options(await camera.command(0x32, bytes([identifier]), 0x76), identifier)


async def catalog(camera):
    from .gopro_radio import http, http_identity
    await asyncio.to_thread(http_identity, camera.serial)
    available = presets(await asyncio.to_thread(http, '/gopro/camera/presets/get'))
    state = await camera.state()
    raw = await asyncio.to_thread(http, '/gopro/camera/state')
    vocabulary = {}
    if any(str(i) in raw.get('settings', {}) for i in LEGACY):
        try:
            definitions = await asyncio.to_thread(http, '/gp/gpControl')
            for item in definitions.get('settings', []):
                identifier = item.get('id')
                if identifier in LEGACY and item.get('display_name') == LEGACY[identifier]:
                    vocabulary[identifier] = {o['id']: TRANSLATIONS.get(o['display_name'], o['display_name'])[:60]
                        for o in item.get('options', []) if type(o.get('id')) is int and 0 <= o['id'] <= 255
                        and isinstance(o.get('display_name'), str)}
        except (OSError, ValueError, SourceConnectionError):
            pass  # Optional legacy controls must not hide supported standard controls.
    current = next((p for p in available if p['id'] == state['preset']), None)
    parameters = []
    for key, (identifier, label, modes) in PARAMETERS.items():
        if not current or current['mode'] not in modes or str(identifier) not in raw.get('settings', {}):
            continue
        if identifier in LEGACY and identifier not in vocabulary:
            continue
        flat = current['flat_mode']
        if (key == 'video_interval' and flat != 13 or key == 'photo_interval' and flat != 20 or
                key == 'lapse_lens' and flat not in (20, 21) or key == 'lens' and flat in (20, 21)):
            continue
        # Camera capabilities, not the preset's abbreviated UI captions, decide
        # availability (HERO9 omits duration and stabilization from settingArray).
        options = await choices(camera, identifier)
        if identifier in LEGACY:
            options = [v for v in options if v in vocabulary[identifier]]
        if not options: continue
        value = raw.get('settings', {}).get(str(identifier))
        label_for = lambda v: vocabulary.get(identifier, {}).get(v, option_label(identifier, v))
        parameters.append({'key': key, 'label': label, 'value': value,
                           'value_label': label_for(value),
                           'options': [{'value': v, 'label': label_for(v)} for v in options]})
    return {**state, 'presets': available, 'parameters': parameters,
            'preset_label': current['label'] if current else '未知',
            'specification': current['specification'] if current else '', 'observed_at': now()}


async def load_preset(camera, parameters):
    from .gopro_radio import http, http_identity
    await asyncio.to_thread(http_identity, camera.serial)
    available = presets(await asyncio.to_thread(http, '/gopro/camera/presets/get'))
    selected = next((p for p in available if p['id'] == parameters['preset']), None)
    if not selected: raise SourceConnectionError('所选预设已不可用，请重新读取拍摄设置')
    state = await camera.state()
    if state['busy'] or state['recording']:
        raise SourceConnectionError('相机正在拍摄或忙碌，未切换预设')
    if state['preset'] == selected['id']:
        return {**state, 'preset_label': selected['label'], 'already_satisfied': True}
    camera.check_expiry()
    lost = False
    try:
        await camera.command(0x40, b'\x04' + selected['id'].to_bytes(4, 'big'))
    except (TimeoutError, SourceConnectionError):
        lost = True
    for _ in range(8):
        state = await camera.state()
        if state['preset'] == selected['id'] and not state['busy']:
            return {**state, 'preset_label': selected['label'], 'specification': selected['specification'], 'ack_lost': lost}
        await asyncio.sleep(.25)
    return {**state, 'uncertain': True, 'reason': '预设切换结果待核验，未重复发送命令'}


async def set_parameter(camera, parameters):
    key, value = parameters['key'], parameters['value']
    if key not in PARAMETERS:
        raise SourceConnectionError('不支持此拍摄参数，请重新读取拍摄设置')
    identifier = PARAMETERS[key][0]
    current = await catalog(camera)
    if current['preset'] != parameters['expected_preset']:
        raise SourceConnectionError('相机预设已变化，请重新读取拍摄设置')
    offered = next((p for p in current['parameters'] if p['key'] == key), None)
    if not offered or value not in [o['value'] for o in offered['options']]:
        raise SourceConnectionError('此参数组合当前不可用，请重新读取拍摄设置')
    if offered['value'] == value:
        return {**current, 'already_satisfied': True}
    camera.check_expiry()
    # Read capabilities again immediately before the single write.
    if value not in await choices(camera, identifier):
        raise SourceConnectionError('相机参数选项已变化，未修改')
    state = await camera.state()
    if state['preset'] != parameters['expected_preset'] or state['busy'] or state['recording']:
        raise SourceConnectionError('相机预设或拍摄状态已变化，未修改参数')
    camera.check_expiry()
    lost = False
    try:
        await camera.command(identifier, bytes([1, value]), 0x74)
    except (TimeoutError, SourceConnectionError):
        lost = True
    from .gopro_radio import decode_statuses
    for _ in range(8):
        actual = decode_statuses(await camera.command(0x12, bytes([identifier]), 0x76)).get(identifier)
        state = await camera.state()
        if state['preset'] != parameters['expected_preset']: break
        if actual == value:
            return {**state, 'parameter_label': offered['label'],
                    'value_label': next(o['label'] for o in offered['options'] if o['value'] == actual), 'ack_lost': lost}
        await asyncio.sleep(.25)
    return {**state, 'uncertain': True, 'reason': '设置结果待核验，未重复发送命令'}
