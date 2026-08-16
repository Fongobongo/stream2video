"""Tests for stream2video.config — defaults, ranges, and user-defaults I/O."""

import json

import pytest

from stream2video.config import (
    AUTO_OR_INT_KEYS,
    CONFIG_DEFAULTS,
    CONFIG_RANGES,
    DEFAULT_PRESET,
    PRESET_NAMES,
    PRESETS,
    USER_DEFAULT_KEYS,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_THEMES,
    apply_preset,
    coerce_typed_value,
    effective_defaults,
    load_user_defaults,
    save_user_defaults,
)

# Keys that accept "auto" OR an int; their CONFIG_DEFAULTS is the
# literal "auto" (a string) so the numeric range doesn't bound it.
# Single source of truth is config.AUTO_OR_INT_KEYS — import it, don't
# re-list the keys here.


class TestConfigDefaults:
    """Sanity checks on the static defaults dict."""

    def test_per_video_dir_is_true_by_default(self):
        # As of the user-defaults feature, per_video_dir defaults to True.
        assert CONFIG_DEFAULTS["per_video_dir"] is True

    def test_required_keys_present(self):
        for key in (
            "threshold",
            "min_silence",
            "margin",
            "method",
            "encoder",
            "force",
            "delete_after",
            "per_video_dir",
            "completion_sound",
            "output_dir",
            "theme",
            "recent_projects",
        ):
            assert key in CONFIG_DEFAULTS, f"missing default for {key}"

    def test_ranges_cover_defaults(self):
        for key, (lo, hi) in CONFIG_RANGES.items():
            default = CONFIG_DEFAULTS[key]
            if key in AUTO_OR_INT_KEYS:
                # These keys default to the literal "auto" — range
                # checks don't apply to the auto-or-int fallback.
                assert default == "auto"
                continue
            assert lo <= default <= hi, (
                f"default for {key} ({default}) is outside range ({lo}, {hi})"
            )


class TestValidLists:
    """The VALID_* lists are the single source of truth for what
    values the CLI / GUI will accept. They must include the defaults
    and stay in sync with concat.py's ENCODER_OPTS."""

    def test_valid_methods_default_in_list(self):
        assert CONFIG_DEFAULTS["method"] in VALID_METHODS

    def test_valid_encoders_default_in_list(self):
        assert CONFIG_DEFAULTS["encoder"] in VALID_ENCODERS

    def test_valid_themes_default_in_list(self):
        assert CONFIG_DEFAULTS["theme"] in VALID_THEMES

    def test_valid_encoders_match_encoder_opts(self):
        # concat.py builds ENCODER_OPTS from the same set; if you add
        # a new encoder, update both.
        from stream2video.concat import ENCODER_OPTS

        assert set(VALID_ENCODERS) == set(ENCODER_OPTS.keys())

    def test_valid_methods_content(self):
        assert set(VALID_METHODS) == {"segment", "batch", "cut_then_encode"}

    def test_valid_themes_content(self):
        assert set(VALID_THEMES) == {"dark", "light", "system"}


class TestCoerceTypedValue:
    """coerce_typed_value — single-key type guard used by both
    load_user_defaults and the GUI's _load_settings."""

    def test_unknown_key_returns_none(self):
        assert coerce_typed_value("not_a_real_key", 42) is None

    def test_float_key_accepts_float(self):
        assert coerce_typed_value("threshold", -30.0) == -30.0

    def test_float_key_accepts_int(self):
        assert coerce_typed_value("threshold", -30) == -30

    def test_float_key_rejects_bool(self):
        # bool is a subclass of int in Python; guard explicitly rejects it
        # so True can't masquerade as a numeric setting.
        assert coerce_typed_value("threshold", True) is None

    def test_float_key_rejects_string(self):
        assert coerce_typed_value("threshold", "abc") is None

    def test_float_key_rejects_none(self):
        assert coerce_typed_value("threshold", None) is None

    def test_float_key_rejects_non_finite(self):
        # Audit round 16 P1: NaN/Infinity written by an older build (or
        # hand-edited) must be DROPPED, not passed through — json.dump
        # accepts them silently, but they poison downstream math
        # (threshold < -60 style comparisons, min/max) and crash the
        # json.dump(allow_nan=False) save path with an ugly traceback
        # instead of a graceful WARN + defaults fallback.
        assert coerce_typed_value("threshold", float("nan")) is None
        assert coerce_typed_value("threshold", float("inf")) is None
        assert coerce_typed_value("threshold", float("-inf")) is None

    def test_float_key_out_of_range_dropped(self):
        # Range guard now also applies to float-typed defaults, so a
        # hand-edited out-of-range value can't poison comparisons.
        assert coerce_typed_value("threshold", 0.0) is None
        assert coerce_typed_value("threshold", -100.0) is None
        assert coerce_typed_value("margin", 10.0) is None
        assert coerce_typed_value("min_silence", 0.0) is None

    def test_int_key_rejects_non_finite(self):
        # int-typed keys: NaN already failed is_integer() pre-fix, but
        # the isfinite guard now rejects it earlier — pin the contract
        # so the order of checks can't silently change behaviour.
        assert coerce_typed_value("segment_encode_timeout", float("nan")) is None
        assert coerce_typed_value("segment_encode_timeout", float("inf")) is None

    def test_str_key_accepts_str(self):
        assert coerce_typed_value("output_dir", "/tmp/foo") == "/tmp/foo"

    def test_str_key_rejects_int(self):
        assert coerce_typed_value("output_dir", 123) is None

    def test_bool_key_accepts_bool(self):
        assert coerce_typed_value("per_video_dir", True) is True
        assert coerce_typed_value("per_video_dir", False) is False

    def test_bool_key_rejects_int(self):
        # 1 is truthy but is not a bool — strict type match.
        assert coerce_typed_value("per_video_dir", 1) is None

    def test_bool_key_rejects_str(self):
        assert coerce_typed_value("per_video_dir", "yes") is None

    def test_list_key_accepts_list(self):
        assert coerce_typed_value("recent_projects", ["a", "b"]) == ["a", "b"]

    def test_list_key_rejects_str(self):
        assert coerce_typed_value("recent_projects", "a,b") is None

    def test_auto_or_int_accepts_auto_case_insensitive(self):
        # Parity with the CLI flag (cli_resolver lowercases before
        # matching) and the GUI's Advanced entries — "auto" has one
        # spelling rule across all surfaces; the returned value is the
        # canonical lowercase form the rest of the pipeline compares
        # against (``== "auto"``).
        assert coerce_typed_value("encoder_threads", "AUTO") == "auto"
        assert coerce_typed_value("encoder_threads", " Auto ") == "auto"
        assert coerce_typed_value("memory_limit_mb", "AUTO") == "auto"

    def test_user_default_keys_are_a_subset_of_defaults(self):
        for key in USER_DEFAULT_KEYS:
            assert key in CONFIG_DEFAULTS, f"USER_DEFAULT_KEYS references unknown key {key}"
        # Per-session keys are NOT in USER_DEFAULT_KEYS
        assert "output_dir" not in USER_DEFAULT_KEYS
        assert "recent_projects" not in USER_DEFAULT_KEYS


class TestLoadUserDefaults:
    """load_user_defaults() — read user_defaults.json and apply type guards."""

    def test_no_file_returns_empty_dict(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        assert load_user_defaults() == {}

    def test_valid_file_returns_parsed_overrides(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps({"threshold": -50.0, "per_video_dir": True}))
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        result = load_user_defaults()
        assert result == {"threshold": -50.0, "per_video_dir": True}

    def test_unknown_keys_ignored(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(
            json.dumps(
                {
                    "threshold": -40.0,
                    "mystery_key": 123,
                    "input_path": "/etc/passwd",
                }
            )
        )
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        result = load_user_defaults()
        assert "mystery_key" not in result
        assert "input_path" not in result
        assert result["threshold"] == -40.0

    def test_wrong_type_dropped(self, tmp_path, monkeypatch):
        # per_video_dir is a bool — reject strings/numbers.
        fake = tmp_path / "user_defaults.json"
        fake.write_text(
            json.dumps(
                {
                    "per_video_dir": "yes",  # wrong type
                    "threshold": "loud",  # wrong type
                    "method": 42,  # wrong type
                }
            )
        )
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        result = load_user_defaults()
        assert result == {}

    def test_bool_not_accepted_for_numeric_keys(self, tmp_path, monkeypatch):
        # In Python, bool is a subclass of int — guard against that.
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps({"threshold": True}))
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        result = load_user_defaults()
        assert "threshold" not in result

    def test_non_finite_literals_dropped(self, tmp_path, monkeypatch):
        # Audit round 16 P1: a JSON file written by an older build with
        # plain json.dump() can contain NaN/Infinity/-Infinity tokens
        # (json.loads accepts them). The load path must drop those keys,
        # not let them reach the pipeline.
        fake = tmp_path / "user_defaults.json"
        fake.write_text(
            '{"threshold": NaN, "min_silence": Infinity, "margin": -Infinity, '
            '"per_video_dir": true}'
        )
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        result = load_user_defaults()
        assert "threshold" not in result
        assert "min_silence" not in result
        assert "margin" not in result
        assert result == {"per_video_dir": True}

    def test_corrupt_json_returns_empty(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text("{not valid json")
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        assert load_user_defaults() == {}

    def test_top_level_non_dict_returns_empty(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps([1, 2, 3]))
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        assert load_user_defaults() == {}


class TestSaveUserDefaults:
    """save_user_defaults() — atomic write, filter to USER_DEFAULT_KEYS."""

    def test_persists_subset_of_keys(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        save_user_defaults(
            {
                "threshold": -45.0,
                "min_silence": 1.5,
                "per_video_dir": True,
            }
        )
        data = json.loads(fake.read_text())
        assert data == {
            "threshold": -45.0,
            "min_silence": 1.5,
            "per_video_dir": True,
        }

    def test_drops_per_session_keys(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        save_user_defaults(
            {
                "threshold": -50.0,
                "output_dir": "/tmp/secret",  # per-session, must be dropped
                "recent_projects": ["/etc/passwd"],  # per-session, must be dropped
                "input_path": "not-a-default",
            }
        )
        data = json.loads(fake.read_text())
        assert "output_dir" not in data
        assert "recent_projects" not in data
        assert "input_path" not in data
        assert data["threshold"] == -50.0

    def test_round_trip(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        original = {
            "threshold": -35.0,
            "min_silence": 3.0,
            "margin": -0.5,
            "method": "segment",
            "encoder": "libx264",
            "force": True,
            "delete_after": False,
            "per_video_dir": True,
            "theme": "light",
        }
        save_user_defaults(original)
        loaded = load_user_defaults()
        assert loaded == original

    def test_non_finite_raises_and_keeps_previous_file(self, tmp_path, monkeypatch):
        # Audit round 16 P1: a NaN/Infinity in the payload must raise
        # ValueError (json.dump(allow_nan=False)) so the GUI's WARN path
        # fires — and the previous file must remain byte-for-byte intact
        # (no partial/tmp-file side effects on the failed save).
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        save_user_defaults({"threshold": -42.0})
        original_text = fake.read_text()
        with pytest.raises(ValueError):
            save_user_defaults({"threshold": float("nan")})
        assert fake.read_text() == original_text


class TestPipelinePhaseTimeouts:
    """Pipeline phase timeouts + tunables exposed via CONFIG_DEFAULTS.

    These were previously module-level constants in concat.py / silence.py
    / waveform.py; the task moved them into the shared config
    so the CLI / GUI can override without code edits.
    """

    def test_phase_timeout_keys_present(self):
        for key in (
            "segment_encode_timeout",
            "final_concat_timeout",
            "silence_timeout",
            "stall_kill_timeout",
            "stall_warning_timeout",
            "waveform_timeout",
            "batch_chunk_size",
            "min_part_bytes",
        ):
            assert key in CONFIG_DEFAULTS, f"missing key {key!r}"

    def test_phase_timeout_defaults_match_historical_values(self):
        # Defaults must match the historical module-level constants so
        # existing behaviour is unchanged when the user doesn't override.
        assert CONFIG_DEFAULTS["segment_encode_timeout"] == 600
        assert CONFIG_DEFAULTS["final_concat_timeout"] == 86400
        assert CONFIG_DEFAULTS["silence_timeout"] == 36000
        assert CONFIG_DEFAULTS["stall_kill_timeout"] == 300
        assert CONFIG_DEFAULTS["stall_warning_timeout"] == 120
        assert CONFIG_DEFAULTS["waveform_timeout"] == 300
        assert CONFIG_DEFAULTS["batch_chunk_size"] == 40
        assert CONFIG_DEFAULTS["min_part_bytes"] == 1024

    def test_phase_timeouts_have_ranges(self):
        # Without a range a typo (e.g. 0 = instant timeout) would be
        # silently accepted. Each timeout has a lower bound > 0.
        for key in (
            "segment_encode_timeout",
            "final_concat_timeout",
            "silence_timeout",
            "stall_kill_timeout",
            "stall_warning_timeout",
            "waveform_timeout",
            "batch_chunk_size",
            "min_part_bytes",
        ):
            assert key in CONFIG_RANGES, f"missing CONFIG_RANGES entry for {key!r}"

    def test_phase_timeouts_in_user_default_keys(self):
        # If a key isn't in USER_DEFAULT_KEYS the GUI's "Save current as
        # defaults" silently drops it — users can't persist overrides.
        for key in (
            "segment_encode_timeout",
            "final_concat_timeout",
            "silence_timeout",
            "stall_kill_timeout",
            "stall_warning_timeout",
            "waveform_timeout",
            "batch_chunk_size",
            "min_part_bytes",
        ):
            assert key in USER_DEFAULT_KEYS, f"{key!r} not persistable as user default"

    def test_phase_timeout_coerce_rejects_garbage(self):
        # Type guard: a string where an int is expected must return None
        # so load_user_defaults() drops the bad entry instead of crashing
        # a later int arithmetic call.
        for key in (
            "segment_encode_timeout",
            "silence_timeout",
            "waveform_timeout",
            "batch_chunk_size",
            "min_part_bytes",
        ):
            assert coerce_typed_value(key, "not a number") is None
            assert coerce_typed_value(key, True) is None
            assert coerce_typed_value(key, None) is None

    def test_phase_timeout_coerce_accepts_int(self):
        for key in (
            "segment_encode_timeout",
            "silence_timeout",
            "waveform_timeout",
            "batch_chunk_size",
            "min_part_bytes",
        ):
            assert coerce_typed_value(key, 1234) == 1234

    def test_atomic_write_no_leftover_tmp(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        save_user_defaults({"threshold": -42.0})
        # The atomic write uses a .tmp file then os.replace — verify no leftover
        siblings = list(tmp_path.iterdir())
        assert siblings == [fake]

    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        # user_defaults_path() normally points to _portable/user_defaults.json
        # which already exists. Here we redirect to a deeper path.
        deep = tmp_path / "a" / "b" / "user_defaults.json"
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: deep)
        save_user_defaults({"threshold": -45.0})
        assert deep.exists()


class TestEffectiveDefaults:
    """effective_defaults() — CONFIG_DEFAULTS overlaid with user overrides."""

    def test_no_file_returns_copy_of_config_defaults(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"  # does not exist
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        result = effective_defaults()
        assert result == CONFIG_DEFAULTS
        # Must be a copy, not the same object
        assert result is not CONFIG_DEFAULTS

    def test_user_overrides_take_precedence(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(json.dumps({"threshold": -30.0, "theme": "light"}))
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        result = effective_defaults()
        assert result["threshold"] == -30.0
        assert result["theme"] == "light"
        # Untouched keys keep their factory values
        assert result["method"] == CONFIG_DEFAULTS["method"]
        assert result["encoder"] == CONFIG_DEFAULTS["encoder"]

    def test_user_invalid_overrides_are_silently_dropped(self, tmp_path, monkeypatch):
        fake = tmp_path / "user_defaults.json"
        fake.write_text(
            json.dumps(
                {
                    "per_video_dir": "maybe",  # wrong type — dropped
                    "threshold": -25.0,  # valid
                }
            )
        )
        monkeypatch.setattr("stream2video.config.user_defaults_path", lambda: fake)
        result = effective_defaults()
        assert result["per_video_dir"] == CONFIG_DEFAULTS["per_video_dir"]  # not overridden
        assert result["threshold"] == -25.0


class TestApplyPreset:
    """apply_preset — pure transform that overlays a preset's tunables
    on top of a config dict. Used by the CLI (`--preset`) and the GUI
    (combo_preset) to bundle x264_low_memory / memory_limit_mb /
    batch_chunk_size / low_process_priority into named profiles."""

    def test_balanced_is_identity_on_default_config(self):
        # ``balanced`` is an empty override dict, so applying it to the
        # default config returns a shallow copy with no changes.
        out = apply_preset(dict(CONFIG_DEFAULTS), "balanced")
        assert out == CONFIG_DEFAULTS
        assert out is not CONFIG_DEFAULTS  # copy, not alias

    def test_balanced_preserves_user_tunables(self):
        # Balanced is the identity preset: it must NOT reset preset-managed
        # keys to CONFIG_DEFAULTS — a YAML with ``x264_low_memory: true`` /
        # ``x264_preset: slow`` / ``memory_limit_mb: 4096`` or GUI checkbox
        # choices would otherwise be silently wiped on every run (the
        # previous reset-to-defaults behaviour, audit P0-1).
        cfg = dict(CONFIG_DEFAULTS)
        cfg["x264_low_memory"] = True
        cfg["batch_chunk_size"] = 20
        cfg["low_process_priority"] = True
        cfg["x264_preset"] = "slow"
        cfg["encoder_threads"] = 4
        cfg["memory_limit_mb"] = 4096
        out = apply_preset(cfg, "balanced")
        assert out["x264_low_memory"] is True
        assert out["batch_chunk_size"] == 20
        assert out["low_process_priority"] is True
        assert out["x264_preset"] == "slow"
        assert out["encoder_threads"] == 4
        assert out["memory_limit_mb"] == 4096
        assert out["preset"] == "balanced"

    def test_low_memory_overrides_tunables(self):
        out = apply_preset(dict(CONFIG_DEFAULTS), "low_memory")
        assert out["x264_low_memory"] is True
        assert out["batch_chunk_size"] == 20
        assert out["low_process_priority"] is True

    def test_maximum_performance_overrides_tunables(self):
        out = apply_preset(dict(CONFIG_DEFAULTS), "maximum_performance")
        assert out["x264_low_memory"] is False
        assert out["memory_limit_mb"] == 0
        assert out["batch_chunk_size"] == 80

    def test_does_not_touch_pipeline_only_keys(self):
        # Presets only touch tunables in PRESETS[name]; pipeline-only
        # keys (method, encoder, *_quality, threshold, timeouts) must
        # survive verbatim.
        cfg = dict(CONFIG_DEFAULTS)
        cfg["method"] = "batch"
        cfg["encoder"] = "libx264"
        cfg["video_quality"] = "high"
        cfg["threshold"] = -45.0
        out = apply_preset(cfg, "low_memory")
        assert out["method"] == "batch"
        assert out["encoder"] == "libx264"
        assert out["video_quality"] == "high"
        assert out["threshold"] == -45.0

    def test_does_not_mutate_input(self):
        cfg = dict(CONFIG_DEFAULTS)
        cfg_id = id(cfg)
        cfg_x264 = cfg["x264_low_memory"]
        out = apply_preset(cfg, "low_memory")
        # Input dict is untouched (caller may still use it).
        assert cfg["x264_low_memory"] == cfg_x264
        assert id(cfg) == cfg_id
        assert out is not cfg
        # And the output reflects the preset.
        assert out["x264_low_memory"] is True

    def test_unknown_preset_raises_value_error(self):
        cfg = dict(CONFIG_DEFAULTS)
        with pytest.raises(ValueError, match="Unknown preset"):
            apply_preset(cfg, "ultra")

    def test_preset_names_match_keys(self):
        # The exported constant should match the dict keys exactly so
        # the CLI's --preset help and the GUI's combobox values stay
        # in sync with PRESETS.
        assert tuple(PRESETS.keys()) == PRESET_NAMES

    def test_default_preset_is_balanced(self):
        # The CLI/GUI default MUST be a preset that produces no changes
        # over CONFIG_DEFAULTS — otherwise a user who never touches the
        # preset combobox would suddenly get non-default tunables, and a
        # YAML key like ``gapless_concat: false`` would be silently
        # re-enabled by the balanced preset on every run (regression).
        assert DEFAULT_PRESET == "balanced"
        assert PRESETS["balanced"] == {}

    def test_all_presets_are_subsets_of_config_defaults_keys(self):
        # Every tunable a preset overrides must exist in CONFIG_DEFAULTS,
        # otherwise apply_preset would silently introduce new keys
        # (which would later be dropped by coerce_typed_value, masking a
        # typo in PRESETS).
        for name, overrides in PRESETS.items():
            for key in overrides:
                assert key in CONFIG_DEFAULTS, (
                    f"preset {name!r} overrides {key!r} but it's not in CONFIG_DEFAULTS"
                )

    # Audit round 13 P1: an EXPLICIT YAML key must win per-key over the
    # preset. ``apply_preset`` accepts the explicit-key set, applies the
    # preset as a baseline, then layers those keys back on top.
    def test_explicit_key_wins_over_preset(self):
        cfg = dict(CONFIG_DEFAULTS)
        cfg["batch_chunk_size"] = 50
        cfg["low_process_priority"] = False
        # The file explicitly wrote batch_chunk_size; leave the other
        # preset-managed keys unset so they pick up the preset's value.
        out = apply_preset(cfg, "low_memory", explicit_keys=frozenset({"batch_chunk_size"}))
        assert out["batch_chunk_size"] == 50  # explicit YAML wins
        assert out["low_process_priority"] is True  # preset fills unset
        assert out["x264_low_memory"] is True  # preset fills unset
        assert out["preset"] == "low_memory"

    def test_no_explicit_keys_uses_preset(self):
        cfg = dict(CONFIG_DEFAULTS)
        cfg["batch_chunk_size"] = 50  # present but NOT marked explicit
        out = apply_preset(cfg, "low_memory", explicit_keys=frozenset())
        assert out["batch_chunk_size"] == 20  # preset wins when not explicit

    def test_explicit_key_none_is_identity(self):
        cfg = dict(CONFIG_DEFAULTS)
        out = apply_preset(cfg, "low_memory", explicit_keys=None)
        assert out["batch_chunk_size"] == 20

    def test_explicit_non_managed_key_is_ignored(self):
        # An explicit key not in the preset's overrides doesn't change
        # anything (there's nothing to re-layer).
        cfg = dict(CONFIG_DEFAULTS)
        out = apply_preset(cfg, "low_memory", explicit_keys=frozenset({"threshold"}))
        assert out["batch_chunk_size"] == 20
        assert out["threshold"] == CONFIG_DEFAULTS["threshold"]
