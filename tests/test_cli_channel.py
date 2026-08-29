"""CLI tests for the Twitch channel import (--channel-limit).

The listing resolver is covered in test_channel.py; these tests drive the
CLI surface: the channel-URL gate (rejection without --channel-limit),
the batch loop (per-VOD controller runs, continue-on-error, batch
epilogue), and cancellation escaping the batch. The controller and the
listing resolver are patched — no network, no real media.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from stream2video.channel import ChannelImportCancelled, ChannelImportError, ChannelVod
from stream2video.cli import app
from stream2video.pipeline_controller import PipelineConcatError

CHANNEL_URL = "https://www.twitch.tv/somechannel/videos"


def _vod(i: int) -> ChannelVod:
    return ChannelVod(
        video_id=f"v{i}",
        url=f"https://www.twitch.tv/videos/{i}",
        title=f"VOD {i}",
        duration=60.0,
    )


class TestChannelUrlGate:
    def test_channel_url_without_limit_exits_2(self, tmp_path: Path):
        result = CliRunner().invoke(
            app,
            [CHANNEL_URL, "-o", str(tmp_path / "out")],
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "Channel URL detected" in result.output
        assert "--channel-limit" in result.output

    def test_channel_url_with_zero_limit_exits_2(self, tmp_path: Path):
        result = CliRunner().invoke(
            app,
            [CHANNEL_URL, "-o", str(tmp_path / "out"), "--channel-limit", "0"],
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "--channel-limit" in result.output

    def test_single_vod_url_untouched(self, tmp_path: Path):
        """A plain VOD URL must NOT go through the channel gate — it is a
        normal single-video run (the controller is reached, the resolver
        is not)."""
        with patch("stream2video.cli.resolve_channel_vods") as fake_resolve:
            result = CliRunner().invoke(
                app,
                ["https://www.twitch.tv/videos/123", "-o", str(tmp_path / "out")],
                catch_exceptions=False,
            )
        # The run fails somewhere downstream (no ffmpeg input to speak
        # of in this environment is fine) — but the point is the gate:
        # the listing resolver was never called and the output does not
        # mention the channel gate.
        assert fake_resolve.call_count == 0
        assert "Channel URL detected" not in result.output


class TestChannelBatchFlow:
    def test_batch_runs_controller_per_vod(self, tmp_path: Path):
        inputs_seen: list[str] = []

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        class _Ctrl:
            def __init__(self, cfg=None, **kw):
                inputs_seen.append(cfg.input_raw)

            def run(self):
                return _R()

        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1), _vod(2)]),
            patch("stream2video.cli.PipelineController", side_effect=_Ctrl),
        ):
            result = CliRunner().invoke(
                app,
                [CHANNEL_URL, "-o", str(tmp_path / "out"), "--channel-limit", "2"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Channel batch: 2/2" in result.output
        # One controller per VOD, each bound to the VOD URL (not the
        # channel listing URL), in listing order.
        assert inputs_seen == [
            "https://www.twitch.tv/videos/1",
            "https://www.twitch.tv/videos/2",
        ]

    def test_batch_continues_after_vod_failure(self, tmp_path: Path):
        """One failing VOD must not kill the queue: the batch logs the
        failure, runs the remaining VODs, and exits non-zero with the
        failed URL listed in the epilogue."""
        calls = []

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        class _Ctrl:
            def __init__(self, cfg=None, **kw):
                self.url = cfg.input_raw

            def run(self):
                calls.append(self.url)
                if self.url.endswith("/1"):
                    raise PipelineConcatError("boom on VOD 1")
                return _R()

        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1), _vod(2)]),
            patch("stream2video.cli.PipelineController", side_effect=_Ctrl),
        ):
            result = CliRunner().invoke(
                app,
                [CHANNEL_URL, "-o", str(tmp_path / "out"), "--channel-limit", "2"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        assert "Channel batch: 1/2" in result.output
        assert "https://www.twitch.tv/videos/1" in result.output
        assert "Concatenation failed" in result.output
        # The second VOD still ran despite the first one failing.
        assert calls == ["https://www.twitch.tv/videos/1", "https://www.twitch.tv/videos/2"]


class TestChannelListingErrors:
    def test_listing_error_exits_1(self, tmp_path: Path):
        with patch(
            "stream2video.cli.resolve_channel_vods",
            side_effect=ChannelImportError("No VODs found"),
        ):
            result = CliRunner().invoke(
                app,
                [CHANNEL_URL, "-o", str(tmp_path / "out"), "--channel-limit", "3"],
                catch_exceptions=False,
            )
        assert result.exit_code == 1
        assert "Channel import failed" in result.output
        assert "No VODs found" in result.output

    def test_listing_cancel_exits_130(self, tmp_path: Path):
        with patch(
            "stream2video.cli.resolve_channel_vods",
            side_effect=ChannelImportCancelled(),
        ):
            result = CliRunner().invoke(
                app,
                [CHANNEL_URL, "-o", str(tmp_path / "out"), "--channel-limit", "3"],
                catch_exceptions=False,
            )
        assert result.exit_code == 130
        assert "cancelled" in result.output.lower()


class TestChannelLimitValidation:
    def test_resolver_receives_limit_and_proxy(self, tmp_path: Path):
        """The CLI must forward --channel-limit as the resolver's limit
        (the playlist window) — a fat-fingered N silently importing the
        whole channel is the failure mode this pins down."""
        with (
            patch(
                "stream2video.cli.resolve_channel_vods",
                return_value=[_vod(1)],
            ) as fake_resolve,
            patch("stream2video.cli.PipelineController"),
        ):
            CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "5",
                    "--proxy",
                    "http://127.0.0.1:8080",
                ],
                catch_exceptions=False,
            )
        assert fake_resolve.call_count == 1
        assert (
            fake_resolve.call_args.kwargs.get(
                "limit",
                fake_resolve.call_args.args[1] if len(fake_resolve.call_args.args) > 1 else None,
            )
            == 5
        )
        assert fake_resolve.call_args.kwargs.get("proxy") == "http://127.0.0.1:8080"
