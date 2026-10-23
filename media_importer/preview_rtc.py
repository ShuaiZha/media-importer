"""Private WebRTC endpoint and receive-only signaling for one preview stream."""
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import os
from pathlib import Path

from .deployment import environment, number, text
from .model import ConfigError


def endpoint(public_url=""):
    """No STUN, public listener or network configuration changes; HLS is fallback."""
    values = environment()
    port = number(values, 'PREVIEW_RTC_PORT', 18555, integer=True)
    if not 1024 <= port <= 65535:
        raise ConfigError('PREVIEW_RTC_PORT 必须在 1024–65535 之间')
    host = '127.0.0.1'
    if public_url:
        executable = shutil.which('tailscale')
        bundled = Path('/Applications/Tailscale.app/Contents/MacOS/Tailscale')
        if not executable and bundled.is_file(): executable = str(bundled)
        try:
            if not executable: return None
            result = subprocess.run([executable, 'status', '--json'], capture_output=True,
                                    text=True, timeout=5, check=True,
                                    env={**os.environ, 'TERM': os.environ.get('TERM') or 'dumb'})
            info = json.loads(result.stdout)
            if not isinstance(info, dict) or info.get('BackendState') != 'Running': return None
            if not isinstance(info.get('Self'), dict): return None
            host = next(ip for ip in info.get('Self', {}).get('TailscaleIPs', [])
                        if ipaddress.ip_address(ip) in ipaddress.ip_network('100.64.0.0/10'))
        except (OSError, ValueError, TypeError, StopIteration, subprocess.SubprocessError):
            return None
    try:
        # Check both transports before giving the same address to the owned child.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp, \
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind((host, port)); udp.bind((host, port))
    except OSError:
        return None
    return {'listen': f'{host}:{port}', 'ice_servers': [],
            'candidates': [f'{host}:{port}'], 'filters': {'candidates': [host]}}


def signaling(kind, value, *, audio=False):
    if not isinstance(value, str): raise ValueError('Invalid signaling')
    if kind == 'webrtc/offer':
        if len(value) > 24000 or not value.startswith('v=0\r\n'):
            raise ValueError('Invalid offer')
        lines = value.splitlines()
        media = [line for line in lines if line.startswith('m=')]
        expected = ['video', 'audio'] if audio else ['video']
        kinds = []
        for section in media:
            match = re.fullmatch(r'm=(video|audio) [0-9]+ UDP/TLS/RTP/SAVPF(?: [0-9]+)+', section)
            if not match: raise ValueError('Invalid media section')
            kinds.append(match[1])
        # Audio is receive-only and only allowed for an audio-enabled session.
        if (sorted(kinds) != sorted(expected) or lines.count('a=recvonly') != len(expected) or
                any(line in ('a=sendrecv', 'a=sendonly', 'a=inactive') for line in lines)):
            raise ValueError('Only receive-only video is allowed')
    elif kind == 'webrtc/candidate':
        if value == '': return
        parts = value.split(' ')
        if (len(value) > 1024 or '\r' in value or '\n' in value or len(parts) < 8 or
                not re.fullmatch(r'candidate:[A-Za-z0-9+/]+', parts[0]) or parts[1] not in ('1', '2') or
                parts[2].lower() not in ('udp', 'tcp') or not parts[3].isdigit() or
                not parts[5].isdigit() or not 1 <= int(parts[5]) <= 65535 or parts[6] != 'typ' or
                parts[7] not in ('host', 'srflx', 'prflx', 'relay')):
            raise ValueError('Invalid ICE candidate')
        try: ipaddress.ip_address(parts[4])
        except ValueError:
            if not re.fullmatch(r'[A-Za-z0-9-]{1,100}\.local', parts[4]):
                raise ValueError('Invalid ICE address') from None
    else:
        raise ValueError('Unsupported signaling')
