"""Tests for the GUI channel picker (stream2video/gui_channel_picker.py).

The picker is a Tk Toplevel: these tests instantiate the real dialog
against the shared ``gui`` fixture (skipped headless — same rule as
test_gui_smoke) with a fake ``listing_factory``, drive the table through
Tk's ``update()`` and assert the selection handoff.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PIL", reason="gui.py requires Pillow ([gui] extra)")
pytest.importorskip("customtkinter", reason="gui.py requires customtkinter ([gui] extra)")

from stream2video.channel import ChannelImportError, ChannelVod


def _vods(n: int = 3) -> list[ChannelVod]:
    return [
        ChannelVod(
            video_id=f"v{i}",
            url=f"https://www.twitch.tv/videos/{i}",
            title=f"Stream {i}",
            duration=60.0 * i,
            view_count=100 * i,
            timestamp=1755500000 + i * 86400,
        )
        for i in range(1, n + 1)
    ]


class TestChannelPickerDialog:
    def _make_dialog(self, gui, factory=None, started=None):
        from stream2video.gui_channel_picker import ChannelPickerDialog

        def _default_factory():
            return _vods()

        if factory is None:
            factory = _default_factory
        if started is None:
            started = []
        dlg = ChannelPickerDialog(
            gui,
            factory,
            on_start=lambda picked: started.append(picked),
        )
        # Pump Tk until the background resolve lands (bounded: a hung
        # factory must not hang the test). The poller runs on the main
        # loop every 60 ms, so a few dozen update() cycles suffice.
        import time

        for _ in range(200):
            try:
                gui.update()
                dlg.update()
            except Exception:
                pytest.skip("dialog update failed (no display)")
                return None, started
            time.sleep(0.01)
            if dlg._rows or "failed" in dlg._lbl_status.cget("text").lower():
                break
        return dlg, started

    def test_dialog_fills_and_selects(self, gui):
        dlg, started = self._make_dialog(gui)
        if dlg is None:
            return
        assert len(dlg._rows) == 3
        assert "3 entries" in dlg._lbl_status.cget("text")

        # Check two boxes, Start -> on_start receives those two. The
        # table is date-sorted (newest first): row order is v3, v2, v1.
        dlg._rows[0]["checked"].set(True)  # v3
        dlg._rows[2]["checked"].set(True)  # v1
        dlg._update_count()
        assert "2 selected" in dlg._lbl_count.cget("text")

        dlg._start()
        assert len(started) == 1
        assert [v.video_id for v in started[0]] == ["v3", "v1"]

    def test_select_all_toggle(self, gui):
        dlg, _ = self._make_dialog(gui)
        if dlg is None:
            return
        dlg._var_all.set(True)
        dlg._toggle_all()
        assert all(r["checked"].get() for r in dlg._rows)
        dlg._var_all.set(False)
        dlg._toggle_all()
        assert not any(r["checked"].get() for r in dlg._rows)

    def test_sort_reorders_rows(self, gui):
        dlg, _ = self._make_dialog(gui)
        if dlg is None:
            return
        # duration desc = default date-desc first (timestamps asc with i)
        # sort by duration: longest first.
        dlg._sort_by("duration")
        assert [r["vod"].duration for r in dlg._rows] == [180.0, 120.0, 60.0]
        # Same column again: flips to ascending.
        dlg._sort_by("duration")
        assert [r["vod"].duration for r in dlg._rows] == [60.0, 120.0, 180.0]

    def test_listing_error_shows_message(self, gui):
        def _boom():
            raise ChannelImportError("channel vanished")

        dlg, started = self._make_dialog(gui, factory=_boom)
        if dlg is None:
            return
        # Pump a bit more so the error path lands.
        import time

        for _ in range(20):
            gui.update()
            dlg.update()
            time.sleep(0.02)
        assert "failed" in dlg._lbl_status.cget("text").lower()
        assert started == []
        dlg.destroy()

    def test_start_disabled_when_nothing_checked(self, gui):
        dlg, started = self._make_dialog(gui)
        if dlg is None:
            return
        # Nothing checked: Start is disabled and a direct _start call is
        # a no-op (the guard).
        assert str(dlg._btn_start.cget("state")) == "disabled"
        dlg._start()
        assert started == []
