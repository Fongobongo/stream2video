# Changelog

## [Unreleased]

### Changed
- **Silence detection — audio-only pipeline (D path)** — `detect_silence()` now optionally extracts audio to a 16kHz mono WAV (via ffmpeg `-vn -ar 16000 -ac 1 -copyts`) and runs `silencedetect` on the WAV instead of the full video. Avoids video decode/discard on every call. The WAV is cached at `{stem}_audio.wav` keyed by source mtime; re-runs with the same (or different) detection params skip the extract step.

### Added
- **`output_dir` parameter on `detect_silence()`** — enables the WAV cache pipeline. When provided, the first call extracts the WAV, runs D (on the full WAV) and a sample A (on the first `_SAMPLE_VERIFY_DURATION = 60s` of the video via ffmpeg `-t`), and keeps the WAV cache if the D-sample and A-sample segments match within `0.05s` per-segment tolerance. On mismatch, the WAV is invalidated and a full A detection is run. Subsequent calls with the same source reuse the cached WAV and skip verification entirely.
- **GUI now passes `output_dir` to `detect_silence()`** — previously the GUI only passed `progress_callback` and `cancel_callback`, so the `output_dir` parameter defaulted to `None` and the GUI was always running the A-path (no WAV cache, no speedup). CLI was already correct.
- **Per-video project directory (`per_video_dir` config / GUI checkbox)** — optional mode that groups all artifacts (downloaded source, WAV cache, JSON cache, compressed output, log file, temp dirs) into a single `{output_dir}/{stem}/` subdirectory. Local source files are never moved or copied. Now defaults to `True` (was `False` before the user-defaults feature landed).
- **Recent Projects panel (GUI)** — the left info panel now shows the 5 most recent project directories (those still on disk). Each row has a click-to-open label and a trash button that confirms before recursively deleting the project dir. List persists in `_portable/settings.json`.
- **Broken-PTS fallback** — if the D-sample and A-sample disagree on start times (e.g., sources with `itsoffset` or corrupted timestamps), the freshly-extracted WAV is deleted and a full A detection is run. Comparison is start-time-only (counts + starts within 0.05s) because A-sample's end times are artificially clipped at the -t boundary, so END comparison would false-positive on every video with a long silence that crosses the 60s window. Logs a WARNING naming the segment count mismatch.
- **Phase 2 STT prep** — the cached WAV is exactly the format Phase 2 will need for `faster-whisper`/Deepgram STT, so the extract step won't be repeated there.
- **Module-level compiled regex** for `silence_start` / `silence_end` parsing (was being recompiled on every detect call).
- **Tests** — `_segments_match` (8 cases: identity, reordering, tolerance, count mismatch, broken-PTS shift), `_get_wav_cache_path` / `_is_wav_cache_valid` (4 cases: missing/newer/older/equal mtime), `TestWavCacheFallback` (4 cases: cache hit, sample-verify pass keeps D, sample-verify mismatch falls back to full A, `output_dir=None`), and `TestEndToEndRealFfmpeg` (2 cases: D matches A on real lavfi-generated video, WAV cache reused on second run).
- **User defaults (GUI)** — new `Save current as defaults` button in the left info panel writes the current tunable settings (threshold, min_silence, margin, method, encoder, force, delete_after, per_video_dir, theme) to `_portable/user_defaults.json`. Per-session state (output_dir, recent_projects, input_path) is intentionally not saved. `Restore defaults` now restores to the user defaults (or the factory `CONFIG_DEFAULTS` if no user defaults exist). Backed by `load_user_defaults()` / `save_user_defaults()` / `effective_defaults()` helpers in `config.py` with type-validated loads, atomic writes, and graceful handling of corrupt or missing files.
- **Default change: `per_video_dir: True`** — flipped in `CONFIG_DEFAULTS` from `False` to `True` to match the workflow most users actually want (every video in its own folder).
- **Default change: `threshold: -30.0`** — was `-60.0`. `-30` is a more typical "speech" silence threshold (cuts clear pauses without being so aggressive it eats quiet consonants or background hum).
- **Status line + overall time label (GUI)** — status line per phase now shows `elapsed / remaining` (was `elapsed / total = elapsed + ETA`). Bottom row replaced the full-width progress bar with a slimmer 60%-width bar + right-aligned label `Elapsed: X | Remaining: ~Y + ?` (drops `+ ?` in the final phase, shows `Total: X` on completion). Wall-clock anchor in `_pipeline_worker`; cleared in `_set_running(False)`.
- **Final summary with size + duration metrics (GUI)** — on completion, the status line, log block, and messagebox popup now show both source and output metrics in a scannable `X -> Y` format: `Complete! 20.0 GB -> 1.1 GB, 06:04:12 -> 00:34:11 (completed in 23m 5s)`. Source duration is probed synchronously via `get_video_duration()` right after download/resolution so the final summary has all four numbers (src size, src duration, dst size, dst duration) plus total pipeline wall-clock. `?` is used for any field that can't be determined (e.g. ffprobe failed on a corrupted file). Log block is delimited by `=` separators so it's greppable. New `_fmt_clock_time()` helper formats durations as zero-padded `HH:MM:SS` (or `D:HH:MM:SS` for >= 24h) — distinct from `_fmt_time()`'s human-friendly `1h 1m 1s` format used during progress updates.

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
