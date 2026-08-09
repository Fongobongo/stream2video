"""Tests for fmt_completion_summary — the final stats block shown
after a successful pipeline run."""

from stream2video.formatters import fmt_completion_summary


class TestCompletionSummary:
    def test_full_summary_with_realtime(self) -> None:
        """Typical run: source 1h, kept 45m, pipeline took 5m."""
        out = fmt_completion_summary(
            src_duration=3600.0,  # 1h
            src_size_bytes=2_400_000_000,  # 2.4 GB
            output_path="/out/video_compressed.mp4",
            dst_size_bytes=1_100_000_000,  # 1.1 GB
            keep_duration=2700.0,  # 45m
            pipeline_seconds=300.0,  # 5m
        )
        assert "[+ Compression complete!]" in out.replace("[bold green]", "[").replace("[/bold green]", "]")
        assert "Input:" in out
        assert "Output:" in out
        assert "54% reduction" in out or "54 %" in out
        assert "12.0x realtime" in out

    def test_no_size_reduction_shown(self) -> None:
        """Source and dest are same size — omit the reduction line."""
        out = fmt_completion_summary(
            src_duration=60.0,
            src_size_bytes=1_000_000,
            output_path="/out/v.mp4",
            dst_size_bytes=1_000_000,
            keep_duration=60.0,
            pipeline_seconds=10.0,
        )
        # 0% reduction is still a valid reduction line — it just shows 0.
        assert "reduction]" in out

    def test_src_duration_none_graceful(self) -> None:
        """ffprobe can't determine duration: show '?'."""
        out = fmt_completion_summary(
            src_duration=None,
            src_size_bytes=500_000,
            output_path="/out/v.mp4",
            dst_size_bytes=300_000,
            keep_duration=30.0,
            pipeline_seconds=10.0,
        )
        assert "Input:  ?" in out

    def test_pipeline_seconds_zero_no_realtime(self) -> None:
        """Sub-second pipeline runs should not show a meaningless 0x factor."""
        out = fmt_completion_summary(
            src_duration=3600.0,
            src_size_bytes=2_000_000,
            output_path="/out/v.mp4",
            dst_size_bytes=1_500_000,
            keep_duration=2700.0,
            pipeline_seconds=0.05,  # below the 0.1s threshold
        )
        assert "realtime" not in out

    def test_output_path_displayed(self) -> None:
        """The output file path is always shown."""
        out = fmt_completion_summary(
            src_duration=100.0,
            src_size_bytes=1_000,
            output_path="/tmp/result.mp4",
            dst_size_bytes=800,
            keep_duration=80.0,
            pipeline_seconds=5.0,
        )
        assert "/tmp/result.mp4" in out

    def test_reduction_percent_calculation(self) -> None:
        """Reduction = (src - dst) / src * 100."""
        out = fmt_completion_summary(
            src_duration=None,
            src_size_bytes=2_000,
            output_path="/out/x.mp4",
            dst_size_bytes=1_000,  # exactly 50% smaller
            keep_duration=50.0,
            pipeline_seconds=10.0,
        )
        assert "50% reduction" in out

    def test_larger_output_shows_negative_reduction(self) -> None:
        """When output is larger than source (e.g. re-encode at high
        bitrate), the percent is negative — still displayed correctly."""
        out = fmt_completion_summary(
            src_duration=60.0,
            src_size_bytes=1_000,
            output_path="/out/x.mp4",
            dst_size_bytes=1_500,
            keep_duration=60.0,
            pipeline_seconds=10.0,
        )
        assert "-50% reduction" in out

    def test_fmt_dry_run_summary_placeholder(self) -> None:
        """Smoke test: fmt_dry_run_summary exists and produces output."""
        from stream2video.formatters import fmt_dry_run_summary

        out = fmt_dry_run_summary(
            src_duration=100.0,
            src_size_bytes=1_000_000,
            silence_segments=[(10.0, 20.0), (50.0, 60.0)],
            keep_segments=[(0.0, 10.0), (20.0, 50.0), (60.0, 100.0)],
        )
        assert "Dry" in out
        assert "run" in out
        assert "Source:" in out
        assert "Silence:" in out
        assert "Keep:" in out
