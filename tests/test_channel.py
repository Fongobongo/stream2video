"""Tests for the Twitch channel import (stream2video/channel.py).

The listing resolver is covered with a fake yt-dlp: the tests spawn no
network and no real media download, but they DO exercise the real
subprocess plumbing (popen + drain thread + cancel + timeout), matching
the downloader's test style.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from stream2video.channel import (
    ChannelImportCancelled,
    ChannelImportError,
    ChannelVod,
    _canonical_vod_url,
    is_twitch_channel_url,
    resolve_channel_vods,
)

CHANNEL_URL = "https://www.twitch.tv/somechannel/videos"


class TestIsTwitchChannelUrl:
    def test_canonical_listing_url(self):
        assert is_twitch_channel_url(CHANNEL_URL)

    def test_mobile_subdomain(self):
        assert is_twitch_channel_url("https://m.twitch.tv/somechannel/videos")

    def test_localized_subdomain(self):
        assert is_twitch_channel_url("https://de.twitch.tv/somechannel/videos")

    def test_trailing_query_filter(self):
        assert is_twitch_channel_url(CHANNEL_URL + "?filter=archives")

    def test_single_vod_url_is_not_channel(self):
        # ``/videos/<id>`` must stay on the single-video path: the
        # pattern requires the channel-name segment BEFORE /videos.
        assert not is_twitch_channel_url("https://www.twitch.tv/videos/12345")

    def test_channel_root_without_videos(self):
        # A bare channel page is a LIVE stream, not a VOD listing —
        # leave it alone (importing a live stream is out of scope).
        assert not is_twitch_channel_url("https://www.twitch.tv/somechannel")

    def test_non_twitch_site(self):
        assert not is_twitch_channel_url("https://www.youtube.com/@somechannel/videos")

    def test_scheme_and_case_insensitive(self):
        assert is_twitch_channel_url("http://WWW.Twitch.TV/SomeChannel/videos")

    def test_bad_channel_charset(self):
        # A name with characters Twitch never allows must not match, so
        # e.g. a crafted URL can't slip an extra path segment through.
        assert not is_twitch_channel_url("https://www.twitch.tv/some channel/videos")

    def test_channel_name_too_long(self):
        assert not is_twitch_channel_url("https://www.twitch.tv/" + "a" * 26 + "/videos")


class TestCanonicalVodUrl:
    def test_v_prefix_stripped(self):
        assert _canonical_vod_url("v123") == "https://www.twitch.tv/videos/123"

    def test_bare_id_kept(self):
        assert _canonical_vod_url("123") == "https://www.twitch.tv/videos/123"


def _fake_ytdlp_script(tmp_path: Path, body: str) -> Path:
    """Write a ``yt_dlp.py`` shim whose module body is ``body``.

    The resolver invokes ``sys.executable -m yt_dlp ...``; a file named
    ``yt_dlp.py`` on PYTHONPATH shadows the real module for the child.
    """
    shim = tmp_path / "yt_dlp.py"
    shim.write_text(textwrap.dedent(body), encoding="utf-8")
    return shim


class TestResolveChannelVods:
    def _env(self, tmp_path: Path, monkeypatch) -> None:
        # Prepend the shim's dir to the CHILD's module search path.
        import os

        monkeypatch.setenv(
            "PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", "")
        )

    def test_lists_vods_newest_first(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(
            tmp_path,
            """
            import sys
            for vid, dur, title in [
                ("v333", "100.0", "Newest"),
                ("v222", "200.0", "Middle"),
                ("v111", "NA", "Live"),
            ]:
                print(f"{vid}::{dur}::{title}")
            """,
        )
        self._env(tmp_path, monkeypatch)

        vods = resolve_channel_vods(CHANNEL_URL, 3)

        assert [v.video_id for v in vods] == ["v333", "v222", "v111"]
        assert vods[0].url == "https://www.twitch.tv/videos/333"
        assert vods[0].title == "Newest"
        assert vods[0].duration == 100.0
        # NA duration (live/upcoming marker) becomes None, not a crash.
        assert vods[2].duration is None

    def test_title_with_separator_stays_intact(self, tmp_path: Path, monkeypatch):
        # maxsplit=2 keeps a ``::`` inside the title in the title field.
        _fake_ytdlp_script(
            tmp_path,
            """
            print("v9::60.0::Title with :: inside")
            """,
        )
        self._env(tmp_path, monkeypatch)

        (vod,) = resolve_channel_vods(CHANNEL_URL, 1)
        assert vod.title == "Title with :: inside"

    def test_unknown_lines_skipped(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(
            tmp_path,
            """
            print("[download] Downloading webpage")
            print("v5::30.0::Real entry")
            """,
        )
        self._env(tmp_path, monkeypatch)

        vods = resolve_channel_vods(CHANNEL_URL, 1)
        assert len(vods) == 1
        assert vods[0].video_id == "v5"

    def test_no_vods_raises(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(tmp_path, "")
        self._env(tmp_path, monkeypatch)

        with pytest.raises(ChannelImportError, match="No VODs"):
            resolve_channel_vods(CHANNEL_URL, 1)

    def test_ytdlp_failure_raises_with_stderr(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(
            tmp_path,
            """
            import sys
            sys.stderr.write("ERROR: somechannel does not exist\\n")
            sys.exit(1)
            """,
        )
        self._env(tmp_path, monkeypatch)

        with pytest.raises(ChannelImportError, match="does not exist"):
            resolve_channel_vods(CHANNEL_URL, 1)

    def test_missing_ytdlp_raises(self, tmp_path: Path, monkeypatch):
        # An empty dir on PYTHONPATH doesn't shadow the module; instead
        # simulate the launcher failure the same way download.py tests
        # do: point the search path at a dir with a BROKEN yt_dlp.py.
        (tmp_path / "yt_dlp.py").write_text("raise FileNotFoundError('launch')\n")
        self._env(tmp_path, monkeypatch)

        with pytest.raises(ChannelImportError):
            resolve_channel_vods(CHANNEL_URL, 1)

    def test_limit_validation(self):
        with pytest.raises(ChannelImportError, match="limit"):
            resolve_channel_vods(CHANNEL_URL, 0)

    def test_timeout_kills_listing(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(
            tmp_path,
            """
            import time
            time.sleep(30)
            """,
        )
        self._env(tmp_path, monkeypatch)

        with pytest.raises(ChannelImportError, match="timed out"):
            resolve_channel_vods(CHANNEL_URL, 1, timeout=1)

    def test_cancel_callback_kills_listing(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(
            tmp_path,
            """
            import time
            print("v1::10.0::Entry")
            time.sleep(30)
            """,
        )
        self._env(tmp_path, monkeypatch)

        def cancel() -> bool:
            return True

        with pytest.raises(ChannelImportCancelled):
            resolve_channel_vods(CHANNEL_URL, 1, cancel_callback=cancel)

    def test_proxy_passed_to_ytdlp(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(
            tmp_path,
            """
            import sys
            # The resolver's argv: assert --proxy <url> is present.
            argv = " ".join(sys.argv)
            if "--proxy http://127.0.0.1:8080" not in argv:
                sys.stderr.write("missing --proxy\\n")
                sys.exit(1)
            print("v2::20.0::OK")
            """,
        )
        self._env(tmp_path, monkeypatch)

        vods = resolve_channel_vods(CHANNEL_URL, 1, proxy="http://127.0.0.1:8080")
        assert vods[0].video_id == "v2"

    def test_playlist_items_capped(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(
            tmp_path,
            """
            import sys
            argv = " ".join(sys.argv)
            if "--playlist-items 1:2" not in argv:
                sys.stderr.write("missing --playlist-items 1:2\\n")
                sys.exit(1)
            print("v1::10.0::A")
            print("v2::20.0::B")
            """,
        )
        self._env(tmp_path, monkeypatch)

        vods = resolve_channel_vods(CHANNEL_URL, 2)
        assert len(vods) == 2


class TestChannelVodDataclass:
    def test_frozen(self):
        import dataclasses

        v = ChannelVod(video_id="v1", url="u", title="t", duration=1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            v.video_id = "v2"  # type: ignore[misc]

    def test_fields_optional(self):
        v = ChannelVod(video_id="v1", url="u", title=None, duration=None)
        assert v.title is None and v.duration is None


def _twitch_reachable(timeout: float = 5.0) -> bool:
    """Best-effort TCP probe of twitch.tv:443 for the network smoke test.

    CI runners are routinely firewalled off from Twitch (geo/abuse
    filters), which must SKIP the smoke test, not fail it - a listing
    contract test can't diagnose a datacenter block. The pure resolver
    logic is fully covered by the shim tests above; this probe only
    gates the optional live check.
    """
    import socket

    try:
        with socket.create_connection(("www.twitch.tv", 443), timeout=timeout):
            return True
    except OSError:
        return False


def test_real_ytdlp_flat_listing(tmp_path: Path, monkeypatch):
    """Network smoke: the REAL yt-dlp lists a real public channel.

    Skipped without network/yt-dlp or when Twitch is unreachable (CI);
    kept minimal (limit=1, one page) so it stays fast. Guards the
    template/flag contract against yt-dlp drift - the unit tests above
    run a shim and can't catch that.
    """
    if (
        subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
        ).returncode
        != 0
    ):
        pytest.skip("yt-dlp not importable")
    if not _twitch_reachable():
        pytest.skip("twitch.tv unreachable (CI firewall / offline)")

    vods = resolve_channel_vods("https://www.twitch.tv/twitch/videos", 1, timeout=120)
    assert len(vods) == 1
    assert vods[0].url.startswith("https://www.twitch.tv/videos/")
