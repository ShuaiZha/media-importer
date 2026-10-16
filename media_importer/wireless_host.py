"""Optional macOS radio helper. It never opens SQLite or reads/writes camera media."""
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from urllib.request import build_opener, ProxyHandler, Request, HTTPRedirectHandler

from .wireless_control import demand, write_json

SERVICE_UUID = '0000fea6-0000-1000-8000-00805f9b34fb'


def characteristic(value):
    return f'b5f9{value:04x}-aa8d-11e3-9046-0002a5d5c51b'


class LinkError(Exception):
    pass


class Packets:
    """Open GoPro BLE length header and continuation framing, bounded to 8 KiB."""
    def __init__(self):
        self.data, self.length, self.sequence = bytearray(), 0, 0

    def feed(self, packet):
        if not packet:
            raise LinkError('相机蓝牙响应为空')
        first = packet[0]
        if first & 0x80:
            if not self.length or first & 0x7f != self.sequence:
                raise LinkError('相机蓝牙分包顺序异常')
            self.sequence = (self.sequence + 1) % 128
            self.data.extend(packet[1:])
        else:
            kind = first >> 5
            header = 1 if kind == 0 else 2 if kind == 1 else 3 if kind == 2 else 0
            if not header or len(packet) < header:
                raise LinkError('相机蓝牙响应头无效')
            self.length = first & 31 if header == 1 else ((first & 31) << 8 | packet[1]) if header == 2 else int.from_bytes(packet[1:3], 'big')
            self.data, self.sequence = bytearray(packet[header:]), 0
        if not 0 < self.length <= 8192 or len(self.data) > self.length:
            raise LinkError('相机蓝牙响应长度异常')
        if len(self.data) == self.length:
            result = bytes(self.data)
            self.length = 0
            return result
        return None


def hardware_serial(body):
    # Hardware-info fields are length-prefixed; model number/board are 4-byte TLVs.
    fields = []
    while body:
        size = body[0]
        if len(body) < size + 1:
            raise LinkError('相机蓝牙身份响应不完整')
        fields.append(body[1:1 + size])
        body = body[1 + size:]
    if len(fields) < 5:
        raise LinkError('相机蓝牙身份响应缺少序列号')
    try:
        return fields[4].decode('ascii')
    except UnicodeError:
        raise LinkError('相机蓝牙身份响应无效') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def camera_identity(url, serial):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(Request(url + '/gp/gpControl/info'), timeout=3) as response:
            value = json.loads(response.read(65536))
        actual = value.get('info', value).get('serial_number')
    except Exception:
        return False
    if actual != serial:
        raise LinkError('无线地址上的相机序列号不符；未处理该设备')
    return True


class Helper:
    def __init__(self, env, root, stop=None):
        from .deployment import text, number
        self.env = env
        self.root = Path(root)
        self.root_identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        self.serial = text(env, 'SOURCE_SERIAL', required=True)
        self.url = text(env, 'SOURCE_WIRELESS_URL', 'http://10.5.5.9:8080').rstrip('/')
        from .adapters.gopro import validate
        validate({'type': 'gopro', 'url': self.url, 'wireless_url': self.url, 'serial': self.serial, 'timeout_seconds': 3})
        self.ble_id = text(env, 'SOURCE_BLUETOOTH_ID')
        self.ssid = None
        self.ap_ready_until = 0
        self.scan_timeout = number(env, 'SOURCE_BLUETOOTH_SCAN_SECONDS', 10)
        self.connect_timeout = number(env, 'SOURCE_BLUETOOTH_CONNECT_SECONDS', 45)
        self.retry_seconds = number(env, 'SOURCE_WIRELESS_RETRY_SECONDS', 10)
        if not (3 <= self.scan_timeout <= 30 and 10 <= self.connect_timeout <= 90 and 1 <= self.retry_seconds <= 60):
            raise LinkError('无线扫描、连接或重试间隔超出允许范围；检查 .env.example')
        self.stop = stop or asyncio.Event()
        self.attempt = 0
        self.via = None
        self.state, self.message = 'idle', '无线助手待命；同步关闭时不连接相机'

    def control(self):
        return demand(self.root, self.serial, self.url)

    def report(self, state, message, **extra):
        if (self.root.stat().st_dev, self.root.stat().st_ino) != self.root_identity:
            raise LinkError('原数据盘不可访问或身份已改变；不会创建替代目录')
        self.state, self.message = state, message
        write_json(self.root / 'wireless/status.json', {
            'serial': self.serial, 'updated_at': time.time(), 'state': state, 'message': message,
            'attempt': self.attempt, 'via': self.via, **extra,
        })

    async def bluetooth(self):
        from bleak import BleakClient, BleakScanner
        self.report('scanning', '正在寻找绑定相机的蓝牙广播')
        def match(device, advertisement):
            if self.ble_id:
                return device.address.lower() == self.ble_id.lower()
            name = advertisement.local_name or device.name or ''
            return name.lower().startswith('gopro') and name.endswith(self.serial[-4:])
        device = await BleakScanner.find_device_by_filter(match, timeout=self.scan_timeout, service_uuids=[SERVICE_UUID])
        if device is None:
            raise LinkError('未发现相机蓝牙；开启无线连接，首次进入 Quik 配对界面，并退出手机 Quik')
        self.report('connecting_bluetooth', '正在连接蓝牙；首次配对请确认 macOS 提示')
        client = BleakClient(device, timeout=self.connect_timeout)
        try:
            await asyncio.wait_for(client.connect(), self.connect_timeout + 5)
            incoming = asyncio.Queue()
            packets = {}
            def receive(char, data):
                try:
                    value = packets.setdefault(char.uuid, Packets()).feed(bytes(data))
                    if value is not None:
                        incoming.put_nowait((char.uuid, value))
                except LinkError as exc:
                    incoming.put_nowait(exc)
            # GoPro requires subscribing to all response characteristics after pairing.
            for service in client.services:
                for char in service.characteristics:
                    if 'notify' in char.properties:
                        await client.start_notify(char, receive if char.uuid in (characteristic(0x73), characteristic(0x77)) else lambda *_: None)
            async def command(code, payload=b'', channel=0x72):
                await client.write_gatt_char(characteristic(channel), bytes([1 + len(payload), code]) + payload, response=True)
                async with asyncio.timeout(12):
                    while True:
                        item = await incoming.get()
                        if isinstance(item, Exception):
                            raise item
                        sender, response = item
                        if sender != characteristic(channel + 1) or response[0] != code:
                            continue
                        if len(response) < 2 or response[1] != 0:
                            raise LinkError('相机拒绝蓝牙命令；退出相机菜单并等待就绪后重试')
                        return response[2:]
            hardware = None
            for attempt in range(3):
                try:
                    hardware = await command(0x3c)
                    break
                except (LinkError, TimeoutError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1)
            if hardware_serial(hardware) != self.serial:
                raise LinkError('蓝牙相机序列号不符；未开启该设备的热点')
            ssid = bytes(await client.read_gatt_char(characteristic(2))).decode().rstrip('\0')
            if not ssid:
                raise LinkError('相机未返回有效的 Wi-Fi 名称')
            await command(0x17, bytes([1, 1]))
            for _ in range(15):
                # Official readiness is Status 69 (AP Mode), not the legacy GP-0005 byte.
                state = await command(0x13, bytes([69]), channel=0x76)
                if len(state) >= 3 and state[0] == 69 and len(state) == state[1] + 2 and int.from_bytes(state[2:], 'big') == 1:
                    return ssid
                await asyncio.sleep(1)
            raise LinkError('相机热点启动超时，稍后重试')
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except Exception:
                pass

    async def bluetooth_bounded(self):
        # CoreBluetooth may hang while cancelling its own connect/disconnect. A
        # bounded child process prevents it from trapping the sync-off operation.
        self.report('connecting_bluetooth', '正在搜索并连接绑定相机；首次配对请确认 macOS 提示')
        keys = ('SOURCE_SERIAL', 'SOURCE_WIRELESS_URL', 'SOURCE_BLUETOOTH_ID',
                'SOURCE_BLUETOOTH_SCAN_SECONDS', 'SOURCE_BLUETOOTH_CONNECT_SECONDS')
        payload = {'env': {key: self.env[key] for key in keys if key in self.env}, 'root': str(self.root)}
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'media_importer.wireless_host', '--ble-handshake',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parents[1]))
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), self.scan_timeout + self.connect_timeout + 35)
            if process.returncode:
                raise LinkError('蓝牙连接失败；检查 Mac 蓝牙权限、相机配对状态，并退出手机 Quik')
            value = json.loads(output)
            if value.get('error'):
                raise LinkError(value['error'])
            return value['ssid']
        except (TimeoutError, ValueError, KeyError):
            raise LinkError('蓝牙连接超时；请确认首次配对提示，助手将自动重试') from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def connect(self):
        self.attempt += 1
        if await asyncio.to_thread(camera_identity, self.url, self.serial):
            self.via = self.via or 'existing_wifi'
            self.report('ready', '无线相机身份核对通过')
            return
        if time.monotonic() >= self.ap_ready_until:
            self.ssid = await self.bluetooth_bounded()
            self.ap_ready_until = time.monotonic() + 60
            self.via = 'bluetooth'
        self.report('waiting_wifi', f'相机热点已开启；请在 Mac Wi-Fi 菜单连接「{self.ssid}」，相机保持预览画面')

    async def run(self):
        task, next_try = None, 0
        try:
            while not self.stop.is_set():
                control = self.control()
                enabled = control.get('enabled') is True
                if not enabled:
                    if task:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        task = None
                    self.attempt, next_try, self.ap_ready_until = 0, 0, 0
                    self.report('idle', '无线助手待命；同步关闭时不连接相机')
                elif task and task.done():
                    try:
                        task.result()
                        next_try = time.monotonic() + 10
                    except Exception as exc:
                        # Never persist raw Bluetooth exception values.
                        message = str(exc) if isinstance(exc, LinkError) else '蓝牙或 Wi-Fi 连接失败；检查 Mac 蓝牙权限及相机配对状态'
                        delay = min(self.retry_seconds * 2 ** min(self.attempt - 1, 3), 60)
                        self.report('retrying', message + f'；{delay:g} 秒后重试')
                        next_try = time.monotonic() + delay
                    task = None
                elif not task and control.get('needs_camera') and time.monotonic() >= next_try:
                    task = asyncio.create_task(self.connect())
                # A heartbeat stays fresh even during bounded BLE connect or backoff.
                self.report(self.state, self.message)
                await asyncio.sleep(.5)
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if self.root.is_dir():
                self.report('stopped', '蓝牙助手已停止；Wi-Fi 由 Mac 手动管理')


def main(argv=None):
    import argparse
    import fcntl
    from dotenv import dotenv_values
    if (argv if argv is not None else sys.argv[1:]) == ['--ble-handshake']:
        value = json.loads(sys.stdin.read(16384))
        helper = Helper(value['env'], value['root'])
        helper.report = lambda *_args, **_kwargs: None
        async def handshake():
            try:
                return {'ssid': await helper.bluetooth()}
            except Exception as exc:
                return {'error': str(exc) if isinstance(exc, LinkError) else '蓝牙连接失败或超时；检查配对界面及 Mac 蓝牙权限'}
        # Return only the network name; no Wi-Fi credentials are read or stored.
        sys.stdout.write(json.dumps(asyncio.run(handshake())))
        return
    parser = argparse.ArgumentParser(description='Mac 无线连接助手：不传输素材，不打开任务数据库')
    parser.add_argument('--env', type=Path, default=Path('.env'))
    args = parser.parse_args(argv)
    if sys.platform != 'darwin':
        parser.error('当前无线助手仅验收 macOS；其他系统仍可使用 USB 模式')
    env = dotenv_values(args.env, interpolate=False)
    root = Path(env.get('MEDIA_IMPORTER_DATA_HOST_DIR', './data'))
    if not root.is_absolute():
        root = args.env.resolve().parent / root
    if not root.is_dir():
        parser.error('数据目录不存在；连接原数据盘后再启动，不会创建替代目录')
    root.joinpath('wireless').mkdir(exist_ok=True)
    with open(root / 'wireless/helper.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            parser.error('此数据目录已有无线助手运行')
        async def run():
            helper = Helper(env, root)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, helper.stop.set)
            await helper.run()
        try:
            asyncio.run(run())
        except (LinkError, ValueError) as exc:
            parser.error(str(exc))


if __name__ == '__main__':
    main()
