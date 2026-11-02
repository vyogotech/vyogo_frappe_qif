# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from vyogo_frappe_qif.importer.stream_reader import (
    get_file_line_iterator,
    stream_lines_from_path,
    stream_lines_from_stream,
)


class TestStreamReader(unittest.TestCase):
    def test_stream_lines_from_path_crlf_lf_cr(self):
        """Test streaming lines with various newline conventions (CRLF, LF, CR)."""
        content = "Line 1\r\nLine 2\nLine 3\rLine 4\r\n"
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(content.encode("utf-8"))
            path = f.name

        try:
            lines = list(stream_lines_from_path(path))
            self.assertEqual(lines, ["Line 1", "Line 2", "Line 3", "Line 4"])
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_stream_lines_with_utf8_bom(self):
        """Test streaming from a file with a UTF-8 BOM."""
        raw_bytes = b"\xef\xbb\xbf!Type:Bank\r\nD01/01/2026\r\nT-10.00\r\n^\r\n"
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(raw_bytes)
            path = f.name

        try:
            lines = list(stream_lines_from_path(path))
            self.assertEqual(lines[0], "!Type:Bank")
            self.assertFalse(lines[0].startswith("\ufeff"))
            self.assertEqual(len(lines), 4)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_stream_lines_from_binary_stream(self):
        """Test streaming directly from a binary stream with small chunk size."""
        raw_bytes = b"!Type:Bank\nLine with unicode \xc3\xa9\n^\n"
        stream = io.BytesIO(raw_bytes)
        lines = list(stream_lines_from_stream(stream, chunk_size=4))
        self.assertEqual(lines, ["!Type:Bank", "Line with unicode \u00e9", "^"])

    def test_remote_file_without_hook_raises(self):
        """Test that a remote file without a configured hook raises an error."""
        file_doc = MagicMock()
        file_doc.name = "REMOTE-FILE-001"
        file_doc.file_url = "https://s3.amazonaws.com/my-bucket/statement.qif"
        file_doc.is_remote_file = 1

        with patch("vyogo_frappe_qif.importer.stream_reader.get_hook_handlers", return_value=[]):
            with self.assertRaises(Exception) as ctx:
                list(get_file_line_iterator(file_doc))
            self.assertIn("Remote file storage detected", str(ctx.exception))
            self.assertIn("qif_file_line_reader", str(ctx.exception))

    def test_remote_file_with_custom_hook(self):
        """Test that a remote file delegates to the qif_file_line_reader hook."""
        file_doc = MagicMock()
        file_doc.name = "REMOTE-FILE-002"
        file_doc.file_url = "https://storage.provider.com/statement.qif"
        file_doc.is_remote_file = 1

        mock_hook = MagicMock(return_value=iter(["!Type:Bank", "D02/02/2026", "^"]))

        with patch("vyogo_frappe_qif.importer.stream_reader.get_hook_handlers", return_value=[mock_hook]):
            lines = list(get_file_line_iterator(file_doc))
            self.assertEqual(lines, ["!Type:Bank", "D02/02/2026", "^"])
            mock_hook.assert_called_once_with(file_doc)
