"""Host Wi-Fi operations; independent of camera vendor protocols."""
import re
import subprocess
import os

from .model import SourceConnectionError


def preview_mapping(public_url, port):
    """Read only the dedicated Serve mapping; never reset unrelated mappings."""
    import json
    import shutil
    from pathlib import Path
    from urllib.parse import urlsplit
    if not public_url:
        return {'remote_state': 'local', 'remote_reason': '仅本机观看'}
    executable = shutil.which('tailscale')
    bundled = Path('/Applications/Tailscale.app/Contents/MacOS/Tailscale')
    if not executable and bundled.is_file(): executable = str(bundled)
    try:
        if not executable: raise ValueError()
        result = subprocess.run([executable, 'serve', 'status', '--json'], capture_output=True,
                                text=True, timeout=5, check=True,
                                # macOS ships GUI and CLI in the same executable;
                                # launchd has no terminal variables to select CLI mode.
                                env={**os.environ, 'TERM': os.environ.get('TERM') or 'dumb'})
        config = json.loads(result.stdout)
        address = urlsplit(public_url)
        key = f'{address.hostname}:{address.port or 443}'
        handler = config.get('Web', {}).get(key, {}).get('Handlers', {}).get('/', {})
        if handler.get('Proxy') == f'http://127.0.0.1:{port}' and config.get('TCP', {}).get(str(address.port or 443), {}).get('HTTPS'):
            return {'remote_state': 'configured', 'remote_reason': 'Tailscale HTTPS 映射已核验；观看设备需连接 Tailscale'}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {'remote_state': 'unverified', 'remote_reason': 'Tailscale 播放入口待核验，请检查 Serve 映射与登录状态'}


def route():
    output = subprocess.run(['/sbin/route', '-n', 'get', 'default'], capture_output=True,
                            text=True, timeout=5).stdout
    found = re.search(r'^\s+interface: (\S+)', output, re.M)
    return found[1] if found else None


def wifi_interface():
    output = subprocess.run(['/usr/sbin/networksetup', '-listallhardwareports'], capture_output=True,
                            text=True, timeout=5).stdout
    for block in output.split('\n\n'):
        if re.search(r'Hardware Port: (Wi-Fi|AirPort)', block):
            match = re.search(r'Device: (\S+)', block)
            if match:
                return match[1]
    raise SourceConnectionError('未找到 Mac Wi-Fi 网卡')


def join_wifi(interface, ssid, password):
    # macOS may exit zero while association failed. Only HTTP identity proves success.
    result = subprocess.run(['/usr/sbin/networksetup', '-setairportnetwork', interface, ssid, password],
                            capture_output=True, text=True, timeout=25, check=False)
    output = (result.stdout + result.stderr).lower()
    # Do not expose raw OS diagnostics: they can contain the SSID or password.
    if 'could not find network' in output:
        return 'not_found'
    if 'authorization' in output or 'permission' in output:
        raise SourceConnectionError('macOS 未允许本进程切换 Wi-Fi；可先手动连接相机热点')
    if 'password' in output or 'authentication' in output:
        raise SourceConnectionError('macOS 未接受相机热点凭据，请检查配对状态或手动连接')
    return 'attempted'
