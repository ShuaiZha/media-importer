"""Bounded HLS proxy for native mobile playback; never exposes arbitrary sources."""
import re
import time
from urllib.request import build_opener, ProxyHandler


class HLS:
    def __init__(self):
        self.sessions = {}

    @staticmethod
    def fetch(url):
        # The caller supplies the validated loopback media base, never a client URL.
        with build_opener(ProxyHandler({})).open(url, timeout=4) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError('Media response too large')
            return data

    def load(self, base, token, resource, stream='preview'):
        if not re.fullmatch(r'preview|[0-9a-f]{32}', stream): raise ValueError('Invalid stream')
        stamp = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items()
                         if stamp - value[1] < 30 and value[0] == token}
        if resource == 'hls/master.m3u8':
            if len(self.sessions) >= 32:
                raise ValueError('Too many playback sessions')
            # MPEG-TS carries the camera's in-band SPS/PPS. The upstream fMP4
            # initializer may contain dummy parameters when its initial probe
            # sees a frame before SPS/PPS; Apple then advances a black video.
            data = self.fetch(base + '/api/stream.m3u8?src=' + stream).decode()
            match = re.search(r'^hls/playlist\.m3u8\?id=([A-Za-z0-9]{8})$', data, re.M)
            if not match:
                raise ValueError('Invalid media playlist')
            sid = match[1]
            self.sessions[sid] = (token, stamp)
            return data.replace(match[0], sid + '/playlist.m3u8').encode(), 'application/vnd.apple.mpegurl'
        match = re.fullmatch(r'hls/([A-Za-z0-9]{8})/(playlist\.m3u8|segment\.ts)(?:\?n=([0-9]{1,12}))?', resource)
        if not match or match[1] not in self.sessions:
            raise ValueError('Unknown playback resource')
        sid, name, sequence = match.groups()
        if bool(sequence is not None) != (name == 'segment.ts'):
            raise ValueError('Invalid playback sequence')
        self.sessions[sid] = (token, stamp)
        suffix = name + '?id=' + sid + ('&n=' + sequence if sequence is not None else '')
        data = self.fetch(base + '/api/hls/' + suffix)
        if name == 'playlist.m3u8':
            playlist = data.decode()
            playlist = playlist.replace('segment.ts?id=' + sid + '&n=', 'segment.ts?n=')
            for line in playlist.splitlines():
                if line and not line.startswith('#') and not re.fullmatch(r'segment\.ts\?n=[0-9]{1,12}', line):
                    raise ValueError('Invalid playback segment')
                if 'URI=' in line:
                    raise ValueError('Invalid playback initialization')
            return playlist.encode(), 'application/vnd.apple.mpegurl'
        return data, 'video/mp2t'
