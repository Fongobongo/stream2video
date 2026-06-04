# Changelog

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
