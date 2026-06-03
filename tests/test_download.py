"""Tests for download module."""

import sys
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stream2video.download import (
    download,
    _validate_url,
    _is_local_file,
    _find_downloaded_file,
    _classify_error,
    URLValidationError,
    VideoNotAvailableError,
    DiskSpaceError,
    PermissionDeniedError,
    DownloadCancelledError,
    DownloadError,
    DownloadResult,
)


class TestURLValidation:
    """Test URL validation."""

    def test_valid_http_url(self):
        assert _validate_url("http://example.com/video")

    def test_valid_https_url(self):
        assert _validate_url("https://www.youtube.com/watch?v=test")

    def test_bare_domain_is_rejected(self):
        """Bare domains (no scheme) must be rejected: a typo'd local filename
        like ``myvideo.mp4`` would otherwise be misclassified as a URL."""
        assert not _validate_url("youtube.com/watch?v=test")

    def test_filename_with_dot_is_rejected(self):
        assert not _validate_url("myvideo.mp4")

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

            assert isinstance(result, DownloadResult)
            assert result.path == test_file
            assert result.path.exists()
            assert result.is_downloaded is False

    def test_invalid_url_raises_error(self):
        """Test that invalid URLs raise URLValidationError."""
        with TemporaryDirectory() as tmpdir:
            with pytest.raises(URLValidationError):
                download("not a valid url", Path(tmpdir))

    def test_cancel_callback_aborts(self):
        """Cancel callback should kill the subprocess and raise DownloadCancelledError."""
        import time
        import subprocess

        _real_popen = subprocess.Popen

        with TemporaryDirectory() as tmpdir:
            cancel_flag = [False]

            def cancel_cb():
                return cancel_flag[0]

            def fake_popen(cmd, **kwargs):
                proc = _real_popen(
                    [sys.executable, "-c",
                     "import time, sys; sys.stdout.write('starting\\n'); "
                     "sys.stdout.flush(); time.sleep(30)"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                cancel_flag[0] = True
                time.sleep(0.3)
                return proc

            with patch("stream2video.download.subprocess.Popen", side_effect=fake_popen):
                with pytest.raises(DownloadCancelledError, match="cancelled"):
                    download("https://example.com/v", Path(tmpdir), cancel_callback=cancel_cb)

    def test_cancelled_is_subclass_of_download_error(self):
        """DownloadCancelledError must remain a DownloadError for backwards-compat catches."""
        assert issubclass(DownloadCancelledError, DownloadError)


class TestFindDownloadedFile:
    """Locate the downloaded file via expected path or glob fallback."""

    def test_expected_path_exists(self):
        with TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "abc123.mp4"
            f.write_text("x")
            assert _find_downloaded_file(Path(tmpdir), f) == f

    def test_glob_fallback_posix_path(self):
        with TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "abc123.mp4"
            f.write_text("x")
            expected = Path("/some/other/place/abc123.mp4")
            assert _find_downloaded_file(Path(tmpdir), expected) == f

    def test_glob_fallback_windows_path(self):
        with TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "abc123.mp4"
            f.write_text("x")
            expected = Path("C:\\Users\\me\\Videos\\abc123.mp4")
            assert _find_downloaded_file(Path(tmpdir), expected) == f

    def test_no_match_returns_none(self):
        with TemporaryDirectory() as tmpdir:
            expected = Path("C:\\Videos\\different.mp4")
            assert _find_downloaded_file(Path(tmpdir), expected) is None


class TestClassifyError:
    """yt-dlp stderr → exception subclass mapping."""

    def test_unavailable_variants(self):
        for marker in (
            "ERROR: Video unavailable",
            "ERROR: This video is unavailable",
            "ERROR: This video is no longer available",
            "ERROR: Video is private",
            "ERROR: This video is private",
            "ERROR: This video has been removed by the uploader",
        ):
            assert isinstance(_classify_error(marker), VideoNotAvailableError)

    def test_unavailable_does_not_match_unrelated(self):
        assert not isinstance(_classify_error("ERROR: Permission not available"),
                              VideoNotAvailableError)

    def test_disk_full(self):
        assert isinstance(_classify_error("OSError: [Errno 28] No space left"),
                          DiskSpaceError)

    def test_permission_denied(self):
        assert isinstance(_classify_error("Permission denied: /video.mp4"),
                          PermissionDeniedError)

    def test_generic(self):
        e = _classify_error("Some unknown error")
        assert type(e).__name__ == "DownloadError"
        assert "Some unknown error" in str(e)

