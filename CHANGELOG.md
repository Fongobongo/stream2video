# Changelog

## [Unreleased]

### Added
- **Audio-only silence detection (D path) with WAV cache** — `detect_silence()` extracts a 16kHz mono WAV via `ffmpeg -vn -ar 16000 -ac 1` and runs `silencedetect` on the WAV instead of decoding the full video. WAV cached at `{stem}_audio.wav` keyed by source mtime. First call runs D on the WAV and a 60-second sample-verify on the source — if the D-sample and A-sample segments match within 0.05s per-segment tolerance, the WAV is kept and reused on every subsequent run (and on the upcoming STT phase, format-compatible). Mismatch (or broken PTS) falls back to a full A detection. GUI now passes `output_dir` so the cache is actually used.
- **Per-video project directory (`per_video_dir`)** — optional mode that groups all artifacts (downloaded source, WAV cache, JSON cache, compressed output, log file, temp dirs) into a single `{output_dir}/{stem}/` subdirectory. Local source files are never moved or copied. CLI also moves the per-run log into the project dir.
- **Recent Projects panel (GUI)** — left info panel shows the 5 most recent project directories. Click label to open, trash button to confirm-and-delete. List persists in `_portable/settings.json`, pruned on render if the directory is gone, eager-saved on add. Rows truncate long filenames to 24 chars.
- **User defaults (`user_defaults.json`)** — new `Save current as defaults` button writes the current tunable settings (threshold, min_silence, margin, method, encoder, force, delete_after, per_video_dir, theme) to a separate file. `Restore defaults` now restores to these user defaults (or to the factory `CONFIG_DEFAULTS` if no user file exists). Type-validated loads, atomic writes via `os.replace`, graceful handling of corrupt or missing files. Per-session state (output_dir, recent_projects, input_path) is intentionally not saved.
- **Per-segment progress streaming + encoder fallback cleanup** — segment mode streams `-progress pipe:1` from ffmpeg and maps per-segment progress to the GUI bar (capped at 0.9, the concat step covers 0.9..1.0). On encoder fallback (h264_mf → libx264), any partial `_{stem}_segments/` is wiped so the retry starts clean.
- **Status line `elapsed/remaining` + bottom overall time label (GUI)** — per phase: `elapsed / remaining` instead of `elapsed / total`. Bottom row: progress bar + right-aligned `Elapsed: X | Remaining: ~Y + ?` (`+ ?` during phases 1-2, dropped in phase 3, `?` when no progress callback, `—` when `remaining <= 0`). Wall-clock anchor in `_pipeline_worker`; cleared in `_set_running(False)`.
- **Final summary on completion (GUI)** — log block + popup now show source/output size and duration in a scannable `X -> Y` format. Status line shows `Complete! (Xm Ys)` (or `Complete! (1h 30m 12s)`). `?` for any field that can't be determined (e.g. ffprobe failed on a corrupted file). New pure-function helper `_build_completion_summary()` for unit-testability.
- **Module-level compiled regex** for `silence_start` / `silence_end` parsing (was being recompiled on every detect call).
- **`--per-video-dir/--no-per-video-dir` CLI flag** — parity with the GUI checkbox; flag value overrides the YAML config's `per_video_dir` setting.
- **Tests** — 212 total (was 90 at v0.1.1): wav cache, sample-verify, end-to-end real ffmpeg, segment mode progress, encoder fallback, CLI per-video-dir, paths, config defaults, user defaults, GUI format helpers, `cancel_monitor` context manager, type-guard (`coerce_typed_value`) coverage, valid-value-list consistency with `concat.ENCODER_OPTS`.

### Changed
- **Default: `per_video_dir: True`** (was `False`).
- **Default: `threshold: -30.0`** (was `-60.0`) — more typical "speech" threshold, cuts clear pauses without eating quiet consonants.
- **Compact action row + slimmer window (GUI)** — Step label moved to the left cluster next to Cancel, left-anchored, fixed max width 400 px. Start/Cancel buttons narrowed to 70 px. Method/encoder combo boxes capped at 120 px (no longer expand to fill column). Progress bar shrunk to ~17% of the row width. Elapsed/Remaining label moved to the same row as the progress bar (right of it). Default window 1250 → 1080, min 1130 → 1000.
- **`_restore_defaults` now uses `_set_checkbox`** — old code hardcoded `chk_force.deselect()` and `chk_per_video_dir.deselect()` which overrode saved user defaults (e.g. `force=True` was lost). Now correctly reads from `effective_defaults()` and sets checkboxes accordingly.

### Fixed
- **Sample-verify false-positive on long silences** — A-sample's end times are clipped at the 60s window boundary, so END comparison false-positives on any video with a silence that crosses 60s. Switched to start-time-only comparison.
- **GUI step transition throttle** — `force=True` now bypasses the 0.5s status throttle on transitions; previously "Step 2/3" could be dropped on fast transitions.
- **GUI `output_dir` pass-through** — GUI was always running the A-path because it didn't pass `output_dir` to `detect_silence()`. Now the WAV cache is actually used.
- **Step hidden by left cluster** — Step label could be pushed off-screen by the buttons. Now in a separate grid column with weight=1 so it always gets the remaining width.
- **Duplicate button creation** — earlier 2-column grid refactor accidentally created two sets of buttons; one set was removed.
- **Log "Phase X/3" → "Step X/3"** — log lines and status line now use the same word.
- **Various Recent Projects fixes** — eager persist on add, file-existence pruning, click-to-open, confirmation before delete, filename truncation.
- **CLI `per_video_dir` default fallback** — `cli.py` used `config.get("per_video_dir", False)`, inconsistent with `CONFIG_DEFAULTS["per_video_dir"] = True`; now reads from `CONFIG_DEFAULTS`.
- **GUI `_load_settings` crash on corrupt JSON** — `settings.json` with a wrong-typed value (e.g. `"threshold": "abc"`) used to flow through unchecked and crash the slider/entry later. Now type-validated via shared `coerce_typed_value()`: unknown types are dropped with a debug log, JSON errors are caught explicitly.
- **GUI status label hard cut-off** — long status strings (e.g. progress with elapsed/remaining) were silently truncated mid-word at 50 chars. Now truncates with `…` like the recent-projects label.
- **GUI recent projects only tracked with `per_video_dir=True`** — users who toggled the checkbox off never saw their output directories appear in the panel. Now tracked regardless of the checkbox state.

### Refactored
- **`_build_completion_summary` extracted as module-level pure function** — takes 6 metrics, returns dict of strings, no Tk dependency, fully unit-testable.
- **Recent Projects moved above Theme** in the info panel.
- **README** — added "Performance & Caching" + "Project directory" + "Recent Projects (GUI)" + "User defaults (GUI)" sections.
- **`_portable/` untracked from git** — per-user state (settings.json, user_defaults.json) not shared across clones. Added to `.gitignore`.
- **`cancel_monitor` extracted to `utils.py`** — `@contextmanager` that spawns the cancel-monitor daemon thread, yields a `threading.Event` set on cancellation or context exit. Replaces three near-duplicate `_cancel_monitor` functions in `concat.py::_run_ffmpeg`, `silence.py::_run_silencedetect`, `silence.py::_extract_audio_wav`. Monitor thread is no longer started at all when `cancel_callback` is None.
- **Single source of truth for `VALID_METHODS` / `VALID_ENCODERS` / `VALID_THEMES`** — exported from `config.py`; CLI error messages, `concat.ENCODER_OPTS` cross-check, and GUI `CTkComboBox` values all read from these lists. New `TestValidLists` test enforces that `VALID_ENCODERS == set(ENCODER_OPTS.keys())` so a new encoder added in one place is added in both.
- **`coerce_typed_value` extracted to `config.py`** — shared type-guard used by both `load_user_defaults` (user-defaults file) and GUI `_load_settings` (session settings file). Same strict-but-forgiving filter (bool/int sub-class trap, list vs str, etc.) in both code paths.
- **`_SILENCE_POLL_INTERVAL` aliased to `CANCEL_POLL_INTERVAL`** — two 0.5s constants for the same polling cadence collapsed into one. `concat.py::_CANCEL_POLL_INTERVAL` alias removed for the same reason.
- **Slider reset binding consistency** — `on_change`, `on_entry_confirm`, and `_reset_default` now all use `key=key` (or `k=key`) as default args, so the closure can't be reused with the wrong key if any of these is ever extracted or moved. Removed unused `import math` (concat.py), `USER_DEFAULT_KEYS` and `load_user_defaults` (gui.py).

## [0.1.1] - 2026-06-04

### Fixed
- **Margin semantics** — `_apply_margin` docstring was backwards; positive margin now correctly shrinks silence (keeps more audio around phrases), negative expands it. Default changed from `-0.5` to `+0.5` so phrases are no longer clipped.
- **AAC encoder lookahead loss** — `segment` method added `-af apad` + extended `-t` by `_AUDIO_PAD = 0.1s` so AAC encoder can flush its lookahead buffer at segment boundaries; previously the last ~20-50ms of each segment's audio was being lost.
- **GUI slider precision** — `number_of_steps` now derived from range (`round((max - min) * 10)`), so sliders land on exact 0.1 increments instead of values like `-25.1` when user wants `-25`.
- **`on_entry_confirm` rounding** — entry field value is now rounded before being applied to slider and config, so entry/slider/config stay in sync.
- **Entry → config sync** — added `_sync_slider_entries()` called before pipeline start; previously, editing an entry field and clicking Start without pressing Enter would use the old value.
- **Status throttling** — terminal status messages (`Complete!`, `Cancelled`, `Failed`, `Error`) now use `force=True` to bypass 0.5s throttling; previously the final "Complete!" was dropped when the pipeline finished quickly.
- **Missing Step 2 in pipeline** — silence detection block was inadvertently removed during refactoring; restored.
- **Dead `download(...)` scaffold** — removed leftover placeholder code that called `download()` with no arguments.

### Added
- **`--delete-after` CLI flag** — parity with GUI `chk_delete` checkbox; deletes downloaded source after successful compression.
- **Resolved output path in GUI** — `lbl_output` label in info panel shows the absolute output directory after pipeline start, so user sees where the file will be saved even when the field is empty and `./compressed_videos` default is used.

### Changed
- **Defaults** — `method=segment`, `encoder=h264_mf`, `threshold=-60.0`, `min_silence=2.0` (was `batch` / `libx264` / `-20` / `1.0`). These are the softest possible values — almost no cutting by default.

### Refactored
- **`config.py`** — new module with `CONFIG_DEFAULTS` and `CONFIG_RANGES`, single source of truth shared by CLI and GUI.
- **`utils.py`** — extracted cancellation helpers (`get_active_process`, `no_window_kwargs`).
- **Code cleanup** — removed dead code, unified GUI/CLI logging, improved test coverage (silence cache, cancellation, validation).

## [0.1.0] - 2026-06-02

Initial MVP release.

- Download (yt-dlp) or local file input
- Silence detection via ffmpeg `silencedetect`
- Two cutting methods: `segment` (per-segment encode + concat demuxer) and `batch` (frame-exact select/aselect)
- Hardware encoder support: NVENC / AMF / MediaFoundation with auto-fallback to `libx264`
- CLI (Typer) and GUI (CustomTkinter) interfaces
- Silence cache (`{stem}_silence_cache.json`) with `(threshold, min_silence, margin)` keying
- Progress bar with ETA, cancellation support
