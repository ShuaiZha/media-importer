import errno,json,os,stat,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from media_importer.adapters.smb_transport import SmbSession
from media_importer.model import DestinationError, Cancelled, NameConflict, LocalCacheChanged
from test_service import configuration

class MemorySmb:
    def __init__(self):
        self.files = {}
        self.serial = 0
        self.writes = []
        self.opens = []
        self.truncates = []
        self.max_write = 5
        self.fail_flush = False
        self.fail_after = None
        self.blocked = None

    def makedirs(self, path, **options):
        if self.blocked:
            options['connection_cache']['test'] = SimpleNamespace(disconnect=lambda **_: self.blocked.set())

    def new(self, path, data=b''):
        self.serial += 1
        self.files[path] = {'data': bytearray(data), 'id': self.serial, 'flushed': b''}

    def open_file(self, path, mode, **options):
        self.opens.append((path, mode, options.get('share_access')))
        if mode == 'xb':
            if path in self.files:
                raise OSError(errno.EEXIST, 'exists')
            self.new(path)
        elif mode == 'rb+' and path not in self.files:
            raise OSError(errno.ENOENT, 'missing')
        assert mode in ('xb', 'rb+'), 'Must never truncate on open'
        return MemoryHandle(self, path)

    def stat(self, path, **options):
        if path not in self.files:
            raise OSError(errno.ENOENT, 'missing')
        item = self.files[path]
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_dev=1, st_ino=item['id'],
                               st_ctime_ns=item['id'] * 1000, st_reparse_tag=0, st_size=len(item['data']))

    def rename(self, source, target, **options):
        if any(name.casefold() == target.casefold() for name in self.files):
            raise OSError(errno.EEXIST, 'exists')
        self.files[target] = self.files.pop(source)


class MemoryHandle:
    def __init__(self, client, path):
        self.client, self.path, self.position = client, path, 0

    def seek(self, offset):
        self.position = offset

    def read(self, *_):
        raise AssertionError('Remote media must never be read back')

    def write(self, data):
        if self.client.blocked:
            self.client.blocked.wait(3)
            raise OSError('connection interrupted')
        if self.client.fail_after is not None and self.position >= self.client.fail_after:
            raise OSError('simulated disconnect')
        amount = min(len(data), self.client.max_write)
        value = self.client.files[self.path]['data']
        value[self.position:self.position + amount] = data[:amount]
        self.client.writes.append((self.path, self.position, amount))
        self.position += amount
        return amount

    def truncate(self, size):
        self.client.truncates.append((self.path, size))
        del self.client.files[self.path]['data'][size:]

    def flush(self):
        if self.client.fail_flush:
            raise OSError('flush acknowledgement lost')
        self.client.files[self.path]['flushed'] = bytes(self.client.files[self.path]['data'])

    def close(self):
        pass



class WholeFileTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.local=Path(self.tmp.name)/'video.bin'
        self.local.write_bytes(bytes(range(64)));self.client=MemorySmb();self.stop=threading.Event()
        self.created=[];self.staged=[];self.progress=[]

    def tearDown(self):self.tmp.cleanup()

    def session(self):
        result=SmbSession(configuration(),'test-only-password',self.stop,self.client)
        result.block_size=8
        return result

    def run_upload(self,name='device/.partial',progress=None):
        self.session().upload(self.local,name,progress or (lambda n,t:self.progress.append(n)),self.created.append,self.staged.append)

    def test_whole_file_short_writes_no_remote_reads(self):
        self.run_upload();self.assertEqual(64,self.progress[-1])
        self.assertEqual(self.created,self.staged);self.assertEqual(1,len(self.created))
        self.assertEqual(self.local.read_bytes(),next(iter(self.client.files.values()))['flushed'])
        self.assertEqual('xb',self.client.opens[0][1])

    def test_interruption_new_attempt_starts_at_zero(self):
        def progress(n,t):
            self.assertTrue(self.created)
            if n>=16:self.stop.set()
        with self.assertRaises(Cancelled):self.run_upload(progress=progress)
        self.assertFalse(self.staged);first=len(self.client.writes);self.stop.clear()
        self.run_upload('device/.new-partial')
        self.assertEqual(0,self.client.writes[first][1]);self.assertTrue(all(o[1]=='xb' for o in self.client.opens))

    def test_failed_flush_never_commits_complete(self):
        self.client.fail_flush=True
        with self.assertRaises(DestinationError):self.run_upload()
        self.assertFalse(self.staged)

    def test_existing_file_never_truncated(self):
        self.run_upload();before=next(iter(self.client.files.values()))['data'][:]
        with self.assertRaises(DestinationError):self.run_upload()
        self.assertEqual(before,next(iter(self.client.files.values()))['data'])

    def test_source_changes_are_rejected_before_staging(self):
        original_time = self.local.stat().st_mtime_ns
        def progress(n,t):
            if n>=16:
                self.local.write_bytes(b'x'*64)
                # tmpfs can coalesce very fast writes into the same clock tick.
                os.utime(self.local, ns=(original_time + 1_000_000_000, original_time + 1_000_000_000))
        with self.assertRaises(LocalCacheChanged):self.run_upload(progress=progress)
        self.assertFalse(self.staged)

    def test_cancellation_releases_blocked_write(self):
        session=self.session();session.pause_timeout=.01;self.client.blocked=threading.Event()
        timer=threading.Timer(.05,self.stop.set);timer.start();start=time.monotonic()
        try:
            with self.assertRaises(Cancelled):session.upload(self.local,'device/.partial',lambda *_:None,self.created.append,self.staged.append)
        finally:timer.cancel()
        self.assertLess(time.monotonic()-start,1.5);self.assertFalse(self.staged)

    def test_atomic_publish_rejects_existing_final(self):
        self.run_upload();session=self.session();src=session.path('device/.partial');dst=session.path('device/final.mp4')
        self.client.new(dst,b'foreign')
        with self.assertRaises(NameConflict):session.publish('device/.partial','device/final.mp4')
        self.assertEqual(b'foreign',bytes(self.client.files[dst]['data']));self.assertIn(src,self.client.files)
