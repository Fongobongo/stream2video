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
    parse_channel_selection,
    resolve_channel_vods,
    sort_channel_vods,
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
            for vid, dur, views, title in [
                ("v333", "100.0", "97428", "Newest"),
                ("v222", "200.0", "3343", "Middle"),
                ("v111", "NA", "NA", "Live"),
            ]:
                print(f"{vid}::{dur}::{views}::{title}")
            """,
        )
        self._env(tmp_path, monkeypatch)

        vods = resolve_channel_vods(CHANNEL_URL, 3)

        assert [v.video_id for v in vods] == ["v333", "v222", "v111"]
        assert vods[0].url == "https://www.twitch.tv/videos/333"
        assert vods[0].title == "Newest"
        assert vods[0].duration == 100.0
        assert vods[0].view_count == 97428
        # NA duration/views (live/upcoming markers) become None, not a crash.
        assert vods[2].duration is None
        assert vods[2].view_count is None

    def test_title_with_separator_stays_intact(self, tmp_path: Path, monkeypatch):
        # maxsplit=3 keeps a ``::`` inside the title in the title field.
        _fake_ytdlp_script(
            tmp_path,
            """
            print("v9::60.0::12::Title with :: inside")
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
            print("v5::30.0::77::Real entry")
            """,
        )
        self._env(tmp_path, monkeypatch)

        vods = resolve_channel_vods(CHANNEL_URL, 1)
        assert len(vods) == 1
        assert vods[0].video_id == "v5"

    def test_no_vods_raises(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(tmp_path, "")
        self._env(tmp_path, monkeypatch)

        with pytest.raises(ChannelImportError, match="No archives"):
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
            print("v1::10.0::5::Entry")
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
            print("v2::20.0::9::OK")
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
            print("v1::10.0::3::A")
            print("v2::20.0::4::B")
            """,
        )
        self._env(tmp_path, monkeypatch)

        vods = resolve_channel_vods(CHANNEL_URL, 2)
        assert len(vods) == 2

    def test_category_filter_in_listing_url(self, tmp_path: Path, monkeypatch):
        """archives/highlights/uploads map to ?filter=; clips re-route to
        the channel's /clips path."""
        _fake_ytdlp_script(
            tmp_path,
            """
            import sys
            argv = " ".join(sys.argv)
            url = [a for a in sys.argv if a.startswith("https://")][-1]
            if "?filter=highlights" not in url:
                sys.stderr.write(f"missing ?filter=highlights in {url}\\n")
                sys.exit(1)
            print("v7::120.0::500::A highlight")
            """,
        )
        self._env(tmp_path, monkeypatch)

        (vod,) = resolve_channel_vods(CHANNEL_URL, 1, category="highlights")
        assert vod.video_id == "v7"

    def test_category_archives_is_bare_listing(self, tmp_path: Path, monkeypatch):
        """``?filter=archives`` returns an EMPTY listing on Twitch (not a
        value the channel page accepts) — the archives tab is the bare
        listing URL. Verified empirically against twitch.tv."""
        _fake_ytdlp_script(
            tmp_path,
            """
            import sys
            url = [a for a in sys.argv if a.startswith("https://")][-1]
            if "?filter=" in url:
                sys.stderr.write(f"archives must be the bare listing, got {url}\\n")
                sys.exit(1)
            print("v7::120.0::500::An archive")
            """,
        )
        self._env(tmp_path, monkeypatch)

        (vod,) = resolve_channel_vods(CHANNEL_URL, 1, category="archives")
        assert vod.video_id == "v7"

    def test_category_clips_reroutes(self, tmp_path: Path, monkeypatch):
        _fake_ytdlp_script(
            tmp_path,
            """
            import sys
            url = [a for a in sys.argv if a.startswith("https://")][-1]
            if "/clips" not in url or "/videos" in url:
                sys.stderr.write(f"expected /clips path, got {url}\\n")
                sys.exit(1)
            print("577522052::19.0::411::A clip")
            """,
        )
        self._env(tmp_path, monkeypatch)

        (vod,) = resolve_channel_vods(CHANNEL_URL, 1, category="clips")
        assert vod.video_id == "577522052"
        # Clip ids have no 'v' prefix; the canonical URL still works.
        assert vod.url == "https://www.twitch.tv/videos/577522052"

    def test_unknown_category_raises(self):
        with pytest.raises(ChannelImportError, match="Unknown channel type"):
            resolve_channel_vods(CHANNEL_URL, 1, category="shorts")


class TestSortChannelVods:
    def _vod(self, vid: str, dur: float | None, views: int | None) -> ChannelVod:
        return ChannelVod(
            video_id=vid,
            url=f"https://www.twitch.tv/videos/{vid.lstrip('v')}",
            title=None,
            duration=dur,
            view_count=views,
        )

    def test_date_sort_is_newest_first_by_id(self):
        # Twitch VOD ids are sequential — a higher id is a newer recording.
        vods = [self._vod("v100", 60.0, 1), self._vod("v300", 60.0, 1), self._vod("v200", 60.0, 1)]
        assert [v.video_id for v in sort_channel_vods(vods, "date")] == ["v300", "v200", "v100"]

    def test_duration_sort_longest_first(self):
        vods = [self._vod("v1", 100.0, 1), self._vod("v2", 900.0, 1), self._vod("v3", 300.0, 1)]
        assert [v.video_id for v in sort_channel_vods(vods, "duration")] == ["v2", "v3", "v1"]

    def test_views_sort_most_watched_first(self):
        vods = [self._vod("v1", 60.0, 500), self._vod("v2", 60.0, 9000), self._vod("v3", 60.0, 12)]
        assert [v.video_id for v in sort_channel_vods(vods, "views")] == ["v2", "v1", "v3"]

    def test_missing_values_sink_not_crash(self):
        # Live entries (duration=None) and NA view counts must not
        # explode the sort — they sink to the end.
        vods = [
            self._vod("v1", None, 100),
            self._vod("v2", 300.0, None),
            self._vod("v3", 100.0, 50),
        ]
        by_dur = sort_channel_vods(vods, "duration")
        # Longest first: v2 (300s) then v3 (100s); v1 (live, no
        # duration) sinks last regardless of its view count.
        assert [v.video_id for v in by_dur] == ["v2", "v3", "v1"]
        by_views = sort_channel_vods(vods, "views")
        # Most watched first: v1 (100 views) then v3 (50); v2 (no view
        # count in the listing) sinks last regardless of duration.
        assert [v.video_id for v in by_views] == ["v1", "v3", "v2"]

    def test_unknown_sort_raises(self):
        with pytest.raises(ValueError, match="Unknown sort"):
            sort_channel_vods([], "rating")

    def test_sort_is_pure(self):
        vods = [self._vod("v1", 100.0, 1), self._vod("v2", 900.0, 9)]
        snapshot = list(vods)
        sort_channel_vods(vods, "duration")
        assert vods == snapshot


class TestParseChannelSelection:
    def test_single_number(self):
        assert parse_channel_selection("2", 5) == [2]

    def test_comma_list(self):
        assert parse_channel_selection("1,3,5", 5) == [1, 3, 5]

    def test_inclusive_range(self):
        assert parse_channel_selection("2-4", 5) == [2, 3, 4]

    def test_mixed_ranges_and_numbers(self):
        assert parse_channel_selection("1,3-5,9", 10) == [1, 3, 4, 5, 9]

    def test_duplicates_collapse(self):
        assert parse_channel_selection("3,3,1-3", 5) == [1, 2, 3]

    def test_result_is_table_order_not_typing_order(self):
        # "5,1-2" selects in table order (1,2,5), not typing order.
        assert parse_channel_selection("5,1-2", 5) == [1, 2, 5]

    def test_whitespace_free_form(self):
        assert parse_channel_selection(" 1 , 3 - 4 ", 5) == [1, 3, 4]

    def test_reversed_range_flipped(self):
        assert parse_channel_selection("4-2", 5) == [2, 3, 4]

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="outside the table"):
            parse_channel_selection("6", 5)

    def test_zero_rejected(self):
        with pytest.raises(ValueError, match="outside the table"):
            parse_channel_selection("0", 5)

    def test_garbage_token_rejected(self):
        with pytest.raises(ValueError, match="Bad"):
            parse_channel_selection("abc", 5)

    def test_bad_range_rejected(self):
        with pytest.raises(ValueError, match="Bad range"):
            parse_channel_selection("1-x", 5)

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="Empty selection"):
            parse_channel_selection("  ", 5)


class TestDurationHm:
    def test_minutes_seconds(self):
        v = ChannelVod(video_id="v1", url="u", title=None, duration=3569.0)
        assert v.duration_hm() == "59m29s"

    def test_exact_minute(self):
        v = ChannelVod(video_id="v1", url="u", title=None, duration=2700.0)
        assert v.duration_hm() == "45m"

    def test_hours(self):
        v = ChannelVod(video_id="v1", url="u", title=None, duration=12569.0)
        assert v.duration_hm() == "3h29m"

    def test_unknown(self):
        v = ChannelVod(video_id="v1", url="u", title=None, duration=None)
        assert v.duration_hm() == "?"


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
