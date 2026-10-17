"""Bounded USB discovery. Only the documented GoPro endpoints are probed."""
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit

from .gopro import GoProSource
from ..model import BackupError


def usb_url(serial):
    tail = str(serial)[-3:]
    return f'http://172.2{tail[0]}.1{tail[1:]}.51:8080' if len(tail) == 3 and tail.isdigit() else None


def probe(url, stop=None, *, timeout=2):
    try:
        config = SimpleNamespace(source={'url': url, 'serial': '', 'timeout_seconds': timeout, 'request_attempts': 1})
        result = GoProSource(config, stop).info()
        return {**result, 'url': url, 'type': 'gopro'}
    except (BackupError, OSError, ValueError, KeyError, TypeError):
        return None


def discover(hints, stop, *, scan=False):
    """Prefer configured/last addresses. Slow discovery is only needed offline.

    No LAN-wide scan, media reads, host helper, or new dependency. A discovered
    address must agree with the serial-derived USB address before it is offered.
    """
    seen = set()
    for url in hints:
        if not url or url in seen or stop.is_set():
            continue
        seen.add(url)
        found = probe(url, stop)
        if found:
            return found
    if not scan or stop.is_set():
        return None

    done = threading.Event()
    def candidate(number):
        if stop.is_set() or done.is_set():
            return None
        url = usb_url(f'{number:03}')
        if url in seen:
            return None
        host = urlsplit(url).hostname
        try:
            with socket.create_connection((host, 8080), timeout=.15):
                pass
        except OSError:
            return None
        result = probe(url, stop, timeout=.5)
        return result if result and usb_url(result['serial_number']) == url else None

    # The documented mapping contains exactly 1,000 addresses, not whole subnets.
    # map preserves order so two visible cameras cannot cause random switching.
    with ThreadPoolExecutor(max_workers=16, thread_name_prefix='usb-discovery') as pool:
        try:
            for found in pool.map(candidate, range(1000)):
                if found:
                    return found
        finally:
            done.set()  # Pending probes become no-ops after a match or cancellation.
    return None
