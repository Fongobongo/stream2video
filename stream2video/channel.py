"""Twitch channel import: resolve a channel's recent VOD list via
yt-dlp's flat-playlist listing, then drive the existing single-VOD
pipeline over each entry.

The single-video ``download()`` contract (one ``after_move:filepath``)
cannot consume a channel URL: yt-dlp would resolve the playlist and
either grab the first entry or die on the output template. This module
splits the job in two:

1. ``resolve_channel_vods`` — a *listing* pass: yt-dlp with
   ``--flat-playlist`` and ``--playlist-items 1:N`` fetches only the
   channel's VOD index (id / title / duration, no media), which is fast
   (seconds for dozens of entries) and cheap on bandwidth.
2. The caller (the CLI) iterates the returned list and hands each VOD
   URL to the existing ``PipelineController`` unchanged, so every
   per-video behaviour (project dirs, silence cache, resume, frame-hole
   gates) applies as-is.

Cancel semantics mirror ``download``: a drain thread watches
``cancel_callback`` and kills the spawned yt-dlp; the caller sees
``ChannelImportCancelled``.
"""

from __future__ import annotations

import dataclasses
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

# Listing timeout (seconds). The flat-playlist index for a channel with
# hundreds of VODs arrives in a few seconds once connected; slow links
# and Twitch API hiccups can stretch that, but a listing that takes
# longer than this is effectively wedged and should be retried, not
# waited on. Media downloads use the (much larger) download_timeout.
_CHANNEL_LIST_TIMEOUT = 300


class ChannelImportError(Exception):
    """Channel listing failed (unreachable, private, or no VODs)."""


class ChannelImportCancelled(ChannelImportError):
    """The listing was cancelled via ``cancel_callback``."""

    def __init__(self, message: str = "Channel import cancelled") -> None:
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class ChannelVod:
    """One VOD in a channel's listing (no media fetched)."""

    video_id: str
    url: str
    title: str | None
    duration: float | None


def is_twitch_channel_url(url: str) -> bool:
    """True when ``url`` is a Twitch *channel VOD-listing* URL.

    Only the listing form matches (``/<channel>/videos``): a single-VOD
    URL (``/videos/<id>``) fails the pattern — the segment after
    ``/videos`` is not a channel name — and stays on the ordinary
    single-video path. Pure and side-effect free.
    """
    return _CHANNEL_RE.match(url.strip()) is not None


def _canonical_vod_url(video_id: str) -> str:
    """Watch URL for a listed VOD id (``v123`` → ``/videos/123``).

    yt-dlp's listing prints ids with a ``v`` prefix; the canonical watch
    URL uses the bare numeric id. Both spellings resolve in yt-dlp, but
    the bare form keeps project-dir identity (the id namespaces the
    cache) identical to what a user gets by pasting the watch URL
    directly — one video, one project dir, both entry points.
    """
    return f"https://www.twitch.tv/videos/{video_id.removeprefix('v')}"


def resolve_channel_vods(
    url: str,
    limit: int,
    *,
    cancel_callback: Callable[[], bool] | None = None,
    proxy: str = "",
    timeout: int = _CHANNEL_LIST_TIMEOUT,
    low_process_priority: bool = False,
) -> list[ChannelVod]:
    """List the channel's ``limit`` most recent VODs via yt-dlp.

    Runs yt-dlp with ``--flat-playlist`` (metadata only — no media is
    downloaded) and ``--playlist-items 1:{limit}`` so the listing pass
    stops after the requested window even on channels with hundreds of
    VODs. Returns entries newest-first (Twitch's listing order), which
    matches what a user sees on the channel page.

    Args:
        url: Channel VOD-listing URL (``.../<channel>/videos``).
        limit: Maximum number of VODs to return; must be >= 1.
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

    # One line per VOD: ``id::duration::title``. ``::`` cannot appear in
    # the id (alphanumeric) or the duration (float/NA), and the title
    # goes LAST so a ``::`` inside a title stays in the title field
    # (split with maxsplit keeps the remainder intact).
    line_template = "%(id)s::%(duration)s::%(title)s"
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
        url,
    ]
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
        parts = raw.split("::", 2)
        if len(parts) != 3:
            # Unknown line shape (a warning that slipped through, an ad
            # marker): skip rather than abort the whole import.
            logger.debug(f"Skipping unrecognized listing line: {raw!r}")
            continue
        video_id, duration_raw, title = parts
        if not video_id:
            continue
        duration: float | None
        try:
            duration = float(duration_raw)
        except ValueError:
            # Live/upcoming entries report no duration; import them
            # anyway and let the media download surface the truth.
            duration = None
        vods.append(
            ChannelVod(
                video_id=video_id,
                url=_canonical_vod_url(video_id),
                title=title or None,
                duration=duration,
            )
        )

    if not vods:
        raise ChannelImportError(
            f"No VODs found for {url} (channel may have no recordings, "
            "or they are subscribers-only)"
        )

    logger.info(f"Channel listing: {len(vods)} VOD(s), newest {vods[0].url}")
    return vods


def _module_smoke() -> None:  # pragma: no cover - manual probe helper
    """``python -m stream2video.channel <url> <limit>`` — quick manual
    check of the listing path outside the CLI (used during development;
    the CLI embeds the same resolver)."""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("limit", type=int)
    ns = ap.parse_args()
    for v in resolve_channel_vods(ns.url, ns.limit):
        dur = f"{v.duration:.0f}s" if v.duration is not None else "?"
        print(f"{v.video_id}  {dur:>8}  {v.title or '-'}  {v.url}")


if __name__ == "__main__":  # pragma: no cover
    _module_smoke()
