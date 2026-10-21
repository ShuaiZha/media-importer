"""Bounded native radio work, isolated from importer, bot and archive threads."""
import json
import os
import signal
import subprocess
import sys
import threading
import time

from .model import Cancelled, SourceConnectionError

_LOCK = threading.Lock()
_PRIORITY = threading.Event()
ACTIONS = {'discover', 'connect', 'state', 'start_recording', 'stop_recording', 'photo', 'mode_video', 'mode_photo',
           'catalog', 'load_preset', 'set_parameter', 'start_timelapse', 'hilight'}


def request(adapter, action, serial, stop=None, *, parameters=None):
    if sys.platform != 'darwin' or os.environ.get('MEDIA_IMPORTER_RUNTIME') != 'native':
        raise SourceConnectionError('无线自动连接和蓝牙控制仅支持 Mac 原生版')
    if action not in ACTIONS:
        raise ValueError('Unsupported radio operation')
    deadline = time.monotonic() + (140 if action == 'connect' else 65)
    mutating = action not in ('discover', 'connect', 'state', 'catalog')
    priority = action == 'stop_recording'
    if priority: _PRIORITY.set()
    acquired = False
    try:
        while not acquired:
            if priority or not _PRIORITY.is_set():
                acquired = _LOCK.acquire(timeout=.1)
            else:
                time.sleep(.1)
            if acquired: break
            if mutating and not priority:
                raise SourceConnectionError('无线连接正在调整，未发送拍摄命令；请稍后重新操作')
            if stop and stop.is_set():
                raise Cancelled('无线操作已停止')
            if time.monotonic() >= deadline:
                raise SourceConnectionError('蓝牙正在处理其他操作，请稍后重试')
    except BaseException:
        if priority: _PRIORITY.clear()
        raise
    process = None
    try:
        if stop and stop.is_set():
            raise Cancelled('无线操作已停止')
        command = ([sys.executable, '_worker', 'radio'] if getattr(sys, 'frozen', False)
                   else [sys.executable, '-m', 'media_importer.radio'])
        # No passwords in argv, output or persistent messages. Each child owns
        # CoreBluetooth's event loop; a stuck framework can be terminated safely.
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        payload = json.dumps({'adapter': adapter, 'action': action, 'serial': serial,
                              'parameters': parameters or {},
                              'expires_at': time.time() + 30 if mutating else None})
        while True:
            if not mutating and _PRIORITY.is_set():
                raise Cancelled('无线连接操作让出通道，优先处理停止录像')
            if stop and stop.is_set():
                raise Cancelled('无线操作已停止；已发出的拍摄命令需查询结果')
            if time.monotonic() >= deadline:
                raise SourceConnectionError('无线操作超时；请检查相机无线连接和 Mac 蓝牙权限')
            try:
                output, _ = process.communicate(payload, timeout=.2)
                break
            except subprocess.TimeoutExpired:
                payload = None
        try:
            result = json.loads(output)
        except ValueError:
            raise SourceConnectionError('无线工作进程异常退出，请检查蓝牙权限；上传不受影响') from None
        if process.returncode or not result.get('ok'):
            raise SourceConnectionError(result.get('error') or '无线操作未完成')
        return result['result']
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        _LOCK.release()
        if priority: _PRIORITY.clear()


def main():
    try:
        value = json.loads(sys.stdin.read(4096))
        if value.get('action') not in ACTIONS:
            raise ValueError('Invalid operation')
        from .adapters import _module, SOURCES
        module = _module(SOURCES, value['adapter'], '来源')
        parameters = value.get('parameters') or {}
        if not isinstance(parameters, dict):
            raise ValueError('Invalid parameters')
        result = module.radio_operation(value['action'], value.get('serial', ''), value.get('expires_at'), parameters) if parameters else module.radio_operation(value['action'], value.get('serial', ''), value.get('expires_at'))
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False), flush=True)
        return 0
    except BaseException as exc:
        # SDK/OS diagnostics may contain SSID/password. Only our safe messages leave the child.
        message = str(exc) if isinstance(exc, SourceConnectionError) else '蓝牙或 Wi-Fi 操作未完成，请检查相机无线连接和系统权限'
        print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
