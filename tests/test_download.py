"""Tests for download module."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from stream2video.download import (
    DiskSpaceError,
    DownloadCancelledError,
    DownloadError,
    DownloadProgress,
    DownloadResult,
    DownloadTimeoutError,
    FileBusyError,
    PermissionDeniedError,
    URLValidationError,
    VideoNotAvailableError,
    _classify_error,
    _find_downloaded_file,
    _format_selector_for_quality,
    _is_local_file,
    _parse_progress_line,
    _resolve_reported_download_path,
    _sweep_partial_fragments,
    _timeout_error,
    _validate_url,
    download,
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
        with TemporaryDirectory() as tmpdir, pytest.raises(URLValidationError):
            download("not a valid url", Path(tmpdir))

    def test_cancel_callback_aborts(self):
        """Cancel callback should kill the subprocess and raise DownloadCancelledError."""
        import subprocess
        import time

        _real_popen = subprocess.Popen

        with TemporaryDirectory() as tmpdir:
            cancel_flag = [False]

            def cancel_cb():
                return cancel_flag[0]

            def fake_popen(cmd, **kwargs):
                proc = _real_popen(
                    [
                        sys.executable,
                        "-c",
                        "import time, sys; sys.stdout.write('starting\\n'); "
                        "sys.stdout.flush(); time.sleep(30)",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                cancel_flag[0] = True
                time.sleep(0.3)
                return proc

            with (
                patch("stream2video.download.subprocess.Popen", side_effect=fake_popen),
                pytest.raises(DownloadCancelledError, match="cancelled"),
            ):
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

    def test_reported_path_scans_past_postprocessor_noise(self):
        with TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            video = out_dir / "abc123.mp4"
            video.write_bytes(b"ok")
            resolved, reported = _resolve_reported_download_path(
                out_dir,
                [
                    "s2v_progress|100|100|NA|0|0",
                    str(video),
                    "Deleting original file abc123.webm (pass -k to keep)",
                ],
            )
            assert resolved == video
            assert reported == video

    def test_timeout_error_includes_stderr_diagnostics(self):
        err = _timeout_error("Download timeout after 1s", ["HTTP 403\n", "proxy refused\n"])
        assert isinstance(err, DownloadTimeoutError)
        assert "HTTP 403" in str(err)
        assert "proxy refused" in str(err)


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
        assert not isinstance(
            _classify_error("ERROR: Permission not available"), VideoNotAvailableError
        )

    def test_disk_full(self):
        assert isinstance(_classify_error("OSError: [Errno 28] No space left"), DiskSpaceError)

    def test_permission_denied(self):
        assert isinstance(_classify_error("Permission denied: /video.mp4"), PermissionDeniedError)

    def test_file_busy_windows(self):
        """[WinError 32] <> Permission denied — the remediation differs
        (close the program holding the file, not chmod)."""
        assert isinstance(
            _classify_error(
                "ERROR: unable to rename file: [WinError 32] The process "
                "cannot access the file because it is being used by another process"
            ),
            FileBusyError,
        )

    def test_file_busy_posix_ebusy(self):
        assert isinstance(
            _classify_error("OSError: [Errno 16] Device or resource busy"),
            FileBusyError,
        )

    def test_file_busy_not_permission(self):
        # Regressions: "being used by another process" used to fall into
        # the generic PermissionDenied bucket.
        err = _classify_error("ERROR: file is being used by another process")
        assert not isinstance(err, PermissionDeniedError)

    def test_generic(self):
        e = _classify_error("Some unknown error")
        assert type(e).__name__ == "DownloadError"
        assert "Some unknown error" in str(e)


class TestFormatSelector:
    """Quality preset → yt-dlp format selector mapping."""

    def test_best_is_pre_merged_with_mp4_preference(self):
        # ``best`` prefers separate audio+video (better codec / quality on
        # YouTube) and falls back to a pre-merged file when the site
        # doesn't serve separate tracks. The pre-merged fallback prefers
        # mp4 container for compatibility with the encoder pipeline.
        assert _format_selector_for_quality("best") == "bestvideo+bestaudio/best[ext=mp4]/best"

    def test_resolution_caps_use_height_filter(self):
        # Resolution presets pick the best video stream up to the given
        # height plus best audio, with a pre-merged fallback.
        for height in ("1080p", "720p", "480p", "360p"):
            sel = _format_selector_for_quality(height)
            n = height.rstrip("p")
            assert f"bestvideo[height<={n}]+bestaudio" in sel
            assert f"best[height<={n}]" in sel
            assert sel.endswith("/best"), "resolution selector must fall back to /best"

    def test_unknown_quality_raises(self):
        with pytest.raises(DownloadError, match="Unknown download quality"):
            _format_selector_for_quality("4k")
        with pytest.raises(DownloadError, match="Unknown download quality"):
            _format_selector_for_quality("")

    def test_quality_is_ignored_for_local_files(self):
        """Local file passthrough must not invoke yt-dlp regardless of
        the quality preset — the source is used as-is and quality only
        applies to URL downloads (Twitch/YouTube)."""
        with TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "input.mp4"
            test_file.write_text("video data")
            for q in ("best", "1080p", "720p", "480p", "360p"):
                result = download(str(test_file), Path(tmpdir) / "out", quality=q)
                assert result.path == test_file
                assert not result.is_downloaded


class TestProgressParsing:
    """_parse_progress_line — yt-dlp --progress-template line parser."""

    def test_returns_none_for_non_progress_lines(self):
        # The final filepath line from --print must be left untouched.
        assert _parse_progress_line("/tmp/abc123.mp4") is None
        assert _parse_progress_line("") is None
        # yt-dlp's own [download] progress (without our template) is also
        # passed through as None — we only care about our prefix.
        assert _parse_progress_line("[download]  50.0% of ~1.00GiB at  5.00MiB/s") is None

    def test_parses_full_line(self):
        # 5-field format from the progress.* template:
        # downloaded|total_bytes|total_bytes_estimate|speed|eta
        line = "s2v_progress|524288000|1073741824|NA|5242880|120"
        prog = _parse_progress_line(line)
        assert prog is not None
        assert prog.downloaded_bytes == 524288000.0
        assert prog.total_bytes == 1073741824.0
        assert prog.speed == 5242880.0
        assert prog.eta == 120.0

    def test_returns_namedtuple_fields(self):
        # DownloadProgress is a NamedTuple with the documented field order.
        line = "s2v_progress|100|200|NA|10|5"
        prog = _parse_progress_line(line)
        assert prog is not None
        assert isinstance(prog, DownloadProgress)
        assert prog._fields == ("downloaded_bytes", "total_bytes", "speed", "eta")

    def test_handles_na_fields(self):
        # yt-dlp emits ``NA`` for unknown fields (e.g. total_bytes
        # when the stream's content-length isn't known, or speed before
        # a steady estimate stabilises).
        prog = _parse_progress_line("s2v_progress|1000|NA|NA|NA|NA")
        assert prog is not None
        assert prog.downloaded_bytes == 1000.0
        assert prog.total_bytes is None
        assert prog.speed is None
        assert prog.eta is None

    def test_falls_back_to_total_bytes_estimate(self):
        # When ``total_bytes`` is ``NA`` but yt-dlp provides an estimate,
        # the parser uses the estimate so the UI can still show a
        # percent / ETA (covers chunked transfers and streams without a
        # Content-Length header).
        prog = _parse_progress_line("s2v_progress|500|NA|1000|100|5")
        assert prog is not None
        assert prog.downloaded_bytes == 500.0
        assert prog.total_bytes == 1000.0
        assert prog.speed == 100.0
        assert prog.eta == 5.0

    def test_prefers_exact_total_over_estimate(self):
        # If both ``total_bytes`` and ``total_bytes_estimate`` are known,
        # the exact value wins — yt-dlp's heuristic can be off, and the
        # server-reported Content-Length is authoritative.
        prog = _parse_progress_line("s2v_progress|500|900|1500|100|5")
        assert prog is not None
        assert prog.total_bytes == 900.0

    def test_handles_empty_fields(self):
        # An empty token also maps to None (defensive — yt-dlp uses NA but
        # a malformed line shouldn't crash).
        prog = _parse_progress_line("s2v_progress||NA|NA|NA|")
        assert prog is not None
        assert prog.downloaded_bytes is None
        assert prog.total_bytes is None

    def test_non_numeric_field_becomes_none(self):
        # Garbage values that can't be parsed to float → None so the
        # caller can't crash on a bad template / future yt-dlp change.
        # ``NaN`` is rejected explicitly: float("NaN") succeeds but
        # breaks numeric comparisons downstream.
        prog = _parse_progress_line("s2v_progress|NaN|NA|NA|NA|NA")
        assert prog is not None
        assert prog.downloaded_bytes is None

    def test_line_with_wrong_field_count_returns_none(self):
        # Defensive: only the exact 5-field template parses. A truncated
        # line, a legacy 4-field line, or a 6+-field line from a future
        # template change must not be mis-parsed silently. The 4-field
        # "legacy" layout was removed because no supported yt-dlp emits
        # it (the template we pass always produces 5 fields).
        assert _parse_progress_line("s2v_progress|100|200") is None
        assert _parse_progress_line("s2v_progress|500|1000|100|5") is None  # legacy 4-field
        assert _parse_progress_line("s2v_progress|1|2|3|4|5|6") is None
        assert _parse_progress_line("s2v_progress|") is None
        assert _parse_progress_line("s2v_progress") is None

    def test_progress_callback_is_invoked(self):
        """The progress_callback is called from the stdout drain thread
        for each progress-template line, with parsed DownloadProgress.

        Bits after the progress lines are NOT forwarded to the callback
        (notably the final filepath line from ``--print after_move:filepath``).
        """
        import subprocess

        real_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            return real_popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "print('s2v_progress|500|1000|NA|100|5')\n"
                        "print('s2v_progress|1000|1000|NA|0|0')\n"
                        "print('/tmp/abc123.mp4')"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

        with TemporaryDirectory() as tmp:
            received: list[DownloadProgress] = []

            def cb(p: DownloadProgress) -> None:
                received.append(p)

            with patch("stream2video.download.subprocess.Popen", side_effect=fake_popen):
                try:
                    download(
                        "https://example.com/v",
                        Path(tmp),
                        quality="best",
                        progress_callback=cb,
                    )
                except DownloadError:
                    pass  # the fake script's path doesn't exist on disk

            assert len(received) >= 2
            assert received[0].downloaded_bytes == 500.0
            assert received[0].total_bytes == 1000.0
            assert received[0].speed == 100.0
            assert received[0].eta == 5.0
            assert received[1].downloaded_bytes == 1000.0
            # Sanity: there's no stray '/tmp/abc123.mp4' line delivered as
            # a progress update (it isn't prefixed with s2v_progress|).
            for p in received:
                assert p.downloaded_bytes is not None
                assert p.total_bytes is not None


class TestProgressParsingEdgeCases:
    """Coverage for the download test matrix — zero/unknown fields that
    yt-dlp emits during connection ramp-up, DNS failure, and stalls."""

    def test_zero_speed_at_start(self):
        # yt-dlp emits speed=0 before the first chunk lands. The parser
        # must surface 0.0 (not None) so the UI can distinguish "starting"
        # from "unknown speed".
        prog = _parse_progress_line("s2v_progress|0|1000|1000|0|0")
        assert prog is not None
        assert prog.speed == 0.0
        assert prog.eta == 0.0

    def test_unknown_total_throughout(self):
        # A live stream or chunked transfer with no Content-Length leaves
        # both total_bytes and total_bytes_estimate as NA for the whole
        # download. The UI must tolerate this and show an indeterminate
        # bar (caller checks ``total_bytes is None``).
        for downloaded in ("100", "500", "1000"):
            prog = _parse_progress_line(f"s2v_progress|{downloaded}|NA|NA|100|5")
            assert prog is not None
            assert prog.total_bytes is None
            assert prog.downloaded_bytes == float(downloaded)

    def test_eta_na_with_known_speed(self):
        # When yt-dlp knows the speed but can't estimate ETA (e.g. the
        # total is unknown so ETA is undefined), speed is surfaced but
        # eta is None so the UI shows "calculating..." rather than 0.
        prog = _parse_progress_line("s2v_progress|500|NA|NA|100|NA")
        assert prog is not None
        assert prog.speed == 100.0
        assert prog.eta is None

    def test_downloaded_bytes_zero_is_valid(self):
        # A literal 0 must not be confused with None / falsy — the
        # parser returns 0.0 so the UI can show "0%" rather than "?%".
        prog = _parse_progress_line("s2v_progress|0|1000|1000|0|10")
        assert prog is not None
        assert prog.downloaded_bytes == 0.0


class TestNetworkErrorClassification:
    """Coverage for the download test matrix — yt-dlp stderr from network
    failures (offline / DNS / timeout / retry / stalled) must map to a
    DownloadError subclass or the generic DownloadError, never silently
    pass through.

    These are unit tests on the classifier; full subprocess-level
    coverage requires real yt-dlp sources and is deferred to the
    integration test matrix.
    """

    def test_offline_is_download_error(self):
        # yt-dlp emits "ConnectionError" / "unable to connect" when the
        # network is down. _classify_error currently maps this to the
        # generic DownloadError (not a specific subclass) — the test
        # pins that behaviour so a future refactor doesn't accidentally
        # drop the error entirely.
        for marker in (
            "ERROR: unable to connect to network",
            "ConnectionError: [Errno 101] Network is unreachable",
            "ERROR: Failed to extract any data",
        ):
            err = _classify_error(marker)
            assert isinstance(err, DownloadError), f"{marker!r} not classified as DownloadError"

    def test_dns_failure_is_download_error(self):
        # DNS failures surface as "Name or service not known" / "Temporary
        # failure in name resolution".
        for marker in (
            "ERROR: [Errno -2] Name or service not known",
            "socket.gaierror: [Errno -2] Temporary failure in name resolution",
            "ERROR: unable to resolve host",
        ):
            err = _classify_error(marker)
            assert isinstance(err, DownloadError)

    def test_timeout_is_download_error(self):
        # yt-dlp emits "Read timed out" / "Connection timed out" on
        # network timeouts.
        for marker in (
            "ERROR: Read timed out",
            "socket.timeout: The read operation timed out",
            "ERROR: Connection timed out",
        ):
            err = _classify_error(marker)
            assert isinstance(err, DownloadError)

    def test_retry_exhausted_is_download_error(self):
        # yt-dlp retries up to 10 times before giving up; the final
        # stderr mentions "giving up" / "max retries".
        for marker in (
            "ERROR: giving up after 10 retries",
            "ERROR: Downloaded 0 bytes",
            "ERROR: unable to download video",
        ):
            err = _classify_error(marker)
            assert isinstance(err, DownloadError)

    def test_network_error_does_not_match_unavailable(self):
        # A network error must NOT be misclassified as
        # VideoNotAvailableError — that would mislead the user into
        # thinking the video is gone when it's just their connection.
        err = _classify_error("ERROR: Connection timed out")
        assert not isinstance(err, VideoNotAvailableError)

    def test_network_error_does_not_match_disk_full(self):
        # Likewise, a network error must not be misclassified as
        # DiskSpaceError.
        err = _classify_error("ERROR: unable to connect to network")
        assert not isinstance(err, DiskSpaceError)


class TestDownloadCancelDuringMerge:
    """Cancel во время download и merge.

    yt-dlp's merge phase (combining bestvideo+bestaudio into a single
    MP4) runs AFTER the download completes but BEFORE the final file
    appears at the expected path. A cancel during this window must
    kill the yt-dlp process and raise ``DownloadCancelledError``,
    not silently leave a half-merged .part file on disk.

    The existing ``test_cancel_callback_aborts`` covers the download
    phase; this test covers the merge phase by using a fake subprocess
    that emits progress lines (download done) and then sleeps (simulating
    ffmpeg merge invoked by yt-dlp).
    """

    def test_cancel_during_merge_kills_process(self):
        import subprocess
        import time

        _real_popen = subprocess.Popen
        cancel_flag = [False]

        def cancel_cb():
            return cancel_flag[0]

        def fake_popen(cmd, **kwargs):
            # Spawn a child that emits progress lines (download phase
            # done) and then hangs — simulating the ffmpeg merge step
            # yt-dlp invokes for bestvideo+bestaudio downloads.
            proc = _real_popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys, time\n"
                        "sys.stdout.write('s2v_progress|1000|1000|NA|100|0\\n')\n"
                        "sys.stdout.flush()\n"
                        "time.sleep(30)\n"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            # Wait for the progress line to be written so the download
            # phase registers as complete, then fire cancel — this is
            # the merge window (process still alive, no final file).
            time.sleep(0.2)
            cancel_flag[0] = True
            return proc

        with (
            TemporaryDirectory() as tmp,
            patch("stream2video.download.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(DownloadCancelledError, match="cancelled"),
        ):
            download(
                "https://example.com/v",
                Path(tmp),
                cancel_callback=cancel_cb,
            )

    def test_cancel_kills_subprocess_not_orphaned(self):
        """When cancel fires, the yt-dlp subprocess must be killed (not
        orphaned). A leaked process would keep writing to stdout/err
        after download() returned, corrupting subsequent runs."""
        import subprocess
        import time

        _real_popen = subprocess.Popen
        killed: list[bool] = []

        def fake_popen(cmd, **kwargs):
            proc = _real_popen(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # Wrap .kill so we can observe whether it's invoked.
            original_kill = proc.kill

            def tracked_kill():
                killed.append(True)
                return original_kill()

            proc.kill = tracked_kill  # type: ignore[method-assign]
            time.sleep(0.2)
            return proc

        with (
            TemporaryDirectory() as tmp,
            patch("stream2video.download.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(DownloadCancelledError),
        ):
            download(
                "https://example.com/v",
                Path(tmp),
                cancel_callback=lambda: True,
            )
        assert killed, "subprocess.kill() was not called — process would be orphaned"


class TestSweepPartialFragments:
    """P2 audit regression: cancelled downloads must not leak .part/.ytdl/.temp.

    The download loop raises ``DownloadCancelledError(partial=True)`` before
    stdout→path resolution, so the controller's cleanup never learns the
    partial's path. ``_sweep_partial_fragments`` runs in the finally block of
    ``download()`` — the one place that knows both the directory and the
    activity window of the attempt.
    """

    def test_deletes_fresh_part_fragments(self):
        import time

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            fragment = out / "vid-12345.webm.part"
            fragment.write_bytes(b"partial data")
            # mtime now → definitely newer than the sweep's start window.
            now = time.time()
            os_utime = fragment.stat().st_mtime
            assert now - os_utime < 1.0  # sanity: file is fresh

            _sweep_partial_fragments(out, time.monotonic() - 10.0)
            assert not fragment.exists()

    def test_keeps_old_fragments(self):
        import time

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            old = out / "old-12345.webm.part"
            old.write_bytes(b"old")
            # Backdate the mtime so it predates the sweep's start window.
            old_time = time.time() - 3600.0
            import os

            os.utime(old, (old_time, old_time))

            _sweep_partial_fragments(out, time.monotonic() - 10.0)
            assert old.exists()

    def test_keeps_non_fragment_files(self):
        import time

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            keep = out / "finished.mp4"
            keep.write_bytes(b"complete")
            _sweep_partial_fragments(out, time.monotonic() - 10.0)
            assert keep.exists()

    def test_missing_dir_is_noop(self):
        _sweep_partial_fragments(Path("does-not-exist-xyz"), 0.0)
