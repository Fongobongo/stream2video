"""Channel/playlist import: resolve a multi-entry listing (Twitch
channel, YouTube channel or YouTube playlist) via yt-dlp's flat-playlist
mode, then drive the existing single-video pipeline over each entry.

The single-video ``download()`` contract (one ``after_move:filepath``)
cannot consume a listing URL: yt-dlp would resolve the playlist and
either grab the first entry or die on the output template. This module
splits the job in two:

1. ``resolve_channel_vods`` — a *listing* pass: yt-dlp with
   ``--flat-playlist`` and ``--playlist-items 1:N`` fetches only the
   entries' metadata (id / title / duration / views, no media), which is
   fast (seconds for dozens of entries) and cheap on bandwidth.
2. The caller (the CLI) iterates the returned list and hands each entry
   to the existing ``PipelineController`` unchanged, so every per-video
   behaviour (project dirs, silence cache, resume, frame-hole gates)
   applies as-is.

Supported listings:
- Twitch channels — ``https://(www|m).twitch.tv/<channel>/videos``
  with the ``?filter=`` tabs (archives / highlights / uploads / all)
  and the separate ``/<channel>/clips`` path.
- YouTube channels — ``/@handle/videos``, ``/channel/<id>/videos``,
  ``/c/<name>/videos``, ``/user/<name>/videos`` and the
  ``/shorts`` / ``/streams`` tabs.
- YouTube playlists — ``/playlist?list=<id>`` (and a bare
  ``watch?v=...&list=...`` link lists that playlist).

Cancel semantics mirror ``download``: a drain thread watches
``cancel_callback`` and kills the spawned yt-dlp; the caller sees
``ChannelImportCancelled``.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from stream2video.utils import popen_with_retry, subprocess_kwargs

logger = logging.getLogger(__name__)

# A Twitch channel URL in VOD-listing form:
# ``https://(www|m|<locale>).twitch.tv/<channel>/videos[?filter=...]``.
#
# The channel-name charset is Twitch's ([A-Za-z0-9_]); length bounds are
# deliberately loose (historical names run 3-25 chars) — a name this
# module "accepts" is only used for a listing call that yt-dlp validates
# anyway, so the pattern gates shape, not account existence. The
# subdomain allows www / m / two-letter locales (de, ru) and their
# three-letter forms (e.g. ``www`` itself); ``m`` is the single-letter
# mobile host.
_CHANNEL_RE = re.compile(
    r"^https?://(?:[a-z]{1,3}\.)?twitch\.tv/([A-Za-z0-9_]{2,25})/videos(?:[/?#]|$)",
    re.IGNORECASE,
)

# YouTube channel-tab URLs. Four channel-address forms are live today
# (@handle, /channel/UC…, legacy /c/ and /user/); the trailing segment
# selects the tab (videos / shorts / streams). The tab is OPTIONAL in
# the pattern — a bare ``/@handle`` is the channel home page whose
# default tab IS videos, and the user pasting that means "the channel's
# videos" exactly like ``/@handle/videos`` does.
_YT_CHANNEL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?youtube\.com/"
    r"(@[A-Za-z0-9_.-]{3,30}|channel/UC[A-Za-z0-9_-]{10,40}"
    r"|c/[A-Za-z0-9_.-]{1,40}|user/[A-Za-z0-9_.-]{1,40})"
    r"(?:/(videos|shorts|streams))?(?:[/?#]|$)",
    re.IGNORECASE,
)

# YouTube playlist URLs: the dedicated ``/playlist?list=…`` page and a
# bare ``watch?v=…&list=…`` link (the user copied it from the playlist
# UI — listing that playlist is the natural meaning).
_YT_PLAYLIST_RE = re.compile(
    r"^https?://(?:www\.|m\.)?youtube\.com/(?:playlist\?list=|watch\?.*?[?&]list=)"
    r"([A-Za-z0-9_-]{10,60})",
    re.IGNORECASE,
)

# Listing timeout (seconds). The flat-playlist index for a channel with
# hundreds of VODs arrives in a few seconds once connected; slow links
# and API hiccups can stretch that, but a listing that takes longer
# than this is effectively wedged and should be retried, not waited
# on. Media downloads use the (much larger) download_timeout.
_CHANNEL_LIST_TIMEOUT = 300

# Listing categories — the platform's own tabs, keyed per site. Twitch's
# channel page exposes archives / highlights / uploads / all via the
# ``?filter=`` query plus the separate /clips path; YouTube channels
# have videos / shorts / streams tabs (and ``videos`` doubles as the
# ``all`` default — the Videos tab IS the main listing). The CLI accepts
# the union and rejects a value that doesn't apply to the input's
# platform with the valid list in the error.
TWITCH_CHANNEL_TYPES = ("archives", "highlights", "uploads", "all", "clips")
YOUTUBE_CHANNEL_TYPES = ("videos", "shorts", "streams")
CHANNEL_TYPES = TWITCH_CHANNEL_TYPES + YOUTUBE_CHANNEL_TYPES

# Sort keys for the listing table. ``date`` is the newest-first default;
# Twitch ids are sequential (a higher id is a newer recording) while
# YouTube's flat listing carries a real ``timestamp``, which
# ``sort_channel_vods`` prefers when present and falls back to the id
# heuristic otherwise.
CHANNEL_SORTS = ("date", "duration", "views")

# How many entries the listing fetches when the user picks interactively.
# A larger window gives the table more to filter/sort through; the flat
# listing is cheap (metadata only), so a generous default keeps the
# picker useful on channels the user doesn't know yet.
_CHANNEL_PICK_WINDOW = 50


class ChannelImportError(Exception):
    """Channel listing failed (unreachable, private, or no VODs)."""


class ChannelImportCancelled(ChannelImportError):
    """The listing was cancelled via ``cancel_callback``."""

    def __init__(self, message: str = "Channel import cancelled") -> None:
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class ChannelVod:
    """One entry in a listing (no media fetched)."""

    video_id: str
    url: str
    title: str | None
    duration: float | None
    view_count: int | None = None
    # Unix upload timestamp when the listing provides one (YouTube's
    # flat listing does; Twitch's does not). Optional so the Twitch
    # shape stays constructible without it.
    timestamp: int | None = None

    def duration_hm(self) -> str:
        """``3569.0 -> "59m29s"`` / ``"45m"`` — for the picker table."""
        if self.duration is None:
            return "?"
        total = round(self.duration)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m"
        return f"{m}m{s:02d}s" if s else f"{m}m"

    def date_label(self) -> str:
        """Short local date for the picker table (``2026-08-14``), or
        ``?`` when the listing carried no timestamp."""
        if self.timestamp is None:
            return "?"
        import datetime

        return datetime.datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d")


def parse_channel_selection(spec: str, max_index: int) -> list[int]:
    """Parse a 1-based entry selection like ``"1,3-5,9"``.

    The interactive picker (and ``--channel-select``) show a numbered
    table; the user checks entries by number. Ranges are inclusive,
    whitespace is free-form, and duplicates collapse while preserving
    the table's order (a range's numbers expand in ascending order, so
    ``"5,1-2"`` selects 1, 2, 5 — the TABLE order, not the typing
    order). Pure and raising on anything ambiguous.

    Raises:
        ValueError: an empty spec, a bad token, or an out-of-range
            number (the error names the valid range, so a paste of the
            table's numbers that lost a digit fails fast instead of
            silently dropping an entry).
    """
    spec = spec.strip()
    if not spec:
        raise ValueError("Empty selection")
    picked: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo_raw, _, hi_raw = token.partition("-")
            try:
                lo, hi = int(lo_raw), int(hi_raw)
            except ValueError as e:
                raise ValueError(f"Bad range {token!r}") from e
            if lo > hi:
                lo, hi = hi, lo
        else:
            try:
                lo = hi = int(token)
            except ValueError as e:
                raise ValueError(f"Bad number {token!r}") from e
        for n in range(lo, hi + 1):
            if n < 1 or n > max_index:
                raise ValueError(f"Number {n} is outside the table (1..{max_index})")
            picked.add(n)
    return sorted(picked)


def sort_channel_vods(vods: list[ChannelVod], key: str) -> list[ChannelVod]:
    """Sort a listing by ``key``: ``date`` (newest first), ``duration``
    (longest first) or ``views`` (most-watched first).

    Pure: returns a new list; entries with a missing value (``?`` in
    the table) sink to the end regardless of key rather than crashing
    the sort. ``date`` prefers a real upload ``timestamp`` when the
    listing provides one; otherwise it applies the sequential-id
    heuristic for Twitch (ids are sequential numbers — a higher id is a
    newer recording) while non-numeric ids (YouTube's random ids keep
    the platform's own newest-first listing order, a stable no-op).
    """
    if key not in CHANNEL_SORTS:
        raise ValueError(f"Unknown sort {key!r} (expected one of {CHANNEL_SORTS})")

    if key == "date":
        # Entries WITH a timestamp sort by it; timestamp-less entries
        # sink last.
        if any(v.timestamp is not None for v in vods):
            return sorted(
                vods,
                key=lambda v: (
                    v.timestamp is None,
                    -(v.timestamp if v.timestamp is not None else 0),
                ),
            )

        # Twitch: ids are sequential numbers (``v246974233`` / bare
        # ``246974233`` / clip ``577522052``) — a higher id is a newer
        # recording, so sort by it. Anything else (YouTube's random
        # 11-char ids — ``1icdW32gS8A``, ``UbAsuvO-164`` — contain
        # digits too, and digit-mining them scrambles the order) keeps
        # the listing's own newest-first order via a stable no-op.
        def _twitch_seq(v: ChannelVod) -> float:
            bare = v.video_id.removeprefix("v")
            return float(bare) if bare.isdigit() else float("inf")

        return sorted(vods, key=_twitch_seq, reverse=True)
    if key == "duration":
        return sorted(vods, key=lambda v: (v.duration is None, -(v.duration or 0.0)))
    # views
    return sorted(vods, key=lambda v: (v.view_count is None, -(v.view_count or 0)))


def parse_channel_filter(spec: str) -> tuple[list[str], list[str]]:
    """Parse a ``--channel-filter`` spec into (include, exclude) globs.

    Grammar (comma-separated terms, matched against the entry TITLE,
    case-insensitively):

    - ``!pattern`` — exclusion: drop matching entries.
    - ``+pattern`` or bare ``pattern`` — inclusion: keep entries
      matching at least one include (an explicit ``+`` is legal for
      readability in mixed specs like ``+*bob*,!*archive*``).
    - ``*`` matches any run of characters, ``?`` a single character;
      everything else is literal (Python ``fnmatch`` semantics).

    A spec with ONLY exclusions (``!a,!b``) implicitly includes
    everything else; a spec with both sides keeps entries that match an
    include and no exclude. Whitespace around terms is free-form.

    Raises:
        ValueError: a term with nothing after its ``!``/``+`` prefix —
            ``!`` alone would silently drop nothing and ``+`` alone
            would silently keep nothing; both are almost certainly
            typos (a dropped ``*``), so fail fast with the term named.
    """
    includes: list[str] = []
    excludes: list[str] = []
    for term in spec.split(","):
        term = term.strip()
        if not term:
            continue
        if term.startswith("!"):
            pattern = term[1:].strip()
            if not pattern:
                raise ValueError(f"Empty exclusion pattern in {term!r}")
            excludes.append(pattern)
        elif term.startswith("+"):
            pattern = term[1:].strip()
            if not pattern:
                raise ValueError(f"Empty inclusion pattern in {term!r}")
            includes.append(pattern)
        else:
            includes.append(term)
    if not includes and not excludes:
        raise ValueError("Empty filter")
    return includes, excludes


def filter_channel_vods(vods: list[ChannelVod], spec: str) -> list[ChannelVod]:
    """Filter a listing by title globs (see ``parse_channel_filter``).

    Pure: returns a new list preserving the input order. Entries whose
    title is missing (``?`` in the table) never match an include and
    are never excluded — a title-less entry can't be judged by its
    title, and a bare ``*`` include means "everything titled", so a
    lone ``!*something*`` spec keeps them (that's the "only exclusions"
    case: the entry is not excluded, hence kept).

    Raises:
        ValueError: the spec doesn't parse (propagated from
            ``parse_channel_filter`` with the term named).
    """
    includes, excludes = parse_channel_filter(spec)

    def _match(title: str | None, patterns: list[str]) -> bool:
        if title is None:
            return False
        low = title.lower()
        return any(fnmatch.fnmatch(low, p.lower()) for p in patterns)

    if includes:
        kept = [v for v in vods if _match(v.title, includes)]
    else:
        # Only exclusions: everything not explicitly dropped is kept,
        # including title-less entries.
        kept = [v for v in vods if not _match(v.title, excludes)]
    if excludes:
        kept = [v for v in kept if not _match(v.title, excludes)]
    return kept


def is_twitch_channel_url(url: str) -> bool:
    """True when ``url`` is a Twitch *channel VOD-listing* URL.

    Only the listing form matches (``/<channel>/videos``): a single-VOD
    URL (``/videos/<id>``) fails the pattern — the segment after
    ``/videos`` is not a channel name — and stays on the ordinary
    single-video path. Pure and side-effect free.
    """
    return _CHANNEL_RE.match(url.strip()) is not None


def is_youtube_channel_url(url: str) -> bool:
    """True when ``url`` addresses a YouTube channel (any of the four
    live address forms, with an optional videos/shorts/streams tab).

    A bare watch URL (``/watch?v=…``) does not match; a playlist URL
    does not either (``is_youtube_playlist_url`` covers it). The tab is
    optional because the channel's home page IS the videos tab.
    """
    return _YT_CHANNEL_RE.match(url.strip()) is not None


def is_youtube_playlist_url(url: str) -> bool:
    """True when ``url`` points at a YouTube playlist — either the
    dedicated ``/playlist?list=…`` page or a ``watch?v=…&list=…`` link
    (copied from the playlist UI; listing that playlist is the natural
    meaning of such a paste)."""
    return _YT_PLAYLIST_RE.match(url.strip()) is not None


def is_listing_url(url: str) -> bool:
    """True when ``url`` is ANY multi-entry listing this module can
    resolve (Twitch channel, YouTube channel or YouTube playlist)."""
    return is_twitch_channel_url(url) or is_youtube_channel_url(url) or is_youtube_playlist_url(url)


def _canonical_vod_url(video_id: str, *, platform: str) -> str:
    """Watch URL for a listed entry id.

    Twitch: yt-dlp's listing prints ids with a ``v`` prefix and the
    canonical watch URL uses the bare numeric id (both resolve in
    yt-dlp, but the bare form keeps project-dir identity — the id
    namespaces the cache — identical to a pasted watch URL).
    YouTube: the plain ``watch?v=<id>`` form.
    """
    if platform == "youtube":
        return f"https://www.youtube.com/watch?v={video_id}"
    return f"https://www.twitch.tv/videos/{video_id.removeprefix('v')}"


def _default_category_for(url: str) -> str:
    """The platform's default tab for ``url``: ``archives`` for Twitch,
    ``videos`` for YouTube (channels and playlists alike)."""
    if is_twitch_channel_url(url):
        return "archives"
    return "videos"


def _resolve_listing_url(url: str, category: str) -> tuple[str, str]:
    """Map the input URL + category to the concrete listing URL and the
    entry-id namespace (``twitch`` / ``youtube``) used for canonical
    watch URLs.

    Twitch: ``archives`` is the bare listing (``?filter=archives`` is NOT
    a value the channel page accepts — it returns an empty listing,
    verified empirically), ``highlights`` / ``uploads`` / ``all`` are
    ``?filter=`` values, and ``clips`` re-routes to ``/<channel>/clips``.
    YouTube: the category IS the tab (``videos`` / ``shorts`` /
    ``streams``) appended to whichever of the four channel-address forms
    the URL used; a URL that already carries a tab keeps it unless the
    user explicitly passes a different ``--channel-type``. Playlists
    have no tabs — the playlist URL is the listing, and the category
    must be left at the default.
    """
    u = url.strip()

    m = _CHANNEL_RE.match(u)
    if m is not None:
        if category not in TWITCH_CHANNEL_TYPES:
            raise ChannelImportError(
                f"Twitch channels accept --channel-type {TWITCH_CHANNEL_TYPES}, not {category!r}"
            )
        channel = m.group(1)
        if category == "clips":
            return f"https://www.twitch.tv/{channel}/clips", "twitch"
        if category == "archives":
            return f"https://www.twitch.tv/{channel}/videos", "twitch"
        return f"https://www.twitch.tv/{channel}/videos?filter={category}", "twitch"

    pm = _YT_PLAYLIST_RE.match(u)
    if pm is not None:
        if category != "videos":
            # Playlists have no tabs; the CLI validates this before the
            # call, so reaching here is a programming error.
            raise ChannelImportError("Playlists have no tabs; leave --channel-type at the default")
        return f"https://www.youtube.com/playlist?list={pm.group(1)}", "youtube"

    cm = _YT_CHANNEL_RE.match(u)
    if cm is not None:
        if category not in YOUTUBE_CHANNEL_TYPES:
            raise ChannelImportError(
                f"YouTube channels accept --channel-type {YOUTUBE_CHANNEL_TYPES}, not {category!r}"
            )
        return f"https://www.youtube.com/{cm.group(1)}/{category}", "youtube"

    raise ChannelImportError(f"Not a supported listing URL: {url}")


def resolve_channel_vods(
    url: str,
    limit: int,
    *,
    category: str = "",
    cancel_callback: Callable[[], bool] | None = None,
    proxy: str = "",
    timeout: int = _CHANNEL_LIST_TIMEOUT,
    low_process_priority: bool = False,
) -> list[ChannelVod]:
    """List the ``limit`` most recent entries of a channel or playlist
    via yt-dlp.

    Runs yt-dlp with ``--flat-playlist`` (metadata only — no media is
    downloaded) and ``--playlist-items 1:{limit}`` so the listing pass
    stops after the requested window even on channels with hundreds of
    entries. Returns entries newest-first (the platform's own listing
    order), which matches what a user sees on the channel page.

    Args:
        url: A Twitch channel VOD-listing URL, a YouTube channel URL
            (any address form, with or without a tab segment) or a
            YouTube playlist URL (``/playlist?list=…`` or a
            ``watch?v=…&list=…`` link).
        limit: Maximum number of entries to return; must be >= 1.
        category: Which tab to list. Empty string (default) picks the
            platform default: ``archives`` for Twitch,
            ``videos`` for YouTube channels and playlists. Explicit
            values: Twitch ``archives`` / ``highlights`` / ``uploads``
            / ``all`` / ``clips``; YouTube ``videos`` / ``shorts`` /
            ``streams``. Playlists have no tabs — any explicit
            category other than ``videos`` is rejected.
        cancel_callback: Optional callable polled by the stdout drain
            thread; returning True kills yt-dlp and raises
            ``ChannelImportCancelled``.
        proxy: Optional proxy forwarded to yt-dlp (same value the media
            downloads will use).
        timeout: Wall-clock ceiling for the whole listing in seconds.
        low_process_priority: Spawn yt-dlp at BELOW_NORMAL (Windows) /
            nice 10 (POSIX), matching the downloader's policy.

    Returns:
        Up to ``limit`` ``ChannelVod`` entries, newest first.

    Raises:
        ChannelImportCancelled: ``cancel_callback`` fired.
        ChannelImportError: listing failed (bad channel, network error,
            or a timeout).
    """
    if limit < 1:
        raise ChannelImportError(f"Channel limit must be >= 1, got {limit}")
    # Empty category = the platform's default tab. The platform is
    # decided by the URL, so the default can only be computed after
    # matching; resolved here and passed down as a concrete value.
    if category not in CHANNEL_TYPES and category != "":
        raise ChannelImportError(
            f"Unknown channel type {category!r} (expected one of {CHANNEL_TYPES})"
        )
    _effective_category = category or _default_category_for(url)

    listing_url, platform = _resolve_listing_url(url, _effective_category)

    # One line per entry: ``id::duration::views::timestamp::title``.
    # ``::`` cannot appear in the id (alphanumeric), the duration
    # (float/NA), the view count (int/NA) or the timestamp (int/NA),
    # and the title goes LAST so a ``::`` inside a title stays in the
    # title field (split with maxsplit keeps the remainder intact).
    # Twitch's flat listing reports NA for the timestamp (its GraphQL
    # listing has no created_at); YouTube's provides it, which powers
    # the honest ``date`` sort and the table's date column.
    line_template = "%(id)s::%(duration)s::%(view_count)s::%(timestamp)s::%(title)s"
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-warnings",
        "--flat-playlist",
        "--no-update",
        "--print",
        line_template,
        "--playlist-items",
        f"1:{limit}",
        listing_url,
    ]
    if platform == "youtube":
        # Flat YouTube listings report NA for timestamps (and view
        # counts) by default; ``approximate_date`` makes the tab
        # extractor carry an upload-date-derived timestamp per entry
        # (verified live: @NASA's listing yields real per-video dates
        # with it, NA without). Powers the honest ``date`` sort and the
        # table's date column at zero extra requests — the page already
        # carries the data. Views stay NA either way (that genuinely
        # needs a per-video extraction, ~1-2 s each — not worth it for
        # a picker table).
        cmd.extend(["--extractor-args", "youtubetab:approximate_date"])
    if proxy:
        cmd.extend(["--proxy", proxy])

    logger.info(f"Listing channel VODs: {url} (limit {limit})")
    try:
        process = popen_with_retry(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            # yt-dlp reconfigures its own stdout to UTF-8 (see
            # download.py for the mojibake this avoids); match it here so
            # non-ASCII titles decode correctly on cp1251 consoles.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **subprocess_kwargs(low_process_priority, 0),
        )
    except FileNotFoundError as e:
        raise ChannelImportError("yt-dlp not found (install via 'pip install yt-dlp')") from e
    except OSError as e:
        raise ChannelImportError(f"Could not start yt-dlp: {e}") from e

    lines: list[str] = []
    cancelled = threading.Event()

    def _drain() -> None:
        # Stream line-by-line: a channel with hundreds of entries must
        # not buffer the whole listing before the cancel check runs.
        assert process.stdout is not None
        for line in process.stdout:
            if cancel_callback is not None and cancel_callback():
                cancelled.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            stripped = line.strip()
            if stripped:
                lines.append(stripped)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    try:
        while True:
            # Cancel is polled HERE as well as in the drain thread: a
            # listing child that has not printed a line yet (slow start,
            # auth redirect) leaves the drain blocked in its readline
            # and its cancel check unreachable — without this check a
            # cancel took as long as the child's own runtime.
            if cancel_callback is not None and cancel_callback():
                cancelled.set()
                try:
                    process.kill()
                except OSError:
                    pass
                raise ChannelImportCancelled()
            try:
                process.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    raise ChannelImportError(
                        f"Channel listing timed out after {timeout}s"
                    ) from None
        reader.join(timeout=5)
    finally:
        if process.poll() is None:  # pragma: no cover - defensive
            try:
                process.kill()
            except OSError:
                pass

    if cancelled.is_set():
        raise ChannelImportCancelled()

    returncode = process.returncode
    if returncode != 0:
        stderr = process.stderr.read() if process.stderr is not None else ""
        snippet = " ".join(stderr.split())[:300]
        raise ChannelImportError(
            f"Channel listing failed (yt-dlp exit {returncode}): {snippet or url}"
        )

    vods: list[ChannelVod] = []
    for raw in lines:
        parts = raw.split("::", 4)
        if len(parts) != 5:
            # Unknown line shape (a warning that slipped through, an ad
            # marker): skip rather than abort the whole import.
            logger.debug(f"Skipping unrecognized listing line: {raw!r}")
            continue
        video_id, duration_raw, views_raw, ts_raw, title = parts
        if not video_id:
            continue
        duration: float | None
        try:
            duration = float(duration_raw)
        except ValueError:
            # Live/upcoming entries report no duration; import them
            # anyway and let the media download surface the truth.
            duration = None
        view_count: int | None
        try:
            view_count = int(float(views_raw))
        except ValueError:
            view_count = None
        timestamp: int | None
        try:
            timestamp = int(float(ts_raw))
        except ValueError:
            timestamp = None
        vods.append(
            ChannelVod(
                video_id=video_id,
                url=_canonical_vod_url(video_id, platform=platform),
                title=title or None,
                duration=duration,
                view_count=view_count,
                timestamp=timestamp,
            )
        )

    if not vods:
        raise ChannelImportError(
            f"No entries found for {url} in category {category!r} "
            "(the channel/playlist may be empty, private, or region-blocked)"
        )

    logger.info(f"Channel listing: {len(vods)} {category} entry(s), newest {vods[0].url}")
    return vods


def _module_smoke() -> None:  # pragma: no cover - manual probe helper
    """``python -m stream2video.channel <url> <limit> [category]`` — quick
    manual check of the listing path outside the CLI (used during
    development; the CLI embeds the same resolver)."""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("limit", type=int)
    ap.add_argument("category", nargs="?", default="archives")
    ns = ap.parse_args()
    for v in resolve_channel_vods(ns.url, ns.limit, category=ns.category):
        dur = v.duration_hm() if v.duration is not None else "?"
        views = str(v.view_count) if v.view_count is not None else "?"
        date = v.date_label()
        print(f"{v.video_id}  {date}  {dur:>8}  {views:>8}  {v.title or '-'}  {v.url}")


if __name__ == "__main__":  # pragma: no cover
    _module_smoke()
