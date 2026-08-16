"""Tests for stream2video.slider_widgets (pure slider text parsing +
formatting helpers extracted from gui.py).

The slider rows in the GUI pair a draggable ``CTkSlider`` with an
editable ``CTkEntry`` that lets the user type the value. The pure math
the entry needs (decimal-comma parse, clamp, round, display format)
lives here; the widget binding stays in ``gui.py``.
"""

from __future__ import annotations

from stream2video.slider_widgets import (
    SLIDER_KEYS,
    SLIDER_VALUE_PRECISION,
    format_slider_entry_value,
    parse_slider_entry_value,
    sync_slider_entries,
)


class TestFormatSliderEntryValue:
    def test_integer_value_shows_one_decimal(self):
        # -30 should display as "-30.0" — not "-30" which would look
        # like an integer and make the user question slider precision.
        assert format_slider_entry_value(-30) == "-30.0"
        assert format_slider_entry_value(0) == "0.0"

    def test_negative_zero_honored(self):
        # Tiny edge case: -0.0 should still render with a sign so it
        # stays visually consistent with the entry's previous value.
        assert format_slider_entry_value(-0.0) == "-0.0"

    def test_fractional_value_prints_one_decimal(self):
        assert format_slider_entry_value(2.5) == "2.5"
        assert format_slider_entry_value(0.05) == "0.1"  # rounded


class TestParseSliderEntryValue:
    def test_basic_decimal_text(self):
        assert parse_slider_entry_value("2.5", 0, 60) == 2.5

    def test_decimal_comma_accepted(self):
        # Some locales type "2,5"; the helper accepts it so a German
        # keyboard user isn't blocked by a ValueError at confirm.
        assert parse_slider_entry_value("2,5", 0, 60) == 2.5

    def test_clamps_to_max(self):
        # Out-of-range high → pins at max (the slider would clamp too,
        # but the entry should still show the clamped value, not the
        # typed one — otherwise the entry and slider disagree).
        assert parse_slider_entry_value("999", 0, 60) == 60

    def test_clamps_to_min(self):
        # Out-of-range low → pins at min (same reason).
        assert parse_slider_entry_value("-100", -60, -5) == -60

    def test_rounds_to_slider_step(self):
        # The slider's stops are 0.1 apart; precision finer than that
        # would never match a stop and would make the entry and slider
        # display different values.
        assert parse_slider_entry_value("2.547", 0, 60) == 2.5
        assert parse_slider_entry_value("2.123456", 0, 60) == 2.1

    def test_returns_none_on_parse_failure(self):
        # Empty / non-numeric → None. The entry-confirm caller falls
        # back to the slider's current value (the legacy GUI's "revert
        # entry text to slider" pattern).
        assert parse_slider_entry_value("", 0, 60) is None
        assert parse_slider_entry_value("abc", 0, 60) is None
        assert parse_slider_entry_value("2.5.7", 0, 60) is None

    def test_negative_value_in_negative_range(self):
        # The slider keys include threshold (range [-60, -5]) and the
        # entry needs to accept negative text in that range.
        assert parse_slider_entry_value("-30", -60, -5) == -30
        assert parse_slider_entry_value("-80", -60, -5) == -60

    def test_zero_in_range_with_zero_min(self):
        # min_silence starts at 0.1 — typing "0" should clamp to 0.1.
        assert parse_slider_entry_value("0", 0.1, 60) == 0.1

    def test_integer_value_no_decimal(self):
        # Probably most users just type "5" → "5.0" expected.
        assert parse_slider_entry_value("5", 0, 60) == 5

    def test_nan_and_infinity_rejected(self):
        """Audit round 15 P1: NaN / ±Infinity must return None (like a
        parse failure) — the min/max clamp used to pass NaN through or
        silently pin it to a range bound instead of rejecting."""
        assert parse_slider_entry_value("nan", 0, 60) is None
        assert parse_slider_entry_value("NaN", 0, 60) is None
        assert parse_slider_entry_value("inf", 0, 60) is None
        assert parse_slider_entry_value("-inf", 0, 60) is None
        assert parse_slider_entry_value("1e999", 0, 60) is None


class TestSyncSliderEntries:
    def test_returns_empty_dict_for_empty_input(self):
        # No entries to parse → no updates to make.
        assert sync_slider_entries({}) == {}

    def test_returns_only_parsed_keys(self):
        # Parse failure shouldn't leave a stale NaN or 0 in the result;
        # the caller keeps its previous value for the failed key.
        result = sync_slider_entries(
            {
                "threshold": "-30.0",
                "min_silence": "abc",  # parse failure
                "margin": "0.5",
            }
        )
        assert "min_silence" not in result
        assert result["threshold"] == -30.0
        assert result["margin"] == 0.5

    def test_accepts_decimal_comma(self):
        # Matches ``parse_slider_entry_value``'s comma acceptance so a
        # user typing ``2,5`` before hitting Start doesn't lose the
        # value on the GUI's pre-run ``_sync_slider_entries`` path.
        result = sync_slider_entries({"threshold": "2,5"})
        assert result == {"threshold": 2.5}

    def test_rounds_to_one_decimal(self):
        # ``2.547`` rounds to ``2.5`` to match the slider's steps.
        assert sync_slider_entries({"threshold": "2.547"}) == {"threshold": 2.5}

    def test_does_not_clamp(self):
        # ``_sync_slider_entries`` is the GUI's pre-run path (it
        # 之美 runs on Start) and historically only ROUNDED — the clamp
        # lived in the entry-confirm path. Pin that behaviour so a
        # user-typed out-of-range value isn't silently clamped here
        # (the slider would just display the out-of-range value; the
        # subsequent Run would either surface it via the slider's own
        # clamp or the pipeline-validations would).
        #
        # Out-of-range = 999, no clamp here → result keeps 100 after round(999, 1)=999.0
        result = sync_slider_entries({"min_silence": "999"})
        assert result == {"min_silence": 999.0}

    def test_skips_key_when_text_missing(self):
        # If the GUI only passed some of SLIDER_KEYS (e.g., the slider
        # for one key wasn't built yet — edge case during initial
        # construction), the missing key is simply absent from the
        # result; the caller keeps its previous value for it.
        result = sync_slider_entries({"threshold": "-30"})
        assert "min_silence" not in result
        assert result["threshold"] == -30.0

    def test_nan_and_infinity_skipped(self):
        """Audit round 15 P1: a NaN/±Inf entry is treated like a parse
        failure — omitted from the result instead of being clamped to a
        range bound or passed through to the pipeline config."""
        result = sync_slider_entries(
            {
                "threshold": "-30.0",
                "min_silence": "nan",
                "margin": "inf",
            }
        )
        assert "min_silence" not in result
        assert "margin" not in result
        assert result["threshold"] == -30.0


class TestSliderKeys:
    def test_contains_three_tunables(self):
        # The three slider rows in the Controls panel do threshold,
        # min_silence, margin — make sure no one accidentally drops or
        # adds one here without also updating ``_sync_slider_entries``.
        assert set(SLIDER_KEYS) == {"threshold", "min_silence", "margin"}

    def test_value_precision_is_one(self):
        # 0.1 steps; if someone changes this they probably also need
        # to update the slider's ``number_of_steps`` and the entry's
        # display format. Test pins it so the change is intentional.
        assert SLIDER_VALUE_PRECISION == 1
