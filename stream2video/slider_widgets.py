"""Pure slider text parsing / formatting helpers — extracted from
``gui.py`` (incremental refactor).

The GUI's slider rows pair a ``CTkSlider`` with a ``CTkEntry`` so the
user can either drag or type the value. Two parsing / formatting
details are easy to get wrong and useful to unit-test in isolation:

  * :func:`parse_slider_entry_value` — the entry accepts decimal
    commas (some locales type ``2,5`` instead of ``2.5``), clamps to
    the slider's min/max, and rounds to 0.1 step so it lines up with
    the slider's discrete stops. Returns ``None`` on parse failure so
    the caller can choose to fall back to the slider's current value
    (the legacy GUI's "revert entry to slider value" path).
  * :func:`format_slider_entry_value` — one-decimal display format
    (``f"{val:.1f}"``); pulled out so a test can pin the format
    without driving the Tk main loop.
  * :func:`sync_slider_entries` — read entries / clamp / round / pair
    with the slider's stored ``_entry_val`` attribute; returns a dict
    of the keys actually updated so the GUI can persist them without
    re-doing the parse.

These match exactly what the GUI used to inline three times (the
``on_entry_confirm`` closure inside ``_add_slider``, the
``_sync_slider_entries`` method, and the
``_restore_defaults`` slider-write-back). The widget-binding side
stays in ``gui.py`` — only the pure math / parse / format moves.
"""

from __future__ import annotations

# Round-trip precision: slider steps are 0.1 apart, so anything finer
# than 0.1 in the entry display doesn't match a slider stop and looks
# like a bug. The GUI used to call ``round(val, 1)`` inline — same here.
SLIDER_VALUE_PRECISION = 1

# The three tunable keys the GUI's slider panel exposes. Also referenced
# by ``_sync_slider_entries`` and ``_restore_defaults`` so the key list
# has a single source of truth.
SLIDER_KEYS: tuple[str, ...] = ("threshold", "min_silence", "margin")


def format_slider_entry_value(value: float) -> str:
    """One-decimal display format used in the entry next to the slider.

    ``f"{value:.1f}"`` — so a slider value of ``-30.0`` reads ``"-30.0"``
    in the entry, not ``"-30"`` (which would look like an int and make
    the user unsure whether the slider precision was real).
    """
    return f"{value:.1f}"


def parse_slider_entry_value(text: str, min_v: float, max_v: float) -> float | None:
    """Parse the user-typed entry text into a clamped, rounded value.

    Rules:
      * Decimal commas are accepted (``"2,5"`` ≡ ``"2.5"`` — some
        locales type commas; the GUI used to fail parsing those).
      * Clamp to ``[min_v, max_v]`` so a typed-out-of-range value lands
        at the nearest valid stop (the slider's own math clamps too,
        but the entry's display should reflect the actual stored value
        immediately after the user confirms).
      * Round to ``0.1`` (``SLIDER_VALUE_PRECISION``) so the entry
        matches a slider step; ``2.547`` becomes ``2.5``.
      * On parse failure (empty / non-numeric / extra chars), return
        ``None``. The caller falls back to the slider's current value.

    Pure: no I/O, no widget reads.
    """
    try:
        # ``replace(",", ".")`` mirrors what the GUI did inline so the
        # helper is a drop-in equivalent (some locales use commas as
        # the decimal separator; we don't accept the thousands separator
        # because that would silently turn "1,500" into "1.500" which is
        # a different value entirely).
        val = float(text.replace(",", "."))
    except (ValueError, AttributeError):
        return None
    val = max(min_v, min(max_v, val))
    return round(val, SLIDER_VALUE_PRECISION)


def sync_slider_entries(
    entries: dict[str, str],
    bounds: dict[str, tuple[float, float]] | None = None,
) -> dict[str, float]:
    """Parse the GUI's slider entry text into a config update.

    ``entries`` maps each key in :data:`SLIDER_KEYS` to the entry's
    text (or missing). Returns a dict of the keys whose entry parsed
    successfully, mapping to the rounded value. Skipped keys (parse
    failure) are simply omitted — the caller keeps the previous value
    for those (mirrors the original ``except ValueError: pass``
    semantics).

    ``bounds`` (optional): per-key ``(min_v, max_v)`` the value is
    clamped to before rounding. The GUI passes the sliders' real
    ranges so a value typed but never confirmed (e.g. the user clicks
    "Start" before FocusOut fires) still lands inside the valid range
    instead of leaking a raw ``-100`` threshold into the pipeline
    config. When omitted, legacy no-clamp behaviour is preserved for
    callers that only sync already-confirmed entries.

    Pure: no widget reads, no I/O.
    """
    result: dict[str, float] = {}
    for key, text in entries.items():
        try:
            val = float(text.replace(",", "."))
        except (ValueError, AttributeError):
            continue
        if bounds is not None and key in bounds:
            min_v, max_v = bounds[key]
            val = max(min_v, min(max_v, val))
        result[key] = round(val, SLIDER_VALUE_PRECISION)
    return result
