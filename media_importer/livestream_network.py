"""Read-only discovery of the local RTMP receiver; never edits Internet Sharing."""
import ipaddress
import plistlib
import re
import subprocess
from pathlib import Path

from .host_network import route
from .model import ConfigError


def interfaces(text):
    result = {}
    for block in re.split(r'(?=^[^\s:]+: flags=)', text, flags=re.M):
        match = re.match(r'([^\s:]+): flags=', block)
        if not match: continue
        addresses = []
        for raw in re.findall(r'^\s+inet ([0-9.]+) ', block, re.M):
            address = ipaddress.ip_address(raw)
            if address.is_private and not address.is_loopback and not address.is_link_local:
                addresses.append(raw)
        result[match[1]] = {'addresses': addresses, 'members': re.findall(r'\bmember: (\S+)', block)}
    return result


def discover():
    try:
        path = Path('/Library/Preferences/SystemConfiguration/com.apple.nat.plist')
        nat = plistlib.loads(path.read_bytes()).get('NAT', {}) if path.exists() else {}
        result = subprocess.run(['/sbin/ifconfig'], capture_output=True, text=True, timeout=5, check=True)
        devices = interfaces(result.stdout)
        sharing = bool(nat.get('Enabled'))
        if sharing:
            outputs = set(nat.get('SharingDevices', []))
            candidates = [(name, ip) for name, item in devices.items()
                          if outputs.intersection(item['members']) or name in outputs
                          for ip in item['addresses']]
            if not candidates:
                # macOS can put a virtual AP interface in the shared bridge.
                # Accept a unique active bridge; never guess among multiple LANs.
                candidates = [(name, ip) for name, item in devices.items() if item['members']
                              for ip in item['addresses']]
        else:
            name = route()
            # VPN default routes are not proof of camera reachability.
            candidates = [(name, ip) for ip in devices.get(name, {}).get('addresses', [])
                          if name and not name.startswith(('utun', 'tun', 'ppp'))]
        if len(candidates) != 1:
            raise ConfigError('无法确定直播接收地址，请检查 Mac 有线网络或互联网共享')
        name, address = candidates[0]
        return {'address': address, 'interface': name, 'hotspot': sharing}
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ConfigError('无法读取 Mac 网络，请检查有线连接与热点状态') from None
