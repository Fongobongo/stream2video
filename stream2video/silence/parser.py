"""silencedetect output parsing: lexer grammar + state machine + margin.

This module owns the data model (``SilenceSegment``), the regex grammar
for ffmpeg's ``silencedetect`` filter output, the unified
``SilenceParser`` state machine used by both the batch and progressive
paths, and the margin/merge post-processing. It has no I/O of its own —
everything it operates on is text supplied by the caller.
"""

import logging
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)


class SilenceDetectionError(Exception):
    """Base silence detection error."""


class SilenceOutOfMemoryError(SilenceDetectionError):
    """ffmpeg was killed by the OOM killer / self-aborted on alloc.

    Distinct subclass so the CLI / GUI can hint the user to lower the
    memory budget or pick the Low-memory preset (see FFmpegOutOfMemoryError
    in concat.py for the detection heuristic).
    """


class SilenceCancelledError(SilenceDetectionError):
    """Silence detection was cancelled by user (not a real failure)."""


class SilenceSegment:
    """Silence segment representation."""

    def __init__(self, start: float, end: float):
        self.start = start
        self.end = end
        self.duration = max(0.0, end - start)

    def __repr__(self) -> str:
        return f"SilenceSegment({self.start:.2f}s - {self.end:.2f}s, duration={self.duration:.2f}s)"


_SILENCE_TIMEOUT = 36000
_SEGMENT_MATCH_TOLERANCE = 0.05
_SAMPLE_VERIFY_DURATION = 60.0
# Resume cache throttling. We save at most every 30 seconds OR every
# 100 new segments, whichever fires first. This keeps the per-segment
# overhead negligible while still letting a cancelled run pick up from
# a useful checkpoint.
_RESUME_THROTTLE_S = 30.0
_RESUME_THROTTLE_N = 100


def _noop_on_segment(_segments: list[SilenceSegment]) -> None:
    """Default no-op callback used when `initial_segments` is set but
    the caller didn't supply `on_segment` — see `_run_silencedetect`."""


# ffmpeg's silencedetect can emit negative starts on sources with a
# negative initial PTS (edit lists / -itsoffset captures):
# ``silence_start: -0.021906``. Without the leading ``-?`` the leading
# silence is silently dropped (the later silence_end has no pending
# start). Negative values are clamped to 0 by apply_margin / the caller.
_NUM = r"-?\d+(?:[.,]\d+)?"
_SILENCE_START_RE = re.compile(rf"silence_start:\s*({_NUM})")
_SILENCE_END_RE = re.compile(rf"silence_end:\s*({_NUM})")


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


class SilenceParser:
    """Unified state machine for parsing ffmpeg ``silencedetect`` output.

    The fix plan (P2.5) flagged three divergent parsers:
      1. ``_parse_ffmpeg_output`` — batch parser, walks the full stderr
         after ffmpeg exits.
      2. ``_run_silencedetect``'s ``_on_line`` closure — progressive
         parser, fires a callback on each new segment.
      3. ``detect_silence_stream``'s inline loop — progressive parser
         for the preview path.

    The decimal-comma bug (P1.13) arose precisely because parsers 2
    and 3 used ``float()`` instead of ``_to_float()``. This class
    unifies all three paths so a future change to the parsing logic
    (e.g. a new ``silence_duration`` field) only needs to be made in
    one place.

    Usage:
        parser = SilenceParser(on_segment=callback)
        for line in stderr_lines:
            parser.feed(line)
        segments = parser.finalize(duration=media_duration)

    ``on_segment`` (optional) is invoked with a snapshot of the running
    segment list every time a new ``silence_end`` arrives. The callback
    runs on the caller's thread; callers touching a UI must dispatch
    to the main thread themselves.

    ``finalize(duration=...)`` closes a trailing ``silence_start``
    without a matching ``silence_end`` (P1.12). When ``duration`` is
    None the trailing start is dropped with a warning (preview path
    that doesn't know the media duration).
    """

    def __init__(
        self,
        on_segment: Callable[[list[SilenceSegment]], None] | None = None,
    ) -> None:
        self._segments: list[SilenceSegment] = []
        self._pending_start: float | None = None
        self._on_segment = on_segment

    @property
    def segments(self) -> list[SilenceSegment]:
        """Snapshot of the segments detected so far (raw, pre-margin)."""
        return list(self._segments)

    @property
    def pending_start(self) -> float | None:
        """The timestamp of the unmatched ``silence_start``, if any."""
        return self._pending_start

    def feed(self, line: str) -> None:
        """Feed one decoded stderr line to the parser.

        Matches ``silence_start`` and ``silence_end`` patterns; on a
        matching ``silence_end`` with a pending start, appends the
        segment and fires the ``on_segment`` callback (if set).
        """
        m_s = _SILENCE_START_RE.search(line)
        if m_s:
            self._pending_start = _to_float(m_s.group(1))
            return
        m_e = _SILENCE_END_RE.search(line)
        if m_e and self._pending_start is not None:
            self._segments.append(SilenceSegment(self._pending_start, _to_float(m_e.group(1))))
            self._pending_start = None
            if self._on_segment is not None:
                self._on_segment(list(self._segments))

    def finalize(self, duration: float | None = None) -> list[SilenceSegment]:
        """Close out parsing and return the final segment list.

        ``duration``: when known, a trailing ``silence_start`` without a
        matching ``silence_end`` (input ended while still silent) is
        closed at the media duration (P1.12). When None, the pending
        start is dropped with a warning.
        """
        if self._pending_start is not None:
            if duration is not None and duration > 0:
                clamped_start = min(self._pending_start, duration)
                if clamped_start < duration:
                    logger.info(
                        f"Trailing silence_start at t={self._pending_start:.3f}s "
                        f"had no matching silence_end; closing at media "
                        f"duration {duration:.3f}s"
                    )
                    self._segments.append(SilenceSegment(clamped_start, duration))
                    if self._on_segment is not None:
                        self._on_segment(list(self._segments))
                else:
                    logger.debug(
                        f"Trailing silence_start at t={self._pending_start:.3f}s "
                        f"is at/after duration {duration:.3f}s; dropping"
                    )
            else:
                logger.warning(
                    "Unmatched silence_start (no silence_end) and no "
                    "media duration available; dropped — ffmpeg output "
                    "may be truncated"
                )
        return list(self._segments)


def _parse_ffmpeg_output(stderr: str, duration: float | None = None) -> list[SilenceSegment]:
    """Parse ffmpeg silencedetect output (batch path).

    Delegates to :class:`SilenceParser` so the parsing logic lives in
    exactly one place — the previous standalone ``zip(starts, ends)``
    implementation diverged from the progressive path on the
    decimal-comma handling (P1.13) before P2.5 unified them.

    ``duration``: when known (media duration in seconds, already
    ffprobe'd by the caller), a trailing ``silence_start`` without a
    matching ``silence_end`` is closed at that duration — the batch
    path then produces the same cut plan as the progressive path
    (P1: CLI/GUI parity). When None the trailing start is dropped with
    a warning (historical behaviour for callers that don't know the
    duration, e.g. the waveform preview).
    """
    parser = SilenceParser()
    for line in stderr.splitlines():
        parser.feed(line)
    return parser.finalize(duration=duration)


def apply_margin(
    segments: list[SilenceSegment], margin: float, duration: float | None = None
) -> list[SilenceSegment]:
    """Apply margin and merge overlapping segments.

    Positive margin shrinks silence (keep more audio around phrases).
    Negative margin expands silence (remove more audio around phrases).

    ``duration`` (optional) is the source media duration in seconds — when
    supplied, ``end`` is clamped to it so a negative margin can't expand a
    silence segment past the end of the media. ``start`` is always clamped to 0.

    Neighbour clamping (P2 audit): a negative margin that would expand
    one silence past the start of the next (eating the loud keep region
    between them and merging the two silences together) is clamped to
    the midpoint of the loud gap. Two expanding silences meet at the
    midpoint instead of overlapping — the loud gap collapses to a
    single shared boundary rather than disappearing entirely (and the
    final merge step therefore never merges the two). Without it,
    ``[(50,60),(66,80)]`` with ``margin=-10`` expanded to
    ``[(50,70),(56,90)]`` then merged into ``[(50,90)]``, eating the
    full 6s of speech between the two silences (the user asked for at
    most 6s total of trimming across both boundaries, not 30s).
    """
    if not segments:
        return segments

    expanded = []
    for i, seg in enumerate(segments):
        start = max(0.0, seg.start + margin)
        end = seg.end - margin
        # Neighbour clamp (P2 audit): a negative margin expands silence
        # into the adjacent loud region. When two raw silences are
        # separated by a loud keep gap (``seg[i].end < seg[i+1].start``)
        # the expansion may overlap the next silence and the merge step
        # would then collapse both silences into one, eating the entire
        # keep gap in between. That contradicts what the detector found
        # (a loud region between two silences) and what the user asked
        # for (drop at most ``|margin|`` seconds at each boundary).
        #
        # Clamp each boundary to the midpoint of the loud gap between
        # adjacent silences — so two expanding silences meet exactly
        # in the middle and the gap collapses to a single shared
        # boundary instead of disappearing entirely. This preserves
        # roughly half of the loud region the detector marked as keep,
        # never letting negative margins eat it whole.
        if i + 1 < len(segments) and seg.end < segments[i + 1].start:
            mid = (seg.end + segments[i + 1].start) / 2.0
            end = min(end, mid)
        if i > 0 and segments[i - 1].end < seg.start:
            mid = (segments[i - 1].end + seg.start) / 2.0
            start = max(start, mid)
        if duration is not None:
            end = min(end, float(duration))
        if start < end:
            expanded.append(SilenceSegment(start, end))

    if not expanded:
        return expanded

    expanded.sort(key=lambda s: s.start)

    merged = []
    current = expanded[0]

    for seg in expanded[1:]:
        # Strict ``<`` (not ``<=``): two silences that meet at exactly
        # one shared boundary point (e.g. ``(40, 63), (63, 90)`` after
        # midpoint clamping) are NOT merged — they're two distinct
        # silences joined at a single time instant. ``<=`` would
        # collapse them into one, hiding the original two-detector
        # boundary and giving the wrong keep-oracle view.
        if seg.start < current.end:
            current = SilenceSegment(current.start, max(current.end, seg.end))
        else:
            merged.append(current)
            current = seg

    merged.append(current)
    return merged
