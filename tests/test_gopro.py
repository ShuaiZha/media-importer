import io
from http.client import IncompleteRead
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from media_importer.adapters.gopro import GoProSource
from media_importer.model import SourceError
from test_service import configuration, footage


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class GoProTests(unittest.TestCase):
    def setUp(self):
        self.source = GoProSource(configuration())
        from media_importer.adapters.gopro_models import compatibility
        self.source.compatibility = compatibility('HERO9 Black', 'HD9.01.01.72.00')

    def test_hero9_uses_busy_8_and_encoding_10(self):
        with patch.object(self.source, "_json", return_value={"status": {"8": 0, "10": 0}}):
            self.source.ensure_idle()
        for state in ({"8": 1, "10": 0}, {"8": 0, "10": 1}, {"8": 0}):
            with patch.object(self.source, "_json", return_value={"status": state}):
                with self.assertRaises(SourceError):
                    self.source.ensure_idle()

    def test_different_serial_is_rejected(self):
        with patch.object(self.source, "_json", return_value={"serial_number": "another"}):
            with self.assertRaises(SourceError):
                self.source.identify()

    def test_hero9_media_path_slash_is_not_percent_encoded(self):
        with patch.object(self.source, "_json", return_value={}) as call:
            self.source.media_info("100GOPRO/GH011408.MP4")
        self.assertIn("path=100GOPRO/GH011408.MP4", call.call_args[0][0])

    def test_grouped_photos_expand_and_skip_deleted_members(self):
        data = {"media": [{"d": "100GOPRO", "fs": [{"n": "G0010008.JPG", "g": "1", "b": "8", "l": "11", "m": ["9"]}]}]}
        with patch.object(self.source, "_json", return_value=data):
            paths = self.source.media_list()
        self.assertEqual({"100GOPRO/G0010008.JPG", "100GOPRO/G0010010.JPG", "100GOPRO/G0010011.JPG"}, set(paths))

    def test_directory_scan_ignores_parent_external_and_sort_links(self):
        root = b'<a href="/videos/DCIM/..">Parent</a><a href="/videos/DCIM/100GOPRO/">folder</a><a href="?nd">sort</a>'
        child = b'<a href="/videos/DCIM/">Parent</a><a href="GH011408.MP4">video</a><a href="GL011408.LRV">proxy</a><a href="raw.GPR">RAW</a><a href="https://other.invalid/steal">no</a>'
        def open_path(path, headers=None):
            return Response(root if path == "/videos/DCIM/" else child, headers={"Content-Type": "text/html"})
        with patch.object(self.source, "identify", return_value={}), patch.object(self.source, "_open", side_effect=open_path):
            self.assertEqual({"100GOPRO/GH011408.MP4", "100GOPRO/GL011408.LRV", "100GOPRO/raw.GPR"}, self.source.paths())

    def test_missing_or_malformed_gumi_is_not_a_valid_identity(self):
        for value in (None, "", "None", "not-a-media-id", 123):
            self.assertEqual("", self.source.gumi(value))
        self.assertEqual("22bbd9fe4421aea5df1e02f5dda07753", self.source.gumi("22BBD9FE4421AEA5DF1E02F5DDA07753"))

    def test_range_probe_reads_only_one_byte(self):
        response = Response(b"x", 206, {"Content-Range": "bytes 0-0/578726", "Content-Length": "1", "Last-Modified": "date"})
        with patch.object(self.source, "_open", return_value=response) as request:
            self.assertEqual((578726, "date"), self.source.file_stat("100GOPRO/GL011408.LRV"))
        self.assertEqual({"Range": "bytes=0-0"}, request.call_args[0][1])

    def test_truncated_json_is_retryable_camera_error(self):
        response = Response()
        with patch.object(response, "read", side_effect=IncompleteRead(b"partial", 20)), patch.object(self.source, "_open", return_value=response):
            with self.assertRaises(SourceError):
                self.source.identify()

    def test_truncated_metadata_range_is_camera_error(self):
        response = Response(status=206)
        with patch.object(response, "read", side_effect=IncompleteRead(b"partial", 8)), \
             patch.object(self.source, "_open", return_value=response), \
             patch.object(self.source, "attempts", 1, create=True):
            with self.assertRaises(SourceError):
                self.source._read("/videos/DCIM/100GOPRO/GH020001.MP4", 9, {"Range": "bytes=0-7"})

    def test_cache_range_requires_exact_partial_response(self):
        asset = footage()
        for status, headers, accepted in (
            (206, {'Content-Range': 'bytes 4-7/32'}, True),
            (200, {'Content-Length': '32'}, False),
            (206, {'Content-Range': 'bytes 0-3/32'}, False),
        ):
            response = Response(b'data', status, headers)
            with patch.object(self.source, 'identify'), patch.object(self.source, 'ensure_idle'), \
                 patch.object(self.source, 'file_stat', return_value=(asset.size, asset.modified)), \
                 patch.object(self.source, '_open', return_value=response):
                if accepted:
                    self.assertEqual(b'data', self.source.read_range(asset, 4, 4))
                else:
                    with self.assertRaises(SourceError):
                        self.source.read_range(asset, 4, 4)

    @staticmethod
    def mp4_fixture(with_gumi=True):
        def box(kind, data):
            return (len(data) + 8).to_bytes(4, 'big') + kind + data
        stamp = 1700000000
        gumi = '0123456789abcdef0123456789abcdef'
        metadata = box(b'mvhd', b'\0' * 4 + (stamp + 2082844800).to_bytes(4, 'big'))
        metadata += box(b'udta', box(b'GUMI', bytes.fromhex(gumi)) if with_gumi else b'')
        body = box(b'ftyp', b'mp41') + box(b'mdat', b'x' * 1024) + box(b'moov', metadata)
        return body, {'cre': str(stamp), 's': str(len(body)), 'gumi': gumi}

    def test_remaining_chapter_identity_reads_only_mp4_headers(self):
        body, expected = self.mp4_fixture()
        reads = []
        def read(path, limit, headers):
            start, end = map(int, headers['Range'][6:].split('-'))
            reads.append((start, end))
            return body[start:end + 1], 206, {'Content-Range': f'bytes {start}-{end}/{len(body)}'}
        with patch.object(self.source, 'file_stat', return_value=(len(body), 'unchanged')), \
             patch.object(self.source, '_read', side_effect=read):
            self.assertEqual(expected, self.source._mp4_media_info('100GOPRO/GH020001.MP4'))
        self.assertLess(sum(end - start + 1 for start, end in reads), 128)
        self.assertTrue(all(end < 20 or start >= 1044 for start, end in reads))

    def test_mp4_fallback_only_for_not_found_video_metadata(self):
        for status in (404, 500):
            error = SourceError('metadata unavailable')
            error.__cause__ = HTTPError('http://camera', status, 'missing', {}, None)
            for path in ('100GOPRO/GH020001.MP4', '100GOPRO/G0010001.JPG'):
                with patch.object(self.source, '_json', side_effect=error), \
                     patch.object(self.source, '_mp4_media_info', return_value={'verified': True}) as fallback:
                    if status == 404 and path.endswith('.MP4'):
                        self.assertEqual({'verified': True}, self.source.media_info(path))
                    else:
                        with self.assertRaises(SourceError):
                            self.source.media_info(path)
                        fallback.assert_not_called()

    def test_mp4_fallback_rejects_missing_identity_and_changed_source(self):
        for missing, changed in ((True, False), (False, True)):
            body, _ = self.mp4_fixture(with_gumi=not missing)
            def read(path, limit, headers):
                start, end = map(int, headers['Range'][6:].split('-'))
                return body[start:end + 1], 206, {'Content-Range': f'bytes {start}-{end}/{len(body)}'}
            with patch.object(self.source, 'file_stat', side_effect=[(len(body), 'before'), (len(body), 'after' if changed else 'before')]), \
                 patch.object(self.source, '_read', side_effect=read):
                with self.assertRaises(SourceError):
                    self.source._mp4_media_info('100GOPRO/GH020001.MP4')


if __name__ == "__main__":
    unittest.main()
