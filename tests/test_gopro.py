import io
from http.client import IncompleteRead
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
