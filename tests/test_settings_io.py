"""Tests for stream2video.settings_io (pure snapshot / YAML helpers
extracted from gui.py — incremental refactor).

The GUI's ``_save_settings`` / ``_save_user_defaults`` /
``_copy_cli_command`` previously inlined the field list / key ordering
and the YAML write. The pure helpers here let the test pin:

  * ``SAVE_SETTINGS_KEYS`` / ``USER_DEFAULTS_KEYS`` — the canonical key
    set so a missing / extra key surfaces as a focused failure instead
    of a regression a user discovers on next startup.
  * ``build_save_settings_snapshot`` / ``build_user_defaults_snapshot``
    — turn a widget-shaped dict into the persisted dict; identity-ish
    (key order preserved; bool casts left to the caller).
"""

from __future__ import annotations

from stream2video.settings_io import (
    SAVE_SETTINGS_KEYS,
    USER_DEFAULTS_KEYS,
    build_save_settings_snapshot,
    build_settings_payload,
    build_user_defaults_snapshot,
    parse_advanced_widgets,
)


class TestSaveSettingsKeys:
    def test_session_state_keys_present(self):
        # window_geometry + input_path + output_dir are session-only
        # but ARE in settings.json so the GUI reopens in the same
        # spot next time.
        for k in ("input_path", "output_dir", "window_geometry"):
            assert k in SAVE_SETTINGS_KEYS

    def test_tunables_present(self):
        # The same flat tunables the GUI's combobox enums expose.
        for k in (
            "method",
            "encoder",
            "video_quality",
            "audio_quality",
            "download_quality",
            "force",
            "delete_after",
            "per_video_dir",
            "completion_sound",
            "x264_low_memory",
            "use_crf",
            "theme",
        ):
            assert k in SAVE_SETTINGS_KEYS


class TestUserDefaultsKeys:
    def test_session_state_keys_absent(self):
        # user_defaults.json is per-user factory defaults, NOT a
        # session snapshot — so input/output paths and recent
        # projects stay out.
        for k in ("input_path", "output_dir", "window_geometry", "recent_projects"):
            assert k not in USER_DEFAULTS_KEYS

    def test_slider_tunables_present(self):
        # threshold / min_silence / margin are slider values, NOT in
        # SAVE_SETTINGS_KEYS (they live on ``self.settings`` and are
        # separately persisted) — but they ARE in user_defaults so a
        # user's slider tunables survive a GUI reset.
        for k in ("threshold", "min_silence", "margin"):
            assert k in USER_DEFAULTS_KEYS

    def test_key_sets_stay_coherent(self):
        # The two lists deliberately overlap (both persist user
        # tunables), but the relationship is exact: every key in
        # user_defaults.json is either a slider tunable (threshold /
        # min_silence / margin — persisted separately, so not in
        # SAVE_SETTINGS_KEYS) or a settings.json key. A new tunable
        # added to ONE list and forgotten in the other now fails here
        # instead of silently dropping from one persistence surface
        # (audit round 12 dedup guard).
        slider_keys = {"threshold", "min_silence", "margin"}
        assert (
            set(USER_DEFAULTS_KEYS)
            == (set(SAVE_SETTINGS_KEYS) - {"input_path", "output_dir", "window_geometry"})
            | slider_keys
        )


class TestBuildSaveSettingsSnapshot:
    def test_returns_dict_with_canonical_keys(self):
        # Pin SAVE_SETTINGS_KEYS so a future addition is visible here.
        assert SAVE_SETTINGS_KEYS == (
            "input_path",
            "output_dir",
            "method",
            "encoder",
            "video_quality",
            "audio_quality",
            "download_quality",
            "output_format",
            "force",
            "delete_after",
            "per_video_dir",
            "completion_sound",
            "x264_low_memory",
            "use_crf",
            "gapless_concat",
            "low_process_priority",
            "preset",
            "theme",
            "proxy",
            "proxy_active",
            # The 18 advanced tunables (previously CLI-only; the audit's
            # GUI-widget gap).
            "software_fallback",
            "x264_preset",
            "encoder_threads",
            "output_fps",
            "memory_limit_mb",
            "memory_reserve_mb",
            "rlimit_as_mb",
            "download_timeout",
            "connect_timeout",
            "no_progress_timeout",
            "segment_encode_timeout",
            "final_concat_timeout",
            "silence_timeout",
            "stall_kill_timeout",
            "stall_warning_timeout",
            "waveform_timeout",
            "batch_chunk_size",
            "min_part_bytes",
            "window_geometry",
        )
        # Use a sentinel dict so the test catches a missing / extra key
        # in ``SAVE_SETTINGS_KEYS`` (snapshot would KeyError or expand).
        widgets = {key: f"value-{key}" for key in SAVE_SETTINGS_KEYS}
        snapshot = build_save_settings_snapshot(widgets)
        assert set(snapshot.keys()) == set(SAVE_SETTINGS_KEYS)

    def test_preserves_input_types(self):
        # Bools stay bools, strings stay strings — the helper does NOT
        # cast (the GUI already casts at widget-read time).
        widgets = {
            "input_path": "vid.mp4",
            "output_dir": "./out",
            "method": "segment",
            "encoder": "libx264",
            "video_quality": "medium",
            "audio_quality": "high",
            "download_quality": "best",
            "output_format": "mp3",
            "force": True,
            "delete_after": False,
            "per_video_dir": True,
            "completion_sound": True,
            "x264_low_memory": False,
            "use_crf": True,
            "gapless_concat": True,
            "low_process_priority": True,
            "preset": "low_memory",
            "theme": "dark",
            "proxy": "",
            "proxy_active": False,
            "window_geometry": "1000x600+10+20",
        }
        snapshot = build_save_settings_snapshot(widgets)
        assert snapshot["force"] is True
        assert snapshot["delete_after"] is False
        assert snapshot["per_video_dir"] is True
        assert snapshot["completion_sound"] is True
        assert snapshot["x264_low_memory"] is False
        assert snapshot["use_crf"] is True
        assert snapshot["preset"] == "low_memory"
        assert snapshot["theme"] == "dark"

    def test_key_order_matches_canonical(self):
        # The on-disk JSON is built from this dict — key order is
        # observable (the file diff is more readable if it stays
        # stable). Pin it.
        widgets = {key: i for i, key in enumerate(SAVE_SETTINGS_KEYS)}
        snapshot = build_save_settings_snapshot(widgets)
        assert list(snapshot.keys()) == list(SAVE_SETTINGS_KEYS)


class TestBuildUserDefaultsSnapshot:
    def test_returns_dict_with_canonical_keys(self):
        widgets = {key: f"value-{key}" for key in USER_DEFAULTS_KEYS}
        snapshot = build_user_defaults_snapshot(widgets)
        assert set(snapshot.keys()) == set(USER_DEFAULTS_KEYS)

    def test_includes_slider_values(self):
        widgets = {
            "threshold": -30.0,
            "min_silence": 2.0,
            "margin": 0.5,
            "method": "segment",
            "encoder": "h264_nvenc",
            "video_quality": "high",
            "audio_quality": "high",
            "download_quality": "best",
            "output_format": "flac",
            "force": False,
            "delete_after": True,
            "per_video_dir": False,
            "completion_sound": True,
            "x264_low_memory": True,
            "use_crf": True,
            "gapless_concat": False,
            "low_process_priority": True,
            "preset": "maximum_performance",
            "theme": "light",
            "proxy": "http://127.0.0.1:8080",
            "proxy_active": True,
        }
        snapshot = build_user_defaults_snapshot(widgets)
        assert snapshot["threshold"] == -30.0
        assert snapshot["margin"] == 0.5
        assert snapshot["completion_sound"] is True
        assert snapshot["x264_low_memory"] is True
        assert snapshot["use_crf"] is True
        assert snapshot["preset"] == "maximum_performance"


class TestParseAdvancedWidgets:
    """Pure parser for the Advanced section's widget strings. Covers
    the audit's "no GUI surface for 18 CLI tunables" gap: the GUI
    forwards ``widget.get()`` strings; parsing must never crash on a
    half-typed field."""

    def test_combo_values_pass_through(self):
        out = parse_advanced_widgets({"x264_preset": "ultrafast", "software_fallback": "ask"})
        assert out["x264_preset"] == "ultrafast"
        assert out["software_fallback"] == "ask"

    def test_auto_or_int_accepts_auto_case_insensitive(self):
        out = parse_advanced_widgets({"encoder_threads": "AUTO", "memory_limit_mb": " auto "})
        assert out["encoder_threads"] == "auto"
        assert out["memory_limit_mb"] == "auto"

    def test_int_strings_become_ints(self):
        out = parse_advanced_widgets(
            {"encoder_threads": "4", "download_timeout": "3600", "min_part_bytes": "1048576"}
        )
        assert out["encoder_threads"] == 4
        assert out["download_timeout"] == 3600
        assert out["min_part_bytes"] == 1048576

    def test_unparseable_entry_falls_back_to_current(self):
        # The fallback keeps a half-typed field from crashing the run —
        # the widget shows the bad text, the run keeps the last
        # known-good value.
        current = {"encoder_threads": 2, "download_timeout": 900}
        out = parse_advanced_widgets(
            {"encoder_threads": "abc", "download_timeout": ""}, current=current
        )
        assert out["encoder_threads"] == 2
        assert out["download_timeout"] == 900

    def test_unparseable_entry_without_current_uses_factory_default(self):
        from stream2video.config import CONFIG_DEFAULTS

        out = parse_advanced_widgets({"encoder_threads": "???"})
        assert out["encoder_threads"] == CONFIG_DEFAULTS["encoder_threads"]

    def test_missing_keys_are_omitted(self):
        # Only keys present in ``raw`` appear in the result — the GUI
        # merges the parse result over the live settings, so omitted
        # keys keep their previous value.
        out = parse_advanced_widgets({"x264_preset": "medium"})
        assert list(out) == ["x264_preset"]

    def test_negative_and_zero_parsable(self):
        # Value-level bounds are ``validate_pipeline_config``'s job; the
        # parser just converts text → number.
        out = parse_advanced_widgets({"rlimit_as_mb": "-1", "memory_reserve_mb": "0"})
        assert out["rlimit_as_mb"] == -1
        assert out["memory_reserve_mb"] == 0


class TestBuildSettingsPayload:
    """Delta rule for settings.json: a key is persisted when it differs
    from the effective defaults (so settings.json can't permanently
    shadow user_defaults.json) or when it is session state."""

    def test_values_equal_to_baseline_are_dropped(self):
        from stream2video.config import effective_defaults

        baseline = effective_defaults()
        snapshot = dict(baseline)
        payload = build_settings_payload(snapshot)
        # Every non-session tunable at its default value is dropped so
        # settings.json can't shadow user_defaults.json. ``output_dir``
        # is the session key that ALSO lives in the defaults snapshot
        # — it always persists regardless of value.
        assert payload == {"output_dir": ""}

    def test_changed_tunable_is_written(self):
        from stream2video.config import effective_defaults

        snapshot = {**effective_defaults(), "threshold": -99.0}
        payload = build_settings_payload(snapshot)
        assert payload["threshold"] == -99.0
        # …and the session key rides along; nothing else at its default
        # value is re-writtten.
        assert set(payload) == {"output_dir", "threshold"}

    def test_session_keys_always_written(self):
        # input_path / output_dir / window_geometry are per-machine
        # state with no baseline — persist even when "unchanged".
        payload = build_settings_payload(
            {"input_path": "vid.mp4", "output_dir": "./out", "window_geometry": "1000x600"},
            baseline={},
        )
        assert payload == {
            "input_path": "vid.mp4",
            "output_dir": "./out",
            "window_geometry": "1000x600",
        }

    def test_recent_projects_rides_along(self):
        # recent_projects isn't a widget value — the GUI merges the
        # whole ``self.settings`` dict in; it must persist when it
        # differs from the (empty) baseline.
        payload = build_settings_payload({"recent_projects": ["a", "b"]}, baseline={})
        assert payload == {"recent_projects": ["a", "b"]}
        # …and is dropped when it equals the baseline (untouched list).
        payload = build_settings_payload({"recent_projects": []}, baseline={"recent_projects": []})
        assert payload == {}

    def test_explicit_baseline_wins(self):
        payload = build_settings_payload({"threshold": -30.0}, baseline={"threshold": -30.0})
        assert payload == {}
