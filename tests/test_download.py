"""Tests for download module."""

import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from stream2video.download import (
    download,
    _validate_url,
    _is_local_file,
    URLValidationError,
)


class TestURLValidation:
    """Test URL validation."""

    def test_valid_http_url(self):
        assert _validate_url("http://example.com/video")

    def test_valid_https_url(self):
        assert _validate_url("https://www.youtube.com/watch?v=test")

    def test_valid_domain_url(self):
        assert _validate_url("youtube.com/watch?v=test")

    def test_invalid_url(self):
        assert not _validate_url("not a url")
        assert not _validate_url("just text")

    def test_empty_url(self):
        assert not _validate_url("")


class TestLocalFileCheck:
    """Test local file detection."""

    def test_existing_file(self):
        with TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.mp4"
            test_file.write_text("test")
            assert _is_local_file(str(test_file))

    def test_nonexistent_file(self):
        assert not _is_local_file("/nonexistent/path/file.mp4")

    def test_url_not_local_file(self):
        assert not _is_local_file("https://example.com/video.mp4")


class TestDownloadFunction:
    """Test download function."""

    def test_local_file_passthrough(self):
        """Test that local files are returned as-is."""
        with TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "input.mp4"
            test_file.write_text("video data")

            result = download(str(test_file), Path(tmpdir) / "output")

            assert result == test_file
            assert result.exists()

    def test_invalid_url_raises_error(self):
        """Test that invalid URLs raise URLValidationError."""
        with TemporaryDirectory() as tmpdir:
            with pytest.raises(URLValidationError):
                download("not a valid url", Path(tmpdir))

