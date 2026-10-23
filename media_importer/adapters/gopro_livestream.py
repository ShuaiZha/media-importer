"""HERO9 station-network and RTMP protocol; no SD recording or file deletion."""
import asyncio
import time
from .gopro_radio import Camera, characteristic, decode_statuses
from ..model import SourceConnectionError

def varint(value):
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128)
        value >>= 7
    return bytes(data + bytes([value]))

def field(number, value):
    if isinstance(value, (str, bytes)):
        value = value.encode() if isinstance(value, str) else value
        return varint(number << 3 | 2) + varint(len(value)) + value
    return varint(number << 3) + varint(int(value))

def decode(data):
    index, result = 0, {}
    def read():
        nonlocal index
        value = shift = 0
        while index < len(data) and shift < 70:
            b = data[index]; index += 1
            value |= (b & 127) << shift
            if not b & 128: return value
            shift += 7
        raise ValueError('Invalid protobuf varint')
    while index < len(data):
        tag = read(); number, wire = tag >> 3, tag & 7
        if wire == 0: value = read()
        elif wire == 2:
            length = read(); value = data[index:index + length]; index += length
            if len(value) != length: raise ValueError('Incomplete protobuf')
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
            value = data[index:index + length]; index += length
        else: raise ValueError('Unsupported protobuf type')
        result.setdefault(number, []).append(value)
    return result

def one(data, key, default=None):
    return data.get(key, [default])[0]

class LiveCamera(Camera):
    async def __aenter__(self):
        await super().__aenter__()
        self.events = []
        await self.client.stop_notify(characteristic(0x92))
        await self.client.start_notify(characteristic(0x92), self.receive)
        return self

    async def proto(self, channel, feature, action, payload=b'', timeout=12):
        body = bytes([feature, action]) + payload
        if len(body) >= 8192: raise SourceConnectionError('直播请求过长')
        header = bytes([0x20 | (len(body) >> 8), len(body) & 255])
        packets = [header + body[:18]]
        packets += [bytes([0x80 | (i % 128)]) + body[start:start + 19]
                    for i, start in enumerate(range(18, len(body), 19))]
        for packet in packets:
            await self.client.write_gatt_char(characteristic(channel), packet, response=True)
        return await self.wait_proto(channel + 1, feature, action | 128, timeout)

    async def wait_proto(self, channel, feature, action, timeout=20):
        for index, (sender, body) in enumerate(self.events):
            if sender == characteristic(channel) and body[:2] == bytes([feature, action]):
                self.events.pop(index); return decode(body[2:])
        async with asyncio.timeout(timeout):
            while True:
                sender, body = await self.incoming.get()
                if sender == characteristic(channel) and body[:2] == bytes([feature, action]):
                    return decode(body[2:])
                self.events.append((sender, body))

    async def live_status(self):
        return await self.proto(0x76, 0xf5, 0x74)

    async def is_live(self):
        result = await self.live_status()
        return one(result, 1) == 3 and not one(result, 3, False)

    async def keep(self):
        await self.command(91, b'\x01\x42', 0x74)

    async def before(self):
        if self.identity['model_name'] != 'HERO9 Black':
            raise SourceConnectionError('有声直播目前仅验证 HERO9 Black')
        state = await self.state()
        if state['recording'] or state['busy'] or (await self.statuses(32))[32] or one(await self.live_status(), 1) != 0:
            raise SourceConnectionError('相机正在使用中，请先结束拍摄或预览')
        state['settings'] = {str(i): decode_statuses(await self.command(0x12, bytes([i]), 0x76))[i] for i in (47, 64)}
        return state

    async def join(self, ssid, password, stop):
        def check():
            if stop.is_set(): raise SourceConnectionError('直播已取消')
        check()
        paired = await self.proto(0x91, 3, 1, field(1, 0) + field(2, 'Media Importer'))
        if one(paired, 1) != 1: raise SourceConnectionError('相机拒绝直播网络会话')
        await self.keep()
        if one(await self.proto(0x91, 2, 2), 1) != 1:
            raise SourceConnectionError('相机无法扫描 Wi-Fi')
        until = time.monotonic() + 30
        while True:
            check()
            result = await self.wait_proto(0x92, 2, 0x0b, max(1, until - time.monotonic()))
            if one(result, 1) not in (0, 1, 2): break
            if time.monotonic() >= until: raise SourceConnectionError('Wi-Fi 扫描超时')
        if one(result, 1) != 5: raise SourceConnectionError('Wi-Fi 扫描失败')
        entries = await self.proto(0x91, 2, 3, field(1, 0) + field(2, one(result, 3, 0)) + field(3, one(result, 2)))
        entry = next((x for x in map(decode, entries.get(3, [])) if one(x, 1, b'').decode() == ssid), None)
        if entry is None: raise SourceConnectionError('未发现配置的直播 Wi-Fi；请先开启热点并检查名称')
        check()
        await self.keep()
        await self.command(0x17, b'\x01\x00')
        await asyncio.sleep(1)
        # Submit the configured password even for remembered networks, so credential
        # changes are not silently ignored. The camera owns network persistence.
        result = await self.proto(0x91, 2, 5, field(1, ssid) + field(2, password))
        if one(result, 1) != 1: raise SourceConnectionError('相机拒绝直播网络连接')
        state = one(result, 2)
        until = time.monotonic() + 90
        while state in (0, 1, 2) and time.monotonic() < until:
            check()
            try:
                state = one(await self.wait_proto(0x92, 2, 0x0c, timeout=3), 1)
            except TimeoutError:
                await self.keep()
        if state not in (5, 6): raise SourceConnectionError('直播 Wi-Fi 连接失败，请检查密码、信号和网络可用性')

    async def start_live(self, url, stop):
        if stop.is_set(): raise SourceConnectionError('直播已取消')
        result = await self.proto(0x72, 0xf1, 0x79, field(1, url) + field(2, False) + field(3, 7)
                                  + field(7, 800) + field(8, 1800) + field(9, 1200))
        if one(result, 1) != 1: raise SourceConnectionError('相机拒绝直播设置')
        until = time.monotonic() + 25
        while time.monotonic() < until:
            if stop.is_set(): raise SourceConnectionError('直播已取消')
            state = one(await self.live_status(), 1)
            if state == 2: break
            if state in (4, 5, 7): raise SourceConnectionError('相机未能准备直播')
            await self.keep(); await asyncio.sleep(.5)
        else: raise SourceConnectionError('准备直播超时')
        await asyncio.sleep(2)
        if stop.is_set(): raise SourceConnectionError('直播已取消')
        await self.keep()
        # Never repeat a shutter start on an uncertain response.
        await self.command(1, b'\x01\x01')
        for _ in range(20):
            if stop.is_set(): raise SourceConnectionError('直播已取消')
            state = await self.live_status()
            if one(state, 3, False): raise SourceConnectionError('相机报告存储卡录制开启，正在停止直播')
            if one(state, 1) == 3: return
            if one(state, 1) in (4, 5, 7): break
            await asyncio.sleep(.5)
        raise SourceConnectionError('直播启动结果未确认')

    async def restore(self, before, close_ingress):
        """Verify idle before releasing station ownership and restoring the AP."""
        async def attempt(call):
            try: await call()
            except (TimeoutError, SourceConnectionError): pass
        if one(await self.live_status(), 1) != 0:
            await attempt(lambda: self.command(1, b'\x01\x00'))
            await attempt(lambda: self.proto(0x72, 0xf1, 0x79, field(1, '')))
        await close_ingress()
        until = time.monotonic() + 55
        while time.monotonic() < until:
            state = await self.state()
            if one(await self.live_status(), 1) == 0 and not state['busy'] and not state['recording']: break
            await self.keep(); await asyncio.sleep(1)
        else: raise SourceConnectionError('直播停止结果待核验，请恢复相机连接后重试结束直播')
        if state['preset'] != before['preset']:
            await self.command(0x40, b'\x04' + before['preset'].to_bytes(4, 'big'))
        from .gopro_capture import choices
        for key, original in before['settings'].items():
            key = int(key)
            current = decode_statuses(await self.command(0x12, bytes([key]), 0x76)).get(key)
            if current != original:
                if original not in await choices(self, key): raise SourceConnectionError('原拍摄参数无法恢复，请检查相机状态')
                await self.command(key, bytes([1, original]), 0x74)
            for _ in range(10):
                if decode_statuses(await self.command(0x12, bytes([key]), 0x76)).get(key) == original: break
                await asyncio.sleep(.3)
            else: raise SourceConnectionError('拍摄参数恢复结果待核验')
        if (await self.state())['preset'] != before['preset']: raise SourceConnectionError('拍摄预设恢复结果待核验')
        await attempt(lambda: self.proto(0x72, 0xf1, 0x78))
        await attempt(lambda: self.command(0x17, b'\x01\x01'))
        until = time.monotonic() + 65
        while time.monotonic() < until:
            if (await self.statuses(69))[69] == 1: return
            await self.keep(); await asyncio.sleep(1)
        raise SourceConnectionError('直播已停止，相机热点未恢复，请重新开启相机无线连接')
