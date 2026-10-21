"""Preview access policy, read-only host checks, and checked atomic saves."""
import json
import os
import shutil
import ssl
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

from .model import ConfigError


def normalize_url(value):
    if not isinstance(value, str) or len(value) > 512 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ConfigError('预览地址必须是 HTTPS 基础地址，不含空格')
    value = value.rstrip('/')
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
                parsed.path or parsed.query or parsed.fragment or '\\' in value or port == 0):
            raise ValueError()
    except ValueError:
        raise ConfigError('预览地址须为 HTTPS 基础地址，不含路径、账号或查询参数') from None
    return value


def tailnet():
    executable = shutil.which('tailscale')
    bundled = Path('/Applications/Tailscale.app/Contents/MacOS/Tailscale')
    if not executable and bundled.is_file():
        executable = str(bundled)
    if not executable:
        raise ConfigError('未找到 Tailscale；请先在运行程序的电脑安装并登录')
    try:
        def query(*args):
            result = subprocess.run([executable, *args, '--json'], capture_output=True, text=True,
                timeout=5, check=True, env={**os.environ, 'TERM': os.environ.get('TERM') or 'dumb'})
            return json.loads(result.stdout)
        status, mapping = query('status'), query('serve', 'status')
        if not isinstance(status, dict) or status.get('BackendState') != 'Running' or not isinstance(mapping, dict):
            raise ValueError()
        device = status['Self']
        hostname = device['DNSName'].rstrip('.').lower()
        import ipaddress
        address = next(ip for ip in device['TailscaleIPs'] if
                       ipaddress.ip_address(ip) in ipaddress.ip_network('100.64.0.0/10'))
        return hostname, address, mapping
    except (KeyError, OSError, ValueError, TypeError, StopIteration, subprocess.SubprocessError):
        raise ConfigError('Tailscale 未就绪；请在本机检查登录和网络连接') from None


def mapped(mapping, hostname, port, local_port):
    handler = mapping.get('Web', {}).get(f'{hostname}:{port}', {}).get('Handlers', {}).get('/', {})
    return (handler.get('Proxy') == f'http://127.0.0.1:{local_port}' and
            mapping.get('TCP', {}).get(str(port), {}).get('HTTPS') is True)


def discover(local_port):
    hostname, _, mapping = tailnet()
    urls = []
    for key in mapping.get('Web', {}):
        try:
            host, port = key.rsplit(':', 1)
            if host == hostname and mapped(mapping, host, int(port), local_port):
                urls.append('https://' + host + (':' + port if port != '443' else ''))
        except (TypeError, ValueError):
            continue
    if not urls:
        raise ConfigError('未发现指向本项目的 Tailscale HTTPS 映射；请先在本机配置 Serve')
    if len(urls) != 1:
        raise ConfigError('发现多个 HTTPS 入口，请手动填写要使用的地址')
    return urls[0]


def inspect(public_url, local_port):
    """Never start a preview or mutate Serve. Only probe this host, never arbitrary URLs."""
    public_url = normalize_url(public_url)
    checks = []
    try:
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{local_port}/health', timeout=3) as response:
            if response.status != 200 or response.read(64) != b'media-importer-preview':
                raise ValueError()
        checks.append({'label': '本机播放服务', 'ok': True, 'detail': '可访问'})
    except (OSError, ValueError):
        checks.append({'label': '本机播放服务', 'ok': False, 'detail': '不可访问，请检查原生后台及本机端口'})
    if not public_url:
        return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'public_url': '', 'mode': 'local'}
    try:
        hostname, address, mapping = tailnet()
        checks.append({'label': 'Tailscale', 'ok': True, 'detail': '本机已连接'})
        url = urlsplit(public_url)
        if url.hostname != hostname:
            raise ConfigError('预览地址必须属于当前运行电脑；可点击「读取本机入口」自动填写')
        if not mapped(mapping, hostname, url.port or 443, local_port):
            raise ConfigError('Serve 映射不匹配；请在本机将此 HTTPS 入口映射到播放服务端口')
        checks.append({'label': 'HTTPS 映射', 'ok': True, 'detail': '目标为本机播放服务'})
        # Connect to the local tailnet address with certificate/SNI validation.
        # This bypasses system fake-IP DNS without editing host/proxy settings.
        with socket.create_connection((address, url.port or 443), timeout=4) as tcp:
            import certifi
            with ssl.create_default_context(cafile=certifi.where()).wrap_socket(tcp, server_hostname=hostname):
                pass
        checks.append({'label': 'HTTPS 连接', 'ok': True, 'detail': '证书与握手通过'})
    except ConfigError as exc:
        checks.append({'label': '远程入口', 'ok': False, 'detail': str(exc)})
    except (OSError, ValueError):
        checks.append({'label': 'HTTPS 连接', 'ok': False, 'detail': '握手未通过，请检查 Serve 与 HTTPS 证书'})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'public_url': public_url, 'mode': 'tailscale'}


def save(settings, fields, expected):
    from .config import Config, preferences
    from .settings import Conflict, atomic_write
    from .preview_access import configuration
    from .store import Store
    from .live_preview import ACTIVE
    if not isinstance(fields, dict) or set(fields) != {'public_url'}:
        raise ConfigError('预览设置格式无效')
    url = normalize_url(fields['public_url'])
    initial = settings.read()
    if initial['error']:
        raise ConfigError(initial['error'])
    if expected is not None and initial['revision'] != expected:
        raise Conflict('设置已经变化，请重新打开预览设置')
    report = inspect(url, configuration(initial['config'])['port'])
    if not report['ok']:
        raise ConfigError('设置未保存：' + '；'.join(c['detail'] for c in report['checks'] if not c['ok']))
    if not (settings.directory / 'state.sqlite3').is_file():
        raise ConfigError('任务目录不可访问，请恢复原磁盘')
    with settings.lock():
        item = settings.read()
        if item['error'] or item['revision'] != initial['revision']:
            raise Conflict('检查期间设置已变化，请重新打开预览设置')
        store = Store(settings.directory)
        try:
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                state = store.get_meta('live_preview', {})
                if state.get('state') in ACTIVE or state.get('cleanup_pending'):
                    raise ConfigError('请先结束实时预览，确认停止后再修改观看入口')
                values = preferences(item['config'])
                values['preview'] = {'public_url': url}
                Config.from_dict(values, settings.path.parent)
                atomic_write(settings.path, json.dumps(values, ensure_ascii=False, indent=2) + '\n')
        finally:
            store.close()
    return settings.read()
