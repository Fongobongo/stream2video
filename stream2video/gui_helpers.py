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

import re
import shlex
from pathlib import Path

from stream2video.formatters import fmt_clock_time, fmt_size, fmt_speed, fmt_time

# Maximum length of the GUI's status-line text (per line when wrapped).
# Kept as a module-level constant so tests can pin it and the GUI can
# import it without a circular dependency on the class. The two-line
# status label (row 0 + row 1 in prog_frame) wraps via _wrap_status_lines,
# so per-line budget stays 50 but total visible chars double.
STATUS_MAX = 50
# Two-line status uses STATUS_MAX per line, so wrap budgets are separate
# but truncation keeps total within 2*STATUS_MAX.
STATUS_MAX_LINES = 2

# Throttle for ``_ui_status``: subsequent updates closer than this many
# seconds apart are dropped (unless ``force=True``). Keeps the status
# line readable during fast yt-dlp progress bursts.
STATUS_UPDATE_INTERVAL = 0.5

# Overall-ETA is hidden until the whole pipeline's progress fraction
# reaches this value. Below it the estimate (elapsed / progress) is so
# noisy it would show absurd values like "5h" after 30 seconds.
TOTAL_ETA_MIN_PROGRESS = 0.02

# EMA factor applied to each raw ETA sample. 0.25 gives a responsive
# but visibly stable readout when the callbacks fire once per second.
_ETA_SMOOTHING = 0.25


class EtaSmoother:
    """Exponential moving average for a stream of ETA samples (seconds).

    The pipeline's ETA callbacks (``elapsed / fraction - elapsed``)
    jitter hectically second-to-second early in a phase. ``update()``
    returns a smoothed value that converges to the raw estimate with a
    ~4-sample time constant. ``update(None)`` pauses sampling and just
    replays the last smoothed value (used when a callback has no ETA
    yet); ``reset()`` clears the filter at phase boundaries so the
    previous phase's estimate doesn't bleed into the next one.
    """

    def __init__(self, alpha: float = _ETA_SMOOTHING) -> None:
        self._alpha = alpha
        self._smoothed: float | None = None

    def reset(self) -> None:
        self._smoothed = None

    def update(self, raw: float | None) -> float | None:
        if raw is None:
            return self._smoothed
        raw = max(0.0, raw)
        if self._smoothed is None:
            self._smoothed = raw
        else:
            self._smoothed = self._smoothed + self._alpha * (raw - self._smoothed)
        return self._smoothed


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
    use_crf: bool = False,
    gapless_concat: bool = False,
    low_process_priority: bool = False,
    preset: str = "balanced",
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
    if use_crf:
        parts.append("--use-crf")
    if gapless_concat:
        parts.append("--gapless-concat")
    if low_process_priority:
        parts.append("--low-process-priority")
    if preset != "balanced":
        parts.extend(["--preset", preset])
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


def _wrap_status_lines(text: str, max_len: int = STATUS_MAX, max_lines: int = STATUS_MAX_LINES) -> list[str]:
    """Wrap a status string into up to *max_lines* lines of *max_len* each.

    Word-aware where possible (breaks on space); hard-breaks when a
    single token exceeds max_len. Truncates with an ellipsis on the last
    line if the budget is exhausted. Pure helper for the two-line status
    label — avoids widening the window to show full progress digits.
    """
    if not text:
        return [""] * max_lines
    # Truncate total budget first to avoid runaway wrapping on very long errors
    total_max = max_len * max_lines
    # Use truncation with ellipsis if over budget, but wrap then truncate last line
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        if not w:
            continue
        # Handle tokens longer than max_len by hard-splitting
        while len(w) > max_len:
            if cur:
                lines.append(cur)
                cur = ""
                if len(lines) >= max_lines:
                    break
            lines.append(w[:max_len])
            w = w[max_len:]
            if len(lines) >= max_lines:
                break
        if len(lines) >= max_lines:
            break
        sep = " " if cur else ""
        if len(cur) + len(sep) + len(w) <= max_len:
            cur = cur + sep + w
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    # Truncate overflow with ellipsis on last line if needed
    if len(" ".join(text.split())) > total_max and lines and len(lines[-1]) == max_len:
        lines[-1] = lines[-1][: max_len - 1] + "…"
    # Pad to max_lines with empty strings for callers that expect two entries
    while len(lines) < max_lines:
        lines.append("")
    return lines[:max_lines]


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


def build_total_line(total_elapsed: float, overall_est: float | None) -> str:
    """Format the ``Total:`` wall-clock label with an optional ETA.

    ``overall_est`` is the estimated total wall-clock of the whole
    pipeline (``elapsed / overall_progress``); None when the estimate
    is still too noisy — the label then shows elapsed only.
    """
    if overall_est is not None and overall_est > total_elapsed:
        return f"Total: {fmt_time(total_elapsed)} / ~{fmt_time(overall_est)}"
    return f"Total: {fmt_time(total_elapsed)}"


def build_progress_meta_line(
    total_elapsed: float,
    eta_tail: str,
    overall_est: float | None,
) -> str:
    """One-line Elapsed/Remaining/Total readout for the bar row.

    Collapses the former two-row readout (Elapsed/Remaining on one line,
    Total on another) into a single string so the progress frame stays
    two rows tall: the status line + the bar row. ``eta_tail`` comes
    from :func:`build_eta_tail`; ``overall_est`` is the estimated
    whole-run wall-clock (None while still too noisy — omitted).
    """
    parts = [build_overall_line(total_elapsed, eta_tail)]
    if overall_est is not None and overall_est > total_elapsed:
        parts.append(build_total_line(total_elapsed, overall_est))
    return " | ".join(parts)


def build_compact_done_line(
    src_duration: float | None,
    keep_duration: float,
    pipeline_seconds: float,
) -> str:
    """One-line post-run summary shown under the log: durations +
    compression percent + wall-clock, e.g.
    ``Done: 42:10 → 12:30 (-70%) in 8:15``.

    The compression percent is omitted when the source duration is
    unknown (failed probe) or non-positive.
    """
    src_s = fmt_clock_time(src_duration)
    keep_s = fmt_clock_time(keep_duration)
    if src_duration is not None and src_duration > 0:
        pct = max(0, min(100, round(100 * (1 - keep_duration / src_duration))))
        return f"Done: {src_s} → {keep_s} (-{pct}%) in {fmt_time(pipeline_seconds)}"
    return f"Done: {src_s} → {keep_s} in {fmt_time(pipeline_seconds)}"


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


# Prefix the pipeline emits on every status update to mark the current
# stage ("Step 2/4: Detecting silence..."). The GUI's merged status line
# renders its own phase indicator, so the prefix is stripped to avoid
# duplication; the log lines keep it for grep-ability in the transcript.
_STEP_PREFIX_RE = re.compile(r"^Step \d+/\d+:\s*")


def strip_status_step_prefix(text: str) -> str:
    """Remove the leading ``"Step N/4: "`` marker from a status line.

    ``PipelineController._set_status`` prefixes every status update with
    the current phase index. ``ProgressUiMixin._ui_status`` needs that
    prefix to detect phase switches (ETA reset + indicator), but the
    status line renders its own merged phase indicator, so repeating the
    marker would duplicate it. Pure: given ``"Step 2/4: Silence... 45%"``
    returns ``"Silence... 45%"``; lines without the marker are returned
    unchanged.
    """
    return _STEP_PREFIX_RE.sub("", text, count=1)


# Leading verb/phrase the pipeline's per-phase status details start with
# ("Silence... 45%", "Cutting... 55%", "Concatenating... 12%",
# "Downloading 42% ..."). The merged status line already carries the
# short phase name ("Phase 2/4 · Silence (35%)"), so the detail must
# drop its own leading verb to avoid "Silence ... Silence". Longest
# first so "Downloading" wins over "Download" etc.
_PHASE_STATUS_VERBS = (
    "Detecting silence",
    "Downloading",
    "Concatenating",
    "Silence",
    "Cutting",
    "Resolving input",
    "Download",
    "Local file",
)


def strip_phase_status_verb(text: str) -> str:
    """Remove the leading phase-name verb from a per-phase status detail.

    Pure: given ``"Silence... 45% (12s/20s)"`` returns ``"45% (12s/20s)"``,
    ``"Silence (cached)"`` → ``"(cached)"``, ``"Detecting silence..."`` →
    ``""``. Text that doesn't start with a known verb is returned
    unchanged (e.g. ``"Complete! (23m 5s)"``).
    """
    stripped = text.strip()
    for verb in _PHASE_STATUS_VERBS:
        if stripped.startswith(verb):
            tail = stripped[len(verb) :].lstrip(".").strip()
            return tail
    return stripped


# Short phase names keyed by the "Step X/4" token the pipeline emits in
# its status lines (see PipelineController._run_*_phase). Used by the
# GUI's merged status line to render "Step 2/4 · Silence (35%)" instead
# of a bare number.
PHASE_LABELS: dict[str, str] = {
    "1": "Download",
    "2": "Silence",
    "3": "Cutting",
    "4": "Concat",
}


def phase_weight_percent(
    bounds: tuple[float, float, float, float], step: str
) -> int | None:
    """Share (percent) the phase ``step`` ("1".."4") occupies of the
    whole pipeline bar, from the plan's boundary fractions
    ``(download_end, silence_end, cut_end, concat_end)``.

    E.g. ``(0.05, 0.40, 0.94, 1.0)`` → download 5, silence 35, cutting
    54, concat 6. Returns None for an unknown step.
    """
    if step not in PHASE_LABELS or len(bounds) != 4:
        return None
    starts = (0.0, bounds[0], bounds[1], bounds[2])
    idx = int(step) - 1
    span = max(0.0, bounds[idx] - starts[idx])
    return round(span * 100)


def build_phase_line(step: str | None, weight_pct: int | None = None) -> str:
    """Render the phase-indicator prefix of the status line.

    ``None`` step (no phase running yet) yields an empty string. A known
    weight appends the phase's share of the bar, e.g.
    ``"Step 2/4 · Silence (35%)"``. The concrete phase detail
    (e.g. ``"45% (12s/20s)"``) is appended by
    :func:`build_phase_status_line`.
    """
    if step is None or step not in PHASE_LABELS:
        return ""
    name = PHASE_LABELS[step]
    if weight_pct is not None:
        return f"Step {step}/4 · {name} ({weight_pct}%)"
    return f"Step {step}/4 · {name}"


def build_phase_status_line(
    step: str | None,
    weight_pct: int | None,
    detail: str,
) -> str:
    """Merge the phase indicator and its status detail into one line.

    ``ProgressUiMixin._ui_status`` renders stage + detail as a single
    status line (there is no separate phase-indicator label), e.g.
    ``"Step 2/4 · Silence (35%) · 45% (12s/20s)"``. ``detail`` is the
    ``strip_phase_status_verb``-stripped tail so the phase name isn't
    repeated. Returns ``detail`` alone when there's no phase prefix yet.
    """
    base = build_phase_line(step, weight_pct)
    if not base:
        return detail
    if not detail:
        return base
    return f"{base} · {detail}"


def build_pct_pair(phase_pct: float, overall_pct: float) -> str:
    """Compact "phase % · overall %" label for the bar's percent readout.

    The first number is the progress WITHIN the current phase, the
    second the whole-pipeline progress — e.g. ``"63% · 42%"``. Pure so
    the GUI's worker-thread dispatchers can unit-test the formatting.
    """
    return f"{phase_pct:.0f}% · {overall_pct:.0f}%"
