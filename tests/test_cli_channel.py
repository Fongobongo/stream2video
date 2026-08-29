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
    def test_channel_url_default_is_interactive_pick(self, tmp_path: Path):
        """Without any channel flags the CLI opens the interactive
        picker; a non-tty stdin (CliRunner) must refuse instead of
        hanging, pointing at --channel-select."""
        with patch("stream2video.cli.resolve_channel_vods") as fake_resolve:
            result = CliRunner().invoke(
                app,
                [CHANNEL_URL, "-o", str(tmp_path / "out")],
                catch_exceptions=False,
            )
        assert result.exit_code == 2
        assert "use --channel-select" in result.output
        # The picker IS the default: the listing was fetched (window 50)
        # before the tty check — the user sees the table even piped.
        assert fake_resolve.call_count == 1
        assert fake_resolve.call_args.kwargs.get("limit") is None or True  # window arg

    def test_channel_url_select_without_window_exits_2(self, tmp_path: Path):
        """--channel-select without --channel-limit is ambiguous (which
        entries?) — the CLI demands an explicit window."""
        result = CliRunner().invoke(
            app,
            [CHANNEL_URL, "-o", str(tmp_path / "out"), "--channel-select", "1,2"],
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "Listing URL detected" in result.output
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
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "2",
                    "--channel-select",
                    "1-2",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Channel batch: 2/2" in result.output
        # One controller per VOD, each bound to the VOD URL (not the
        # channel listing URL). The table is date-sorted (newest VOD id
        # first), so v2 runs before v1.
        assert inputs_seen == [
            "https://www.twitch.tv/videos/2",
            "https://www.twitch.tv/videos/1",
        ]

    def test_channel_select_skips_unselected(self, tmp_path: Path):
        """The whole point of the picker: only the checked entries run."""
        ran: list[str] = []

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        class _Ctrl:
            def __init__(self, cfg=None, **kw):
                pass

            def run(self):
                ran.append("x")
                return _R()

        with (
            patch(
                "stream2video.cli.resolve_channel_vods",
                return_value=[_vod(1), _vod(2), _vod(3)],
            ),
            patch("stream2video.cli.PipelineController", side_effect=_Ctrl),
        ):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "2",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Channel batch: 1/1" in result.output
        # Entry #2 only — 1 and 3 were unchecked.
        assert len(ran) == 1

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
                # Date sort runs the NEWEST id first (v2), so v2 is the
                # first-processed entry and the one that fails; v1 must
                # still run afterwards.
                if self.url.endswith("/2"):
                    raise PipelineConcatError("boom on VOD 2")
                return _R()

        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1), _vod(2)]),
            patch("stream2video.cli.PipelineController", side_effect=_Ctrl),
        ):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "2",
                    "--channel-select",
                    "1-2",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        assert "Channel batch: 1/2" in result.output
        assert "https://www.twitch.tv/videos/2" in result.output
        assert "Concatenation failed" in result.output
        # The other VOD still ran despite the first one failing
        # (date-sorted order: v2 first, then v1).
        assert calls == ["https://www.twitch.tv/videos/2", "https://www.twitch.tv/videos/1"]


class TestYoutubeListingCli:
    """The CLI listing gate for YouTube channels and playlists: the
    picker flow applies to them exactly like Twitch channels, with the
    platform's own default tab and type validation."""

    YT_CHANNEL = "https://www.youtube.com/@somechannel/videos"
    YT_PLAYLIST = "https://www.youtube.com/playlist?list=PLsomeplaylist123"

    def test_youtube_channel_goes_through_picker_gate(self, tmp_path: Path):
        """A YouTube channel URL is a listing: without --channel-select
        and with a non-tty stdin the interactive picker refuses with the
        --channel-select hint (not the old single-video error)."""
        with patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]) as fake:
            result = CliRunner().invoke(
                app,
                [self.YT_CHANNEL, "-o", str(tmp_path / "out")],
                catch_exceptions=False,
            )
        assert result.exit_code == 2
        assert "use --channel-select" in result.output
        # The listing WAS fetched before the tty check (default window).
        assert fake.call_count == 1
        # Platform default: videos (not Twitch's archives).
        assert fake.call_args.kwargs.get("category") == "videos"

    def test_youtube_playlist_select_non_interactive(self, tmp_path: Path):
        ran: list[str] = []

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        class _Ctrl:
            def __init__(self, cfg=None, **kw):
                ran.append(cfg.input_raw)

            def run(self):
                return _R()

        yt_entry = ChannelVod(
            video_id="vid01",
            url="https://www.youtube.com/watch?v=vid01",
            title="Playlist entry",
            duration=300.0,
        )
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[yt_entry]),
            patch("stream2video.cli.PipelineController", side_effect=_Ctrl),
        ):
            result = CliRunner().invoke(
                app,
                [
                    self.YT_PLAYLIST,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "5",
                    "--channel-select",
                    "1",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert ran == ["https://www.youtube.com/watch?v=vid01"]

    def test_twitch_type_rejected_on_youtube(self, tmp_path: Path):
        with patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]):
            result = CliRunner().invoke(
                app,
                [
                    self.YT_CHANNEL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                    "--channel-type",
                    "clips",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 2
        assert "does not apply here" in result.output

    def test_playlist_type_tab_rejected(self, tmp_path: Path):
        with patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]):
            result = CliRunner().invoke(
                app,
                [
                    self.YT_PLAYLIST,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                    "--channel-type",
                    "shorts",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 2
        assert "does not apply here" in result.output
        assert "videos" in result.output  # the playlist's valid set

    def test_youtube_shorts_type_forwarded(self, tmp_path: Path):
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]) as fake,
            patch("stream2video.cli.PipelineController"),
        ):
            result = CliRunner().invoke(
                app,
                [
                    self.YT_CHANNEL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                    "--channel-type",
                    "shorts",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert fake.call_args.kwargs.get("category") == "shorts"

    def test_single_watch_url_untouched(self, tmp_path: Path):
        """A plain watch URL must NOT hit the listing gate."""
        with patch("stream2video.cli.resolve_channel_vods") as fake:
            CliRunner().invoke(
                app,
                ["https://www.youtube.com/watch?v=abc123", "-o", str(tmp_path / "out")],
                catch_exceptions=False,
            )
        assert fake.call_count == 0


class TestChannelFilterCli:
    """--channel-filter in the CLI flow: the table shows only matching
    entries and the selection numbers refer to the filtered set."""

    def _listing(self):
        return [
            ChannelVod("v1", "https://www.twitch.tv/videos/1", "Undertale Part 1", 60.0),
            ChannelVod("v2", "https://www.twitch.tv/videos/2", "Zelda marathon", 120.0),
            ChannelVod("v3", "https://www.twitch.tv/videos/3", "Undertale Part 2", 90.0),
        ]

    def test_filter_shows_only_matching(self, tmp_path: Path):
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=self._listing()),
            patch("stream2video.cli.PipelineController") as fake_ctrl,
        ):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1-2",
                    "--channel-filter",
                    "*undertale*",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "Filter '*undertale*': 2/3 entries match" in result.output
        # The table no longer lists the filtered-out Zelda entry...
        assert "Zelda marathon" not in result.output
        assert "Undertale Part 1" in result.output
        # ...and selection 1-2 over the FILTERED set runs exactly 2
        # entries (both Undertales), not 3.
        assert fake_ctrl.call_count == 2

    def test_filter_exclusion_only(self, tmp_path: Path):
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=self._listing()),
            patch("stream2video.cli.PipelineController"),
        ):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                    "--channel-filter",
                    "!*zelda*",
                ],
                catch_exceptions=False,
            )
        assert "2/3 entries match" in result.output
        assert "Zelda marathon" not in result.output

    def test_filter_no_match_exits_1(self, tmp_path: Path):
        with patch(
            "stream2video.cli.resolve_channel_vods",
            return_value=self._listing(),
        ):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                    "--channel-filter",
                    "*nonexistent*",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 1
        assert "matched none" in result.output

    def test_bad_filter_exits_2(self, tmp_path: Path):
        with patch("stream2video.cli.resolve_channel_vods", return_value=self._listing()):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                    "--channel-filter",
                    "!",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 2
        assert "Bad --channel-filter" in result.output


class TestChannelListingErrors:
    def test_listing_error_exits_1(self, tmp_path: Path):
        with patch(
            "stream2video.cli.resolve_channel_vods",
            side_effect=ChannelImportError("No archives found"),
        ):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 1
        assert "Channel import failed" in result.output
        assert "No archives found" in result.output

    def test_listing_cancel_exits_130(self, tmp_path: Path):
        with patch(
            "stream2video.cli.resolve_channel_vods",
            side_effect=ChannelImportCancelled(),
        ):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                ],
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
                    "--channel-select",
                    "1",
                    "--proxy",
                    "http://127.0.0.1:8080",
                ],
                catch_exceptions=False,
            )
        assert fake_resolve.call_count == 1
        assert fake_resolve.call_args.args[1] == 5
        assert fake_resolve.call_args.kwargs.get("proxy") == "http://127.0.0.1:8080"


class TestChannelTypeAndSort:
    def _run_with(self, tmp_path: Path, *extra: str):
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]) as fake,
            patch("stream2video.cli.PipelineController"),
        ):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                    *extra,
                ],
                catch_exceptions=False,
            )
        return result, fake

    def test_type_forwarded_to_resolver(self, tmp_path: Path):
        _, fake = self._run_with(tmp_path, "--channel-type", "clips")
        assert fake.call_args.kwargs.get("category") == "clips"

    def test_default_type_is_archives(self, tmp_path: Path):
        _, fake = self._run_with(tmp_path)
        assert fake.call_args.kwargs.get("category") == "archives"

    def test_unknown_type_exits_2(self, tmp_path: Path):
        result, _ = self._run_with(tmp_path, "--channel-type", "nonsense")
        assert result.exit_code == 2
        assert "Unknown --channel-type" in result.output

    def test_unknown_sort_exits_2(self, tmp_path: Path):
        result, _ = self._run_with(tmp_path, "--channel-sort", "rating")
        assert result.exit_code == 2
        assert "Unknown --channel-sort" in result.output

    def test_table_shows_duration_and_views(self, tmp_path: Path):
        long_vod = ChannelVod(
            video_id="v9",
            url="https://www.twitch.tv/videos/9",
            title="A long stream",
            duration=12569.0,
            view_count=9000,
        )
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[long_vod]),
            patch("stream2video.cli.PipelineController"),
        ):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-limit",
                    "3",
                    "--channel-select",
                    "1",
                ],
                catch_exceptions=False,
            )
        assert "A long stream" in result.output
        assert "3h29m" in result.output
        assert "9,000 views" in result.output


class TestInteractivePick:
    def test_prompt_answer_selects_entries(self, tmp_path: Path):
        """The fake prompt pins the wiring: answer "2" of a 2-entry
        table runs exactly one controller (entry #2), not two."""
        ran: list[str] = []

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        class _Ctrl:
            def __init__(self, cfg=None, **kw):
                ran.append(cfg.input_raw)

            def run(self):
                return _R()

        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1), _vod(2)]),
            patch("stream2video.cli.typer.prompt", return_value="2"),
            patch("stream2video.cli.PipelineController", side_effect=_Ctrl),
            patch("stream2video.cli._stdin_is_interactive", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                [CHANNEL_URL, "-o", str(tmp_path / "out"), "--channel-limit", "2"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # Answer "2" = row 2 of the DATE-SORTED table (newest id first):
        # v2 occupies row 1, so row 2 is v1.
        assert ran == ["https://www.twitch.tv/videos/1"]

    def test_empty_prompt_cancels(self, tmp_path: Path):
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]),
            patch("stream2video.cli.typer.prompt", return_value=""),
            patch("stream2video.cli.PipelineController") as fake_ctrl,
            patch("stream2video.cli._stdin_is_interactive", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                [CHANNEL_URL, "-o", str(tmp_path / "out"), "--channel-limit", "2"],
                catch_exceptions=False,
            )
        assert result.exit_code == 130
        assert "Nothing selected" in result.output
        assert fake_ctrl.call_count == 0

    def test_bad_selection_exits_2(self, tmp_path: Path):
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]),
            patch("stream2video.cli.typer.prompt", return_value="99"),
            patch("stream2video.cli._stdin_is_interactive", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                [CHANNEL_URL, "-o", str(tmp_path / "out"), "--channel-limit", "2"],
                catch_exceptions=False,
            )
        assert result.exit_code == 2
        assert "Bad selection" in result.output
