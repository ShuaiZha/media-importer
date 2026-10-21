"""A source endpoint can change without rebuilding the archive worker.

Only IO resolves the current connection. Pure media rules stay in the adapter;
each IO call pins its client for the duration of that operation.
"""
from .adapters import create_source
from .config import Config
from .model import SourceConnectionError
from .source_connection import read, fresh


class SourceSession:
    IO = {'identify', 'info', 'telemetry', 'snapshot', 'paths', 'download', 'delete',
          'keep_alive', 'read_range', 'media_info', 'verify_primary'}

    def __init__(self, config, directory, stop=None, purpose='transfer'):
        self.config, self.directory, self.stop, self.purpose = config, directory, stop, purpose
        self.client = create_source(config, stop)
        self.session_id = None
        self.on_activity = None

    def fork(self, stop=None, *, purpose='transfer'):
        return type(self)(self.config, self.directory, self.stop if stop is None else stop, purpose)

    @property
    def transport(self):
        state = read(self.directory)
        return (state.get('device') or {}).get('transport', self.config.source['transport'])

    def resolve(self):
        state = read(self.directory)
        device = state.get('device', {})
        if (state.get('state') != 'approved' or not fresh(state) or not device.get('http_ready', True) or
                device.get('type') != self.config.source['type'] or
                device.get('serial_number') != self.config.source['serial']):
            raise SourceConnectionError('当前相机连接不可用，等待重新连接')
        if state.get('id') != self.session_id:
            values = {**self.config.values, 'source': {**self.config.source,
                      'url': device['url'], 'transport': device['transport']}}
            client = create_source(Config.from_values(values), self.stop)
            self.client = client.fork(self.stop, purpose=self.purpose) if hasattr(client, 'fork') else client
            self.session_id = state['id']
        self.client.on_activity = self.on_activity
        return self.client

    def __getattr__(self, name):
        if name in self.IO:
            def operation(*args, **kwargs):
                return getattr(self.resolve(), name)(*args, **kwargs)
            return operation
        return getattr(self.client, name)
