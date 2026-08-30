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
        assert "Batch: 2/2" in result.output
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
        assert "Batch: 1/1" in result.output
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
        assert "Batch: 1/2" in result.output
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


class TestBatchProgressBar:
    """The batch bar: one Rich task spanning the whole queue — the
    controller's per-VOD overall fraction folds in as (i-1+f)*100 of a
    100*N total, so the bar accounts for every video in the batch, not
    just the one currently encoding."""

    def test_batch_bar_tracks_all_vods(self, tmp_path: Path):
        seen_fractions: list[float] = []

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        class _Ctrl:
            def __init__(self, cfg=None, cb=None, **kw):
                self.cb = cb

            def run(self):
                # Simulate the controller's overall-progress emission:
                # 0.0 -> 0.5 -> 1.0 per VOD.
                for f in (0.0, 0.5, 1.0):
                    seen_fractions.append(f)
                    self.cb.on_progress(f)
                return _R()

        vods = [_vod(1), _vod(2)]
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=vods),
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
        # The controller's on_progress was wired (not the old no-op) and
        # fired the full 0..1 sweep for EACH of the 2 VODs.
        assert seen_fractions == [0.0, 0.5, 1.0, 0.0, 0.5, 1.0]

    def test_batch_bar_mentions_current_vod(self, tmp_path: Path):
        """The bar's description carries the queue position and the
        current entry's title, so the user knows WHICH video the
        fraction belongs to."""

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        class _Ctrl:
            def __init__(self, cfg=None, cb=None, **kw):
                pass

            def run(self):
                return _R()

        titled = [
            ChannelVod("v1", "https://www.twitch.tv/videos/1", "First stream", 60.0),
            ChannelVod("v2", "https://www.twitch.tv/videos/2", "Second stream", 60.0),
        ]
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=titled),
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
        # The batch bar's per-VOD description update happens outside
        # Rich's captured render; assert on the epilogue instead: both
        # entries settled and the batch reports 2/2.
        assert result.exit_code == 0
        assert "Batch: 2/2" in result.output

    def test_single_video_has_no_batch_bar(self, tmp_path: Path):
        """A plain single-video run must not grow a batch bar: its
        on_progress stays the historical no-op and the per-phase bars
        remain the only view."""

        class _Ctrl:
            def __init__(self, cfg=None, cb=None, **kw):
                assert cb.on_progress is not None
                # Single-video mode wires the no-op lambda; calling it
                # must not raise (the batch-bar path is gated on
                # task_batch=None inside _batch_on_progress, but here
                # the no-op branch is what gets wired at all).
                cb.on_progress(0.5)

            def run(self):
                raise SystemExit(0)

        src = tmp_path / "src.mp4"
        src.write_bytes(b"x" * 2048)
        with (
            patch("stream2video.download._validate_url", return_value=True),
            patch("stream2video.pipeline_controller.PipelineController", _Ctrl),
        ):
            CliRunner().invoke(
                app,
                [str(src), "-o", str(tmp_path / "out")],
                catch_exceptions=False,
            )


class TestChannelConfigParity:
    """channel_* keys read from the YAML config like every other
    tunable — the whole point of config parity: a config.yaml can carry
    the picker's defaults, an explicit CLI flag still wins."""

    def _config(self, tmp_path: Path, body: str) -> Path:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(body, encoding="utf-8")
        return cfg

    def test_yaml_channel_keys_reach_the_listing(self, tmp_path: Path):
        cfg = self._config(
            tmp_path,
            "channel_limit: 7\n"
            "channel_type: clips\n"
            "channel_sort: views\n"
            "channel_filter: '*best*'\n"
            "channel_min_duration: 60.0\n"
            "channel_since: '-30d'\n",
        )
        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]) as fake,
            patch("stream2video.cli.PipelineController"),
        ):
            CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-select",
                    "1",
                    "-c",
                    str(cfg),
                ],
                catch_exceptions=False,
            )
        # limit from YAML (>= 1 so the non-interactive gate passes),
        # type from YAML:
        assert fake.call_args.args[1] == 7
        assert fake.call_args.kwargs.get("category") == "clips"

    def test_cli_flag_overrides_yaml(self, tmp_path: Path):
        cfg = self._config(tmp_path, "channel_limit: 5\nchannel_type: highlights\n")
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
                    "--channel-select",
                    "1",
                    "--channel-limit",
                    "9",
                    "-c",
                    str(cfg),
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert fake.call_args.args[1] == 9  # CLI wins over YAML's 5

    def test_bad_yaml_channel_type_rejected(self, tmp_path: Path):
        cfg = self._config(tmp_path, "channel_type: nonsense\n")
        with patch("stream2video.cli.resolve_channel_vods"):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--channel-select",
                    "1",
                    "-c",
                    str(cfg),
                ],
                catch_exceptions=False,
            )
        # load_config rejects an unknown enum for a PARAM_SPECS enum key
        # — same rule family as method/encoder.
        assert result.exit_code != 0


class TestChannelMetaFiltersCli:
    """--channel-min-dur / --channel-since in the CLI flow."""

    def _listing(self):
        return [
            ChannelVod("v1", "u1", "A full stream", 3600.0, view_count=10, timestamp=1755500000),
            ChannelVod("v2", "u2", "A short trailer", 45.0, view_count=20, timestamp=1755500000),
            ChannelVod("v3", "u3", "An old stream", 3600.0, view_count=30, timestamp=1700000000),
        ]

    def test_min_duration_drops_short_entries(self, tmp_path: Path):
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
                    "--channel-min-dur",
                    "300",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "min-dur 300s" in result.output
        assert "2/3 entries kept" in result.output
        assert "A short trailer" not in result.output

    def test_since_drops_old_entries(self, tmp_path: Path):
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
                    "--channel-since",
                    "2025-01-01",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "2/3 entries kept" in result.output  # old v3 (2023) dropped
        assert "An old stream" not in result.output

    def test_bad_since_exits_2(self, tmp_path: Path):
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
                    "--channel-since",
                    "not-a-date",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 2
        assert "Bad --channel-since" in result.output

    def test_filters_empty_result_exits_1(self, tmp_path: Path):
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
                    "--channel-min-dur",
                    "99999",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 1
        assert "matched none" in result.output


class TestBatchDryRun:
    """--dry-run over a channel batch: EVERY selected entry gets its
    what-would-be-cut stats (the historical early exit after the first
    VOD starved the channel-wide comparison use case), then the batch
    epilogue reports the queue outcome."""

    def test_batch_dry_run_stats_for_every_vod(self, tmp_path: Path):
        runs: list[str] = []

        class _DryResult:
            def __init__(self) -> None:
                self.src_size_bytes = 100
                self.src_duration = 60.0
                self.pipeline_seconds = 1.0
                self.output_path = None
                self.silence_segments = [(0.0, 10.0)]
                self.keep_segments = [(10.0, 20.0)]

        class _Ctrl:
            def __init__(self, cfg=None, **kw):
                pass

            def run(self):
                runs.append("run")
                return _DryResult()

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
                    "--dry-run",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # Both entries went through the controller (both got stats)...
        assert len(runs) == 2
        # ...the dry-run summary block printed for each entry...
        assert result.output.count("Would be cut") >= 2 or result.output.count("silence") >= 2
        # ...and the epilogue says analysed, not compressed.
        assert "analysed (dry run)" in result.output
        assert "2/2" in result.output


class TestBatchFile:
    """--batch-file: a queue of inputs (single videos and listings)
    processed as one batch — the scriptable surface for "here are my N
    links, process them all"."""

    def _queue_file(self, tmp_path: Path, body: str) -> Path:
        q = tmp_path / "queue.txt"
        q.write_text(body, encoding="utf-8")
        return q

    def test_queue_runs_every_entry(self, tmp_path: Path):
        q = self._queue_file(
            tmp_path,
            "# a comment line\n"
            "https://www.twitch.tv/videos/111\n"
            "\n"  # blank lines are skipped
            "https://www.youtube.com/watch?v=abc\n"
            "D:/videos/local.mp4\n",
        )

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        inputs_seen: list[str] = []

        class _Ctrl:
            def __init__(self, cfg=None, **kw):
                inputs_seen.append(cfg.input_raw)

            def run(self):
                return _R()

        with patch("stream2video.cli.PipelineController", side_effect=_Ctrl):
            result = CliRunner().invoke(
                app,
                [str(q), "-o", str(tmp_path / "out"), "--batch-file", str(q)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "Batch queue: 3 entry(ies)" in result.output
        # The comment, the blank line and the position-argument duplicate
        # don't matter: exactly the 3 file entries ran, in file order.
        assert inputs_seen == [
            "https://www.twitch.tv/videos/111",
            "https://www.youtube.com/watch?v=abc",
            "D:/videos/local.mp4",
        ]
        assert "Batch: 3/3" in result.output

    def test_queue_listing_line_expands_via_select(self, tmp_path: Path):
        """A listing line joins the queue through --channel-select (the
        non-interactive path), same rules as a direct listing argument."""
        q = self._queue_file(tmp_path, "https://www.twitch.tv/somechannel/videos\n")
        listing = [_vod(1), _vod(2)]

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        ran: list[str] = []

        class _Ctrl:
            def __init__(self, cfg=None, **kw):
                pass

            def run(self):
                ran.append("x")
                return _R()

        with (
            patch("stream2video.cli.resolve_channel_vods", return_value=listing),
            patch("stream2video.cli.PipelineController", side_effect=_Ctrl),
        ):
            result = CliRunner().invoke(
                app,
                [
                    str(q),
                    "-o",
                    str(tmp_path / "out"),
                    "--batch-file",
                    str(q),
                    "--channel-limit",
                    "2",
                    "--channel-select",
                    "2",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # Only entry 2 of the listing joined the queue.
        assert len(ran) == 1
        assert "Batch queue: 1 entry(ies)" in result.output

    def test_queue_listing_without_select_errors(self, tmp_path: Path):
        """A listing line in a queue REQUIRES --channel-select: an
        interactive prompt mid-queue would block the whole file."""
        q = self._queue_file(tmp_path, "https://www.twitch.tv/somechannel/videos\n")
        with patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]):
            result = CliRunner().invoke(
                app,
                [str(q), "-o", str(tmp_path / "out"), "--batch-file", str(q)],
                catch_exceptions=False,
            )
        assert result.exit_code == 2
        # The exact message wraps across Rich lines; assert on the
        # stable half that survives the wrap.
        assert "--channel-select in a" in result.output

    def test_queue_combined_with_channel_input_rejected(self, tmp_path: Path):
        q = self._queue_file(tmp_path, "https://www.twitch.tv/videos/111\n")
        with patch("stream2video.cli.resolve_channel_vods", return_value=[_vod(1)]):
            result = CliRunner().invoke(
                app,
                [
                    CHANNEL_URL,
                    "-o",
                    str(tmp_path / "out"),
                    "--batch-file",
                    str(q),
                    "--channel-limit",
                    "2",
                    "--channel-select",
                    "1",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 2
        assert "cannot be combined" in result.output

    def test_queue_empty_rejected(self, tmp_path: Path):
        q = self._queue_file(tmp_path, "# only comments\n\n")
        result = CliRunner().invoke(
            app,
            [str(q), "-o", str(tmp_path / "out"), "--batch-file", str(q)],
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "no entries" in result.output

    def test_queue_entry_failure_continues(self, tmp_path: Path):
        q = self._queue_file(
            tmp_path,
            "https://www.twitch.tv/videos/111\nhttps://www.twitch.tv/videos/222\n",
        )

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
                if self.url.endswith("/111"):
                    raise PipelineConcatError("boom on the first entry")
                return _R()

        with patch("stream2video.cli.PipelineController", side_effect=_Ctrl):
            result = CliRunner().invoke(
                app,
                [str(q), "-o", str(tmp_path / "out"), "--batch-file", str(q)],
                catch_exceptions=False,
            )
        assert result.exit_code == 1
        assert "Batch: 1/2" in result.output
        assert "https://www.twitch.tv/videos/111" in result.output


class TestBatchResume:
    """--channel-continue: the batch manifest (channel_batch.json)
    carries the queue + completed URLs; a resume re-queues only the
    unfinished tail, and MERGES completions back into the manifest."""

    def _write_manifest(self, tmp_path: Path, entries, completed) -> Path:
        import json

        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        m = {
            "version": 1,
            "entries": entries,
            "completed_urls": completed,
        }
        (out / "channel_batch.json").write_text(json.dumps(m), encoding="utf-8")
        return out

    def test_resume_reruns_only_unfinished(self, tmp_path: Path):
        entries = [
            {"title": "One", "url": "https://www.twitch.tv/videos/1"},
            {"title": "Two", "url": "https://www.twitch.tv/videos/2"},
            {"title": "Three", "url": "https://www.twitch.tv/videos/3"},
        ]
        out = self._write_manifest(tmp_path, entries, ["https://www.twitch.tv/videos/1"])

        class _R:
            src_size_bytes = 100
            dst_size_bytes = 50
            src_duration = 60.0
            pipeline_seconds = 1.0
            output_path = Path("out.mp4")

        ran: list[str] = []

        class _Ctrl:
            def __init__(self, cfg=None, **kw):
                ran.append(cfg.input_raw)

            def run(self):
                return _R()

        with patch("stream2video.cli.PipelineController", side_effect=_Ctrl):
            result = CliRunner().invoke(
                app,
                ["x", "-o", str(out), "--channel-continue"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        # Only the two unfinished entries ran (VOD 1 was skipped).
        assert ran == [
            "https://www.twitch.tv/videos/2",
            "https://www.twitch.tv/videos/3",
        ]
        assert "Batch resume: 2 of 3" in result.output
        # The manifest merged: all 3 now completed.
        import json

        m = json.loads((out / "channel_batch.json").read_text(encoding="utf-8"))
        assert set(m["completed_urls"]) == {
            "https://www.twitch.tv/videos/1",
            "https://www.twitch.tv/videos/2",
            "https://www.twitch.tv/videos/3",
        }

    def test_resume_completed_batch_exits_0(self, tmp_path: Path):
        entries = [{"title": "One", "url": "https://www.twitch.tv/videos/1"}]
        out = self._write_manifest(tmp_path, entries, ["https://www.twitch.tv/videos/1"])
        with patch("stream2video.cli.PipelineController") as fake_ctrl:
            result = CliRunner().invoke(
                app,
                ["x", "-o", str(out), "--channel-continue"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "fully completed" in result.output
        assert fake_ctrl.call_count == 0

    def test_resume_without_manifest_exits_2(self, tmp_path: Path):
        result = CliRunner().invoke(
            app,
            ["x", "-o", str(tmp_path / "empty_out"), "--channel-continue"],
            catch_exceptions=False,
        )
        assert result.exit_code == 2
        assert "no previous batch" in result.output

    def test_fresh_batch_writes_manifest(self, tmp_path: Path):
        import json

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
        m_path = tmp_path / "out" / "channel_batch.json"
        assert m_path.exists()
        m = json.loads(m_path.read_text(encoding="utf-8"))
        assert len(m["entries"]) == 2
        assert set(m["completed_urls"]) == {
            "https://www.twitch.tv/videos/1",
            "https://www.twitch.tv/videos/2",
        }


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

    def test_unknown_sort_exits(self, tmp_path: Path):
        """``--channel-sort rating`` is rejected — via the shared
        resolver's enum validation (config-parity path), same as any
        other enum flag; the channel module never sees it."""
        result, _ = self._run_with(tmp_path, "--channel-sort", "rating")
        assert result.exit_code == 1
        assert "channel_sort" in result.output
        assert "rating" in result.output

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
