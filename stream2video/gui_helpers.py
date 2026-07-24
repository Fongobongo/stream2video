"""Pure-function helpers extracted from ``gui.py`` (P2.x in the fix plan).

These functions have no Tk / no side effects so they can be unit-tested
without instantiating the GUI. The GUI class delegates formatting and
string construction to them; this both shrinks ``gui.py`` and lets the
test suite cover the actual logic (CLI command shape, status-line
truncation, ETA breakdown) instead of trying to drive the widgets via
pytest-qt or a Tkinter event loop.

Anything that touches ``self``, ``ctk``, ``messagebox``, ``Tk``, or the
clipboard stays in ``gui.py`` — only pure transformations live here.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from stream2video.formatters import fmt_clock_time, fmt_size, fmt_speed, fmt_time

# Maximum length of the GUI's status-line text. Kept as a module-level
# constant so tests can pin it and the GUI can import it without a
# circular dependency on the class.
STATUS_MAX = 50

# Throttle for ``_ui_status``: subsequent updates closer than this many
# seconds apart are dropped (unless ``force=True``). Keeps the status
# line readable during fast yt-dlp progress bursts.
STATUS_UPDATE_INTERVAL = 0.5


def build_cli_command(
    input_raw: str,
    output_dir: Path,
    *,
    method: str,
    encoder: str,
    video_quality: str,
    download_quality: str,
    audio_quality: str = "medium",
    software_fallback: str = "ask",
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    output_fps: str = "source",
    output_format: str = "video",
    x264_low_memory: bool = False,
    gapless_concat: bool = False,
    memory_limit_mb: str | int = "auto",
    memory_reserve_mb: int = 2048,
    segment_encode_timeout: int = 600,
    final_concat_timeout: int = 86400,
    silence_timeout: int = 36000,
    stall_kill_timeout: int = 300,
    waveform_timeout: int = 300,
    batch_chunk_size: int = 40,
    min_part_bytes: int = 1024,
    force: bool = False,
    delete_after: bool = False,
    config_path: Path | None = None,
) -> str:
    """Build the equivalent CLI invocation for the current GUI settings.

    Used by the GUI's "Copy CLI command" button so a user who's tuned
    the GUI can paste the same operation into a shell, a script, or
    documentation. Pure: returns the string, doesn't touch the
    clipboard.

    The shape mirrors what the CLI actually accepts (see
    ``stream2video.cli.main``). The newer flags (``--audio-quality``,
    ``--software-fallback``, ``--x264-preset``, ``--encoder-threads``,
    ``--output-fps``) are appended only when their value diverges from
    the default — that keeps the copied command readable when the user
    hasn't customised everything.

    ``config_path``: when set, the GUI writes the slider-only values
    (threshold/min_silence/margin) to this YAML file and passes it via
    ``-c`` so the copied command stays short. When None, those slider
    values are NOT in the command (the CLI would then use its own
    defaults for them); the caller should warn the user.
    """
    parts = ["stream2video"]
    if input_raw:
        parts.append(shlex.quote(input_raw))
    parts.extend(["-o", shlex.quote(str(output_dir))])
    if config_path is not None:
        parts.extend(["-c", shlex.quote(str(config_path))])
    parts.extend(["--method", method, "--encoder", encoder])
    parts.extend(["--video-quality", video_quality])
    parts.extend(["--download-quality", download_quality])
    # Newer flags — only append when they're not the default so the
    # copied command stays compact. The defaults match CONFIG_DEFAULTS
    # so a user who hasn't touched the advanced panel gets a clean
    # command-line reproducing their GUI choices.
    if audio_quality != "medium":
        parts.extend(["--audio-quality", audio_quality])
    if software_fallback != "ask":
        parts.extend(["--software-fallback", software_fallback])
    if x264_preset != "medium":
        parts.extend(["--x264-preset", x264_preset])
    if encoder_threads != "auto":
        parts.extend(["--encoder-threads", str(encoder_threads)])
    if output_fps != "source":
        parts.extend(["--output-fps", output_fps])
    if output_format != "video":
        parts.extend(["--output-format", output_format])
    if x264_low_memory:
        parts.append("--x264-low-memory")
    if gapless_concat:
        parts.append("--gapless-concat")
    if memory_limit_mb != "auto":
        parts.extend(["--memory-limit-mb", str(memory_limit_mb)])
    if memory_reserve_mb != 2048:
        parts.extend(["--memory-reserve-mb", str(memory_reserve_mb)])
    # P3.4: phase timeouts. Only appended when non-default so the
    # copied command stays readable when the user hasn't customised.
    if segment_encode_timeout != 600:
        parts.extend(["--segment-timeout", str(segment_encode_timeout)])
    if final_concat_timeout != 86400:
        parts.extend(["--final-concat-timeout", str(final_concat_timeout)])
    if silence_timeout != 36000:
        parts.extend(["--silence-timeout", str(silence_timeout)])
    if stall_kill_timeout != 300:
        parts.extend(["--stall-timeout", str(stall_kill_timeout)])
    if waveform_timeout != 300:
        parts.extend(["--waveform-timeout", str(waveform_timeout)])
    if batch_chunk_size != 40:
        parts.extend(["--batch-chunk-size", str(batch_chunk_size)])
    if min_part_bytes != 1024:
        parts.extend(["--min-part-bytes", str(min_part_bytes)])
    if force:
        parts.append("-f")
    if delete_after:
        parts.append("--delete-after")
    return " ".join(parts)


def truncate_status(text: str, max_len: int = STATUS_MAX) -> str:
    """Truncate a status-line string to ``max_len`` chars.

    Adds an ellipsis when truncating so the user can see the line was
    cut. A string at or under the limit is returned unchanged. The
    GUI's status bar has a fixed width; without truncation a long
    download URL or ffmpeg error would push the layout around.
    """
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def build_download_status(
    *,
    downloaded_bytes: float | None,
    total_bytes: float | None,
    speed: float | None,
    eta: float | None,
    pct: float | None = None,
) -> str:
    """Format a yt-dlp progress update as a status-line string.

    Two shapes:
      * ``total_bytes`` known: ``"Downloading 42.3% (123 MB / 291 MB) at 5.2 MiB/s ETA 1m 12s"``
      * ``total_bytes`` unknown: ``"Downloading 89 MB at 5.2 MiB/s"``

    Falls back to ``"?"`` for unknown fields so the line is always
    readable. Pure: callers (GUI worker thread, CLI progress callback)
    format the bytes/sizes via :mod:`stream2video.formatters` and pass
    them here.
    """
    downloaded_s = fmt_size(int(downloaded_bytes)) if downloaded_bytes else "?"
    speed_s = fmt_speed(speed)
    eta_s = fmt_time(eta) if eta else "?"
    if total_bytes:
        total_s = fmt_size(int(total_bytes))
        if pct is None:
            # Caller didn't compute percent — derive it here so the
            # status line stays self-contained.
            pct = 100.0 * (downloaded_bytes or 0.0) / total_bytes
        return f"Downloading {pct:.1f}% ({downloaded_s} / {total_s}) at {speed_s} ETA {eta_s}"
    return f"Downloading {downloaded_s} at {speed_s}"


def build_eta_tail(
    phase_remaining: float | None,
    more_phases: bool,
) -> str:
    """Render the ``Remaining:`` tail of the Elapsed/Remaining line.

    * ``phase_remaining=None`` or ``<=0``: ``"?"`` when more phases
      follow (we don't know how long they'll take), ``"—"`` when this
      is the last phase (the line is about to disappear).
    * Otherwise: ``"~1m 12s"`` for the last phase, ``"~1m 12s + ?"``
      when more phases follow (the ``+ ?`` flags that the total ETA
      is a lower bound, not a precise estimate).

    Pure: takes the ETA float and the more-phases flag, returns the
    display string. The GUI uses it inside its ``_ui_overall`` method.
    """
    if phase_remaining is None or phase_remaining <= 0:
        return "?" if more_phases else "—"
    formatted = f"~{fmt_time(phase_remaining)}"
    return f"{formatted} + ?" if more_phases else formatted


def build_overall_line(total_elapsed: float, eta_tail: str) -> str:
    """Format the full ``Elapsed: X | Remaining: Y`` line."""
    return f"Elapsed: {fmt_time(total_elapsed)} | Remaining: {eta_tail}"


def build_silence_info_line(
    *,
    num_silence: int,
    num_keep: int,
    keep_duration: float | None,
) -> str:
    """Format the ``Silence: N segments / Keep: M segments (Xm Ys)`` line.

    ``keep_duration=None`` skips the duration parenthetical so the line
    works for the pre-cut preview where we don't yet know how long the
    output will be.
    """
    if keep_duration is not None and keep_duration > 0:
        return (
            f"Silence: {num_silence} segments\n"
            f"Keep: {num_keep} segments ({fmt_time(keep_duration)})"
        )
    return f"Silence: {num_silence} segments\nKeep: {num_keep} segments"


def build_waveform_view_label(
    *,
    view_start: float,
    view_end: float,
    zoom: float,
) -> str:
    """Format the waveform popup's view-range label.

    Renders as ``"00:00:00 - 00:01:30  |  15x"`` so the user can see
    both the visible window and the zoom level at a glance. Pure: takes
    the view bounds and zoom multiplier, returns the display string.
    """
    return f"{fmt_clock_time(view_start)} - {fmt_clock_time(view_end)}  |  {zoom:.1f}x"


def should_update_status(
    last_update_monotonic: float,
    now_monotonic: float,
    *,
    force: bool = False,
    interval: float = STATUS_UPDATE_INTERVAL,
) -> bool:
    """Decide whether a status-line update should be applied or throttled.

    The GUI's ``_ui_status`` receives many rapid updates from yt-dlp
    progress callbacks; without throttling the status bar redraws
    faster than the eye can read. ``force=True`` bypasses the throttle
    for explicit user-initiated updates (step transitions, errors).
    """
    if force:
        return True
    return now_monotonic - last_update_monotonic >= interval


def build_completion_summary(
    src_size_bytes: int,
    src_duration: float | None,
    dst_size_bytes: int,
    dst_duration: float,
    pipeline_seconds: float,
    output_path: str,
) -> dict:
    """Build the user-facing strings emitted on pipeline completion.

    Pure function — no Tk / no side effects — so it can be unit-tested
    without instantiating the GUI. Previously lived inline in
    ``gui._build_completion_summary`` (Этап 10 extraction).

    Returns a dict with keys:
      - status:    one-line headline for the status bar: 'Complete!' plus
                   the total wall-clock in parentheses, e.g. 'Complete! (23m 5s)'.
                   Size and duration go in the log block and the popup only.
      - log_lines: list of log lines (with '=' separators) for grep-ability.
                   Contains the full src->dst size/duration breakdown.
      - popup:     multi-line message for the 'Complete' messagebox.
                   Full breakdown with Source/Output labels.
    """
    src_size_s = fmt_size(src_size_bytes)
    dst_size_s = fmt_size(dst_size_bytes)
    src_dur_s = fmt_clock_time(src_duration)
    dst_dur_s = fmt_clock_time(dst_duration)
    pipe_s = fmt_time(pipeline_seconds)

    sep = "=" * 60
    log_lines = [
        sep,
        f"[SUCCESS] Output: {output_path}",
        f"  Size:     {src_size_s} -> {dst_size_s}",
        f"  Duration: {src_dur_s} -> {dst_dur_s}",
        f"  Pipeline: {pipe_s}",
        sep,
    ]

    popup = (
        f"Video saved to:\n{output_path}\n\n"
        f"Source:  {src_size_s}, {src_dur_s}\n"
        f"Output:  {dst_size_s}, {dst_dur_s}\n\n"
        f"Pipeline: {pipe_s}"
    )

    return {
        "status": f"Complete! ({pipe_s})",
        "log_lines": log_lines,
        "popup": popup,
    }


# Back-compat alias so existing imports of ``_build_completion_summary``
# (inside gui.py and any downstream code) keep working during the
# incremental refactor.
_build_completion_summary = build_completion_summary
