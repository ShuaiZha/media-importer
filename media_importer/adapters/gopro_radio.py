"""Minimal Open GoPro BLE control; no media deletion or proprietary pairing reset.

Protocol: https://gopro.github.io/OpenGoPro/docs/ble/
Wi-Fi association is implemented only for the tested macOS native deployment.
"""
import asyncio
import json
import re
import subprocess
import time
from urllib.request import build_opener, ProxyHandler

from ..model import SourceConnectionError, now
from .gopro_models import compatibility

WIFI_URL = 'http://10.5.5.9:8080'


def characteristic(number):
    return f'b5f9{number:04x}-aa8d-11e3-9046-0002a5d5c51b'


class Packets:
    def __init__(self):
        self.buffer, self.length, self.sequence = bytearray(), 0, 0

    def feed(self, packet):
        if not packet:
            raise ValueError('Empty frame')
        header = packet[0]
        if header & 0x80:
            if not self.length or header & 0x7f != self.sequence:
                raise ValueError('Invalid continuation')
            self.sequence = (self.sequence + 1) % 128
            self.buffer.extend(packet[1:])
        else:
            size = {0: 1, 1: 2, 2: 3}.get(header >> 5, 0)
            if not size or len(packet) < size:
                raise ValueError('Invalid header')
            self.length = header & 31 if size == 1 else (((header & 31) << 8 | packet[1])
                          if size == 2 else int.from_bytes(packet[1:3], 'big'))
            self.buffer, self.sequence = bytearray(packet[size:]), 0
        if not 0 < self.length <= 8192 or len(self.buffer) > self.length:
            raise ValueError('Invalid length')
        if len(self.buffer) == self.length:
            self.length = 0
            return bytes(self.buffer)
        return None


def hardware(body):
    fields = []
    while body:
        size = body[0]
        if len(body) <= size:
            raise ValueError('Incomplete identity')
        fields.append(body[1:1 + size]); body = body[1 + size:]
    if len(fields) < 5:
        raise ValueError('Missing identity')
    model, firmware, serial = (fields[i].decode('utf-8').rstrip('\0') for i in (1, 3, 4))
    if not serial or not serial.isalnum():
        raise ValueError('Invalid serial')
    return {'model_name': model, 'firmware_version': firmware, 'serial_number': serial}


def decode_statuses(body):
    result = {}
    while body:
        if len(body) < 2 or len(body) < body[1] + 2 or body[0] in result:
            raise ValueError('Invalid status response')
        key, size = body[:2]
        result[key] = int.from_bytes(body[2:2 + size], 'big')
        body = body[2 + size:]
    return result


def http(path):
    with build_opener(ProxyHandler({})).open(WIFI_URL + path, timeout=3) as response:
        return json.loads(response.read(1024 * 1024))


def http_identity(serial):
    raw = http('/gp/gpControl/info')
    info = raw.get('info', raw)
    if info.get('serial_number') != serial:
        raise SourceConnectionError('无线相机身份不符，未继续连接或操作')
    return info


# Keep adapter-local names for the protocol workflow; implementation is host-owned.
from ..host_network import route, wifi_interface, join_wifi


class Camera:
    def __init__(self, serial):
        self.serial, self.client, self.expires_at = serial, None, None
        self.incoming, self.packets = asyncio.Queue(), {}

    async def __aenter__(self):
        from bleak import BleakClient, BleakScanner
        def matches(device, advertisement):
            name = advertisement.local_name or device.name or ''
            return name.lower().startswith('gopro') and (not self.serial or name.endswith(self.serial[-4:]))
        device = await BleakScanner.find_device_by_filter(matches, timeout=10)
        if device is None:
            raise SourceConnectionError('未发现相机蓝牙；请保持开机、开启无线连接并退出手机 Quik')
        self.client = BleakClient(device, timeout=20)
        try:
            await asyncio.wait_for(self.client.connect(), 25)
            for service in self.client.services:
                for char in service.characteristics:
                    if 'notify' in char.properties:
                        callback = self.receive if char.uuid in (characteristic(0x73), characteristic(0x77)) else lambda *_: None
                        await asyncio.wait_for(self.client.start_notify(char, callback), 5)
            identity = hardware(await self.command(0x3c))
            if self.serial and identity['serial_number'] != self.serial:
                raise SourceConnectionError('蓝牙相机序列号不符，未执行操作')
            self.identity, self.serial = identity, identity['serial_number']
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.client and self.client.is_connected:
            try:
                await asyncio.wait_for(self.client.disconnect(), 3)
            except Exception:
                pass

    async def __aexit__(self, *_):
        await self.close()

    def receive(self, char, packet):
        try:
            body = self.packets.setdefault(char.uuid, Packets()).feed(bytes(packet))
            if body is not None:
                self.incoming.put_nowait((char.uuid, body))
        except ValueError:
            self.incoming.put_nowait((char.uuid, b''))

    async def command(self, code, payload=b'', channel=0x72):
        while not self.incoming.empty():
            self.incoming.get_nowait()
        await asyncio.wait_for(self.client.write_gatt_char(characteristic(channel),
            bytes([len(payload) + 1, code]) + payload, response=True), 6)
        async with asyncio.timeout(8):
            while True:
                sender, body = await self.incoming.get()
                if sender != characteristic(channel + 1) or not body or body[0] != code:
                    continue
                if len(body) < 2 or body[1] != 0:
                    raise SourceConnectionError('相机拒绝本次命令，请检查当前模式和忙碌状态')
                return body[2:]

    async def statuses(self, *ids):
        result = decode_statuses(await self.command(0x13, bytes(ids), 0x76))
        if not set(ids) <= set(result):
            raise SourceConnectionError('相机未返回所需状态，未继续执行拍摄')
        return result

    async def state(self):
        values = await self.statuses(8, 10, 38, 39, 96, 97)
        return {'recording': bool(values[10]), 'busy': bool(values[8]),
                'mode': {1000: 'video', 1001: 'photo'}.get(values[96], 'other'),
                'preset': values[97], 'photos': values[38], 'videos': values[39], 'observed_at': now()}

    def check_expiry(self):
        if self.expires_at is not None and time.time() >= self.expires_at:
            raise SourceConnectionError('连接耗时过长，拍摄命令已过期；请刷新状态后重新操作')

    async def set_mode(self, mode):
        # JSON uses protobuf enum names on HERO9; BLE uses numeric group IDs.
        # Preset IDs vary by camera. Discover them instead of assuming 0 / 65536.
        # Loading a group alone could select burst, live-burst or looping capture.
        if mode not in ('video', 'photo'):
            raise SourceConnectionError('不支持的拍摄模式')
        value = 1000 if mode == 'video' else 1001
        await asyncio.to_thread(http_identity, self.serial)
        raw = await asyncio.to_thread(http, '/gopro/camera/presets/get')
        groups = raw.get('presetGroupArray', [])
        presets = [p for g in groups if g.get('id') in (value, f'PRESET_GROUP_ID_{mode.upper()}')
                   for p in g.get('presetArray', [])
                   if p.get('mode') == f'FLAT_MODE_{mode.upper()}'
                   and type(p.get('id')) is int and 0 <= p['id'] <= 0xffffffff]
        if not presets:
            raise SourceConnectionError('此相机未报告普通视频／单张照片预设，暂不支持该模式切换')
        # Retain the most recently used normal preset (including its resolution).
        remembered_id = 93 if mode == 'video' else 94
        remembered = (await self.statuses(remembered_id))[remembered_id]
        preset = next((p for p in presets if p['id'] == remembered),
                      next((p for p in presets if not p.get('userDefined')), presets[0]))
        self.check_expiry()
        await self.command(0x40, bytes([4]) + preset['id'].to_bytes(4, 'big'))
        for _ in range(8):
            state = await self.state()
            if state['mode'] == mode and state['preset'] == preset['id'] and not state['busy']:
                return state
            await asyncio.sleep(.25)
        raise SourceConnectionError('模式切换结果尚未确认，请刷新相机状态')

    async def execute(self, action):
        before = await self.state()
        if action == 'state':
            return before
        if action == 'stop_recording' and not before['recording']:
            return {**before, 'already_satisfied': True}
        if action == 'start_recording' and before['recording']:
            return {**before, 'already_satisfied': True}
        if action != 'stop_recording' and (before['recording'] or before['busy']):
            raise SourceConnectionError('相机正在录像或忙碌；请先停止录像并等待完成')
        if action in ('mode_video', 'mode_photo'):
            return await self.set_mode(action.removeprefix('mode_'))
        desired = 'photo' if action == 'photo' else 'video'
        if action == 'photo' or (action != 'stop_recording' and before['mode'] != desired):
            before = await self.set_mode(desired)
        self.check_expiry()
        # Never retry shutter writes: an ACK timeout can mean the operation succeeded.
        uncertain = False
        try:
            await self.command(1, bytes([1, int(action != 'stop_recording')]))
        except (TimeoutError, SourceConnectionError):
            uncertain = True
        for _ in range(12):
            after = await self.state()
            done = (after['photos'] > before['photos'] and not after['busy'] if action == 'photo' else
                    after['recording'] == (action == 'start_recording') and
                    (action != 'stop_recording' or not after['busy']))
            if done:
                return {**after, 'ack_lost': uncertain}
            await asyncio.sleep(.4)
        return {**after, 'uncertain': True, 'reason': '命令结果未确认，请刷新状态；不会自动重复拍摄'}


async def perform(action, serial, expires_at=None):
    if action != 'discover' and not serial:
        raise SourceConnectionError('请先通过飞书确认相机身份')
    interface = uplink = None
    if action == 'connect':
        interface = await asyncio.to_thread(wifi_interface)
        uplink = await asyncio.to_thread(route)
        # Do not take over the only uplink. Tailscale/PPP default routes are not
        # sufficient proof of independent Ethernet connectivity.
        if not uplink or uplink == interface or not uplink.startswith('en'):
            raise SourceConnectionError('自动连接需要独立的有线上网链路；请连接网线，或先手动连接相机 Wi-Fi')
    async with Camera(serial) as camera:
        camera.expires_at = expires_at
        profile = compatibility(camera.identity['model_name'], camera.identity['firmware_version'])
        if not profile['import_supported']:
            raise SourceConnectionError('已识别相机，但此型号尚未适配无线操作')
        device = {**camera.identity, 'type': 'gopro', 'url': WIFI_URL, 'transport': 'wifi',
                  'http_ready': False, 'compatibility': {**profile, 'delete_verified': False}}
        if action == 'discover':
            return device
        if action == 'connect':
            ssid = bytes(await camera.client.read_gatt_char(characteristic(2))).decode().rstrip('\0')
            password = bytes(await camera.client.read_gatt_char(characteristic(3))).decode().rstrip('\0')
            if not ssid or not password:
                raise SourceConnectionError('相机未提供热点凭据')
            await camera.command(0x17, bytes([1, 1]))
            for _ in range(15):
                if (await camera.statuses(69))[69] == 1:
                    break
                await asyncio.sleep(1)
            else:
                raise SourceConnectionError('相机热点尚未就绪，请稍后重试')
            # The AP flag can precede the first visible beacon. Retrying only
            # that transient OS result avoids requiring a second user action.
            for attempt in range(3):
                if await asyncio.to_thread(join_wifi, interface, ssid, password) != 'not_found':
                    break
                if attempt == 2:
                    raise SourceConnectionError('相机热点已开启，但 Mac 暂未发现网络；等待下一次重试')
                await asyncio.sleep(4)
            password = None
            for _ in range(10):
                try:
                    await asyncio.to_thread(http_identity, serial)
                    break
                except SourceConnectionError:
                    raise
                except Exception:
                    await asyncio.sleep(1)
            else:
                raise SourceConnectionError('Wi-Fi 连接命令已结束，但相机 HTTP 不可访问')
            if await asyncio.to_thread(route) != uplink:
                raise SourceConnectionError('上网链路已变化，暂不开始传输，请检查有线网络')
            return {**device, 'http_ready': True}
        # BLE has already verified the full serial. A lost HTTP media route
        # must not prevent stopping an active recording or reading its state.
        return await camera.execute(action)


def run(action, serial, expires_at=None):
    return asyncio.run(asyncio.wait_for(perform(action, serial, expires_at), 130 if action == 'connect' else 60))
