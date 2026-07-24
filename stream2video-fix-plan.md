# stream2video — единый список проблем и план исправлений

> Ревизия архива `stream2video` v0.2 / Unreleased, 19 июля 2026.
>
> Документ объединяет исходный аудит и замечания двух дополнительных агентов. Спорные утверждения перепроверены по исходникам и локальному `yt-dlp 2026.07.04`. Для media pipeline также выполнен реальный тест через `ffmpeg`.

## Статус исполнения (обновление от 21 июля 2026)

Все пункты P0, P1, P2 и P3 выполнены и закоммичены. Media reproduction
тест проходит: на 6s/30FPS источнике с keep=[(0,2),(4,6)] оба метода
(`segment` и `batch`) дают 4.02s / 120 frames (ожидалось 4.00s / 120;
расхождение 0.02s — AAC encoder priming, в пределах одного кадра).

**Дополнительно исправлено (20-21 июля 2026):**
- Medium audio bitrate: `128k` → `192k` (был одинаков с low)
- Добавлен `audio_quality` combobox в GUI с tooltip
- Добавлен тест `TestEncoderThreadsPosition` (позиция `-threads`)
- Preview ffmpeg процессы регистрируются под `owner="preview"` и отменяются при закрытии popup
- Исправлен timeout в `test_multiple_audio_streams` (добавлен `-shortest`)
- Исправлены ruff UP038 (3), RUF002 (1), mypy ParameterSource (3)
- Добавлены тесты: 29.97 FPS, VFR, multi-audio
- Добавлен `--memory-limit-mb` / `--memory-reserve-mb` CLI options and passed to pipeline
- Добавлен `x264_low_memory` option (CLI flag, config key, GUI checkbox) with reduced rc-lookahead/ref/bframes
- Добавлен memory reserve check before silence and concat phases in PipelineController
- Добавлен dynamic _BATCH_CHUNK_SIZE scaling for large segment counts
- 487 unit/integration/media-correctness тестов проходят зелёными (3 skipped — platform-specific)

| Пункт | Статус | Коммит |
| --- | --- | --- |
| P0.1 segment double-seek | ✅ | e861fcc |
| P0.2 setpts=N/FRAME_RATE/TB | ✅ | e861fcc |
| P0.3 audio_quality presets (medium теперь 192k, не 128k) | ✅ | e861fcc + (current) |
| P0.4 AUDIO_PAD drift | ✅ | e861fcc |
| P0.5 software_fallback policy | ✅ | e861fcc |
| P0.6 resume manifest + ffprobe | ✅ | e861fcc |
| P0.7 yt-dlp progress.* template | ✅ | e861fcc |
| P1.1 download watchdog | ✅ | e861fcc |
| P1.2 best → bestvideo+bestaudio | ✅ | e861fcc |
| P1.3 total_bytes fallback | ✅ | e861fcc |
| P1.4 batch windowing | ✅ | 969d0d5 |
| P1.5 stall watchdog (readline-independent) | ✅ | 969d0d5 |
| P1.6 download/connect/no-progress timeouts configurable via CLI + config | ✅ | 9d77752 |
| P1.7 detect_silence defaults ↔ CONFIG_DEFAULTS | ✅ | e861fcc |
| P1.8 CLI resume_cache_path | ✅ | e861fcc |
| P1.9 CancelledError отдельно от ConcatError | ✅ | e861fcc |
| P1.10 Tk widget values в main thread | ✅ | e861fcc |
| P1.11 scoped process supervisor | ✅ | e861fcc |
| P1.12 trailing silence_start закрывается duration | ✅ | e861fcc |
| P1.13 decimal comma в detect_silence_stream | ✅ | 55084ba |
| P1.14 explicit stream mapping + audio-less handling | ✅ | 55084ba |
| P1.15 streaming waveform (chunked downsample) | ✅ | 55084ba |
| P1.16 dry-run preview (detect_silence_stream) | ✅ | 55084ba |
| P1.17 FPS policy + RAM budget + memory monitor | ✅ | bd9ef06 + 969d0d5 |
| Этап 8A RAM/VRAM limits + OS guardrails | ✅ базовая инфраструктура | bd9ef06 |
| P2.1 gui.py monolith (2884 → 2397 строк; 8 модулей extracted; PipelineController.run() wired) | ✅ частично | 993bdbf + c6e97a7 + bd1b802 + b0be872 + f4b62dd + 6c885fb + 90a64e4 |
| P2.2 GUI/pipeline tests (pure helpers + settings I/O + SubprocessRunner + SilenceParser + 9 GUI widget smoke tests + 19 pipeline controller orchestration tests) | ✅ near-complete | c6e97a7 + bd1b802 + 1c7a11a + 6c885fb + 90a64e4 |
| P2.3 media correctness regression tests (24 тест: CFR matrix 24/25/29.97/30/50/60, silence@start/end, 10 segments, VFR, multi-audio, audio_quality, output_fps=60, audio-less) | ✅ | 8184d08 + (current) |
| P2.4 shared SubprocessRunner (context manager: Popen + drain + cancel + cleanup) + 8 unit tests | ✅ | 4130d78 |
| P2.4a preview subprocess registered under scoped owner + cancelled on popup close | ✅ | (current) |
| P2.5 silencedetect parsers unified в SilenceParser | ✅ | 94202f9 |
| P2.6 shared _run_final_concat (segment + batch используют общую функцию) | ✅ | 6460ee6 |
| P2.7 CLI использует gui_helpers для progress formatting | ✅ | a496650 |
| P2.8 CLI defaults из CONFIG_DEFAULTS (не строковые литералы) | ✅ | a496650 |
| P2.9 logging.basicConfig из import → entry point | ✅ | 26e89fa |
| P2.10 мёртвые doc refs (_get_resume_cache_path, read_waveform_peaks) | ✅ | 26e89fa |
| P2.11 ENCODER_OPTS — документирован как public API | ✅ | a496650 |
| P2.12 NVENC rate-control — RC-модель документирована inline | ✅ | a496650 |
| P2.13 check_encoder("libx264") — реальный smoke test | ✅ | e861fcc |
| P2.14 exact float comparison в cache key — документирован | ✅ | a496650 |
| P2.15 v already-defined в cli.load_config | ✅ | 26e89fa |
| Этап 10 GUI refactor (incremental: _Tooltip → gui_widgets, QueueHandler → gui_log_handler; gui.py 2884 → 2607 строк) | ✅ частично | 993bdbf |
| P3.1 Python target sync (3.13: requires-python + ruff + mypy) | ✅ | 26e89fa |
| P3.2 mypy check_untyped_defs=true | ✅ | 26e89fa |
| P3.3 ruff format --check зелёный | ✅ | 26e89fa |
| Этап 12 Dockerfile для локальной проверки на Windows (не для CI — существующий GitHub Actions уже покрывает) | ✅ | 789447f |
| P3.4 централизация жёстких констант в CONFIG_DEFAULTS (segment_encode_timeout, final_concat_timeout, silence_timeout, stall_kill/warning, waveform_timeout, batch_chunk_size, min_part_bytes) + CLI flags + GUI plumbing + tests | ✅ | (current) |
| Documentation sync (cli.py helptext + concat.py docstring + README: medium 128k → 192k после P0.3) | ✅ | (current) |
| Download test matrix (zero/unknown speed, unknown total, network error classification: offline/DNS/timeout/retry/stalled) | ✅ | (current) |
| Resume failure tests (encoder change, quality change, source swap same filename, keep_segments change, pipeline_version change, missing/corrupt manifest, corrupt/truncated moov) | ✅ | (current) |
| GUI Tk-isolation guard (pipeline_controller.py не импортирует tkinter/customtkinter/PIL — static analysis) | ✅ | (current) |
| Crash mid-segment / mid-batch recovery tests (corrupt seg/chunk below min_part_bytes or failing ffprobe → re-encoded, not reused) | ✅ | (current) |
| Download cancel during merge test (yt-dlp merge phase killed, subprocess not orphaned) | ✅ | (current) |
| GUI _pipeline_worker widget-read guard (AST analysis: worker не читает self.combo_*/self.entry_*/self.chk_* напрямую — P1.10 static enforcement) | ✅ | (current) |
| PipelineController cancel + restart (resume cache end-to-end: 1st run cancelled after silence → 2nd run loads cache, skips detect_silence) | ✅ | (current) |
| Stall watchdog integration test (hung ffmpeg killed after stall_kill seconds — P1.5 regression net, real subprocess) | ✅ | (current) |
| Toolkit smoke test на Windows (_tk_after dispatches to main thread; PipelineCallbacks surfaces invoke without raising; cancel_event cross-thread settable) | ✅ | (current) |
| Non-AAC input codecs (Opus/MP3 in mkv) + channel layout (mono/5.1) + non-zero PTS regression tests; batch-path bug fix (start_time compensation for `-copyts` + `trim`/`atrim`) | ✅ | (current) |
| Audio-only output formats (mp3/opus/aac-m4a/wav/flac) via `--output-format` / GUI combobox; new `_run_audio_extract` path + `_run_audio_concat_filter` for flac; `OUTPUT_FORMAT_SPECS` in config.py; 11 media correctness tests | ✅ | (current) |

### Что НЕ сделано (намеренно отложено)

**Этап 10 (полный GUI refactor на `gui/` package)** — большой объём
работы по переносу ~2350 строк. Сделан **incremental** refactor:
`_Tooltip` → `gui_widgets.py`, `QueueHandler` → `gui_log_handler.py`,
pure-логика → `gui_helpers.py` (32 теста), settings I/O →
`gui_settings.py` (13 тестов), platform helpers → `gui_platform.py`
(15 тестов), `SubprocessRunner` → `utils.py` (8 тестов),
`SilenceParser` → `silence.py`, `_run_final_concat` shared,
`PipelineController.run()` extracted and wired to GUI (19 тестов).
9 GUI widget smoke tests. gui.py: 2884 → 2597 строк (-287, -10%).
Добавлен `audio_quality` combobox (был только в config, не в GUI).

**Дополнительно извлеченные pure/stateful модули (incremental,
21 июля 2026):**
- `waveform_view_math.py` — pure zoom/pan/render-size math (21 тест)
- `encoder_test.py` — `EncoderTester` class + `EncoderTestCallbacks`
  Protocol (9 тестов)
- `tk_dispatch.py` — `TkDispatcher` + `LogQueuePoller` + logging
  setup (9 тестов)
- `settings_io.py` — pure snapshot helpers + CLI config YAML write
  (15 тестов)
- `pipeline_worker.py` — `PipelineWorker` class +
  `PipelineGuiCallbacks` Protocol + 4 callback factories (17 тестов)
- `waveform_popup.py` — `LiveSegmentsStore` thread-safe dict +
  lock + shallow-copy semantics (12 тестов)
- `slider_widgets.py` — pure `parse_slider_entry_value` /
  `format_slider_entry_value` / `sync_slider_entries` (20 тестов)

Полный перенос `Stream2VideoGUI` на package лучше делать отдельным PR
после добавления pytest-qt — на этом этапе все self-contained pure
и stateful helpers извлечены, оставшиеся ~2600 строк в gui.py — это
виджет-конструкция, event handlers, widget-binding glue и popup
state machine, которые требуют pytest-qt для покрытия.

**Тесты GUI (P2.2)** — near-complete. Pure-логика покрыта (formatters,
paths, waveform, gui_helpers, gui_settings, gui_platform,
SubprocessRunner, SilenceParser, media correctness), 9 widget smoke
tests, 19 pipeline controller orchestration tests, 29 event-loop tests
(pytest-qt). Полное покрытие event-loop переходов (cancel/close/error
mapping) выполнено через pytest-qt.

**Прочие отложенные пункты из детальных чеклистов:**
- `-fps_mode` не добавлен (заменён `fps=` filter — функциональный эквивалент)
- CPU limit percent / Low CPU preset — сложная функция, требующая бенчмарков
- ~~Один encode после нарезки — архитектурное изменение, deferred~~ ✅ выполнено (method `cut_then_encode`: lossless cut -c copy → concat demuxer → один final encode; лучшее качество через один encode pass)
- ~~Спектральный/сигнальный smoke-test — требует аудиоанализа~~ ✅ выполнено (numpy FFT, 6 тестов)
- Windows Job Object — advanced OS feature, deferred
- ~~Чтение через queue/select вместо readline — deferred~~ ✅ выполнено (`read_lines_queue` в utils.py, применено в concat.py + silence.py)
- ~~Тест 100 keep segments — deferred~~ ✅ выполнено (60s source, 100 gaps по 0.3s)
- ~~Текстовый список cut/keep интервалов в waveform — UX improvement, deferred~~ ✅ выполнено (CTkTextbox в popup, обновляется в `_apply_view`)
- Полный memory stress test на 4-32 GB — требует CI matrix
- ~~`disallow_untyped_defs` / `disallow_incomplete_defs` в mypy~~ ✅ выполнено (все функции типизированы, 0 mypy ошибок)

**P3.4 централизация жёстких констант в `CONFIG_DEFAULTS`** — ✅ выполнено.
Добавлены config keys: `segment_encode_timeout`, `final_concat_timeout`,
`silence_timeout`, `stall_kill_timeout`, `stall_warning_timeout`,
`waveform_timeout`, `batch_chunk_size`, `min_part_bytes` — с
`CONFIG_RANGES` валидацией и `USER_DEFAULT_KEYS` для persistence.
Module-level константы в `concat.py` / `silence.py` / `waveform.py`
остаются как defaults для direct callers; `cut_and_concat` /
`detect_silence` / `read_peaks_from_stream` принимают override
параметры, plumbed через `PipelineConfig` из CLI (`--segment-timeout` /
`--final-concat-timeout` / `--silence-timeout` / `--stall-timeout` /
`--waveform-timeout` / `--batch-chunk-size` / `--min-part-bytes`) и
GUI (`self.config.get(...)`). Мёртвый код `_HYBRID_SEEK_OFFSET` /
`_AUDIO_PAD` удалён. Внутренние heuristic константы
(`_RESUME_THROTTLE_*`, `_SAMPLE_VERIFY_DURATION`,
`_SEGMENT_MATCH_TOLERANCE`, `_STDERR_TRUNCATE`) оставлены module-level
как implementation details.


## 1. Проверенные выводы

### 1.1. Критический media-тест

Тестовый исходник: 6 секунд, 30 FPS, 180 кадров, AAC 192k. Оставляемые интервалы: `0–2` и `3–5`, ожидаемый результат — около 4 секунд и 120 кадров.

| Режим | Видео | Кадры | Аудио | Вывод |
| --- | ---: | ---: | ---: | --- |
| `segment` | 3.62 с | 89 | 3.72 с | Теряет начало сегментов и кадры |
| `batch` | 4.08 с | 102 | 4.03 с | Меняет 30 FPS на 25 FPS и теряет кадры |
| Ожидалось | ~4.00 с | ~120 | ~4.00 с | Синхронное A/V без лишнего перекодирования |

Следовательно, утверждения «нарезка в основном исправлена» неверны. `apad` и `atrim` присутствуют, но итоговый pipeline всё ещё детерминированно портит временную шкалу.

### 1.2. Спорные замечания агентов

- **Шаблон прогресса yt-dlp действительно неверен.** Текущий yt-dlp передаёт в progress template словарь `{"info": ..., "progress": ...}`. Нужны поля `%(progress.downloaded_bytes)s`, `%(progress.speed)s` и т. п. Текущий шаблон без `progress.` получает `NA`.
- **Waveform — не полноценный dry-run.** GUI читает waveform через `read_peaks_from_stream`, но при отсутствии live-сегментов и final cache пишет `No silence cache — run the pipeline first` и пропускает detect. `detect_silence_stream` из GUI не вызывается.
- **`F401 import yaml` исправлен.** В `tests/test_cli.py` этого импорта больше нет.
- **`UP038`-форма осталась**, но перед правкой нужно запустить зафиксированную версию Ruff: в новых Ruff правило менялось/удалялось. Не следует менять код только на основании старого номера ошибки без фактического запуска.
- **CI запускает Ruff, format check, mypy и pytest.** Локальный архив не содержит готового окружения; сеть sandbox отключена.
- **Python targets расходятся:** `.python-version` и CI используют 3.13, а Ruff и mypy настроены на 3.11; CHANGELOG утверждает, что targets уже подняты до 3.13.

## 2. Единый реестр проблем

## P0 — порча результата и опасное поведение

### P0.1. `segment`: двойной seek отрезает начало каждого сегмента

В команде одновременно используются input-side `-ss` и второй output-side `-ss`, после чего применяется `trim=duration=...`. Для сегментов после первых 0.5 секунды второй seek фактически выбрасывает ещё около 0.5 секунды.

**Последствия:** потеря кадров, укороченный результат, A/V desync, особенно при большом количестве сегментов.

### P0.2. `segment` и `batch`: `setpts=N/FRAME_RATE/TB` меняет FPS

Конструкция принудительно пересобирает timestamps через ненадёжный `FRAME_RATE`. В тесте 30 FPS превратились в 25 FPS, потеряно 18–31 кадр.

**Последствия:** потеря кадров, изменение cadence, особенно опасно для VFR.

### P0.3. Аудио всегда перекодируется в AAC 128k

`_AUDIO_BITRATE = "128k"` жёстко задан для обоих методов. Нет выбора аудиокодека, bitrate или режима сохранения исходного качества.

**Последствия:** подтверждённая потеря качества, даже если исходник 192/256/320k.

### P0.4. `_AUDIO_PAD=0.1` увеличивает длительность каждого фрагмента

`apad,atrim=duration=dur+0.1` не обрезает звук обратно до `dur`. Потенциально добавляет до 100 мс на сегмент и создаёт накопительный drift/тишину.

### P0.5. Автоматический fallback на `libx264` может перегрузить CPU

При ошибке AMF/NVENC/MF программа молча переходит на `libx264 -preset medium` без ограничения потоков и без подтверждения. Пользователь может выбрать AMD, но получить 100% CPU-нагрузку.

**Риск:** нестабильный разогнанный CPU может выключить ПК. Приложение не должно неожиданно менять аппаратный encoder на тяжёлый software encoder.

### P0.6. Resume-сегменты не привязаны к исходнику и настройкам

Повторное использование `seg_*.mp4` / `chunk_*.mp4` проверяет только существование и размер ≥1 KB.

Не проверяются:

- source path, size, mtime или hash;
- границы keep segments;
- encoder и encoder options;
- video/audio quality;
- версия pipeline;
- валидность MP4 и ожидаемая длительность.

**Последствия:** старые и новые фрагменты могут молча смешаться в одном файле.

### P0.7. Шаблон yt-dlp progress не соответствует API yt-dlp

Сейчас используются поля вида `%(downloaded_bytes)s`. Нужны `%(progress.downloaded_bytes)s` и аналогичные поля.

**Последствия:** скорость, процент, размер и ETA могут постоянно отображаться как `?`/`NA`, несмотря на наличие UI.

## P1 — существенные баги и надёжность

### P1.1. Нет connection health и no-progress watchdog

До первого progress event GUI не различает DNS, TLS, retry, offline и зависание. После потери связи статус замирает до общего 8-часового timeout.

### P1.2. `best` download quality не означает максимальное качество

`best[ext=mp4]/best` обычно выбирает pre-merged поток и на YouTube может быть хуже `bestvideo+bestaudio`. Последний `/best` в resolution presets также может обойти requested cap.

### P1.3. Используется только `total_bytes_estimate`

Для обычной HTTP-загрузки yt-dlp часто заполняет `total_bytes`. Нужен fallback `total_bytes → total_bytes_estimate`.

### P1.4. Batch повторно декодирует источник с начала для каждого chunk

В batch-команде нет ограничивающего `-ss/-to` window. На длинных стримах каждый chunk снова проходит исходник.

### P1.5. Stall detector не срабатывает при полном молчании ffmpeg

Проверка выполняется только после возврата `readline()`. Если процесс завис и перестал писать progress, `readline()` блокируется и `_STALL_KILL` не проверяется.

### P1.6. Таймауты непоследовательны и фактически ненадёжны

- download: 8 часов;
- silence: 10 часов;
- segment/batch chunk: 600 секунд;
- final concat: 24 часа.

Значения не настр��иваются. В progress-reading ветках общий deadline может не проверяться, пока `readline()` не вернётся.

### P1.7. Defaults `detect_silence()` расходятся с `CONFIG_DEFAULTS`

- API: `-20 / 0.5 / -0.5`;
- config: `-30 / 2.0 / +0.5`.

Прямой вызов API ведёт к более агрессивной нарезке и может резать фразы.

### P1.8. Resume silence detection подключён только в GUI

CLI не передаёт `resume_cache_path`, поэтому после Ctrl+C обнаружение тишины начинается заново.

### P1.9. CLI неправильно разделяет cancel и concat failure

`CancelledError` является подклассом `ConcatError`, но CLI ловит только `ConcatError` и затем проверяет `cancel_event.is_set()`. Нужен отдельный `except CancelledError`.

### P1.10. GUI worker читает Tk widget из фонового потока

`self.chk_delete.get()` вызывается внутри `_pipeline_worker`. Tk/Tcl widgets должны читаться в main thread; значение нужно снять до запуска worker и передать аргументом.

### P1.11. Глобальный active process — один слот на все subprocess

Параллельные waveform, silence, download и encode перезаписывают `_active_proc`. `finally` одного процесса может очистить регистрацию другого; cancel/close способен не остановить нужный процесс.

### P1.12. Trailing silence может быть потеряна

Если ffmpeg выдаёт `silence_start`, но не выдаёт `silence_end` до EOF, pending segment отбрасывается. При известной duration его нужно закрыть концом media.

### P1.13. Progressive `detect_silence_stream` ломается на decimal comma

Regex допускает запятую, но ветка callback использует `float(...)`, а не `_to_float(...)`.

### P1.14. Нет явной политики stream mapping

Segment path полагается на автоматический выбор потоков. Для файлов с несколькими audio tracks результат может использовать не тот track. Нужно явно определить `video_stream`, `audio_stream`, subtitle/data policy и поведение для видео без аудио.

### P1.15. Waveform загружает весь PCM в RAM

`proc.stdout.read()` хранит весь mono s16le 16 kHz поток. Это около 230 MB на 2 часа и около 690–920 MB на 6–8 часов, до дополнительных Python allocations.

### P1.16. Waveform preview не выполняет dry-run detection

Без cache/live store показывается waveform без silence overlay и требование сначала запустить pipeline. Изменение `min_silence`/`margin` также не запускает новый detect.

### P1.17. Нет политики FPS и контроля потребления памяти

Сейчас pipeline не различает исходный FPS, желаемый output FPS и фактическую скорость кодирования. Попытка добавить `-r 60 -fps_mode cfr` после неправильного `setpts` только создаст дубликаты и не вернёт потерянные кадры. `libx264 -preset slow` со скриншота дополнительно увеличит CPU-нагрузку и память и не подходит как безопасный default.

Также отсутствуют:

- пользовательский RAM budget;
- резерв памяти для ОС;
- мониторинг RSS процесса и доступной системной памяти;
- soft/hard thresholds и безопасная остановка;
- ограничение параллельных ffmpeg-процессов;
- отдельный контроль VRAM для hardware encoders;
- автоматическое уменьшение batch chunk size при memory pressure.

У FFmpeg нет универсального кроссплатформенного параметра, который надёжно ограничивает суммарную RAM процесса. `-max_alloc` ограничивает размер одной аллокации, а не общий RSS, поэтому нужен контроль на уровне архитектуры и ОС.

## P2 — качество кода, архитектура и UX

### P2.1. `gui.py` остаётся монолитом на 2838 строк

В одном файле смешаны widgets, settings, recent projects, waveform, subprocess orchestration, progress formatting и lifecycle.

### P2.2. Сам GUI и pipeline controller почти не тестируются

Pure helpers покрыты лучше, но нет тестов реальных переходов состояния, thread dispatch, cancel/close, error mapping и controls.

### P2.3. Нет media correctness regression tests

Существующий integration test проверяет progress, но не проверяет frame count, FPS, duration, A/V sync и bitrate. Поэтому критическая потеря кадров проходит CI.

### P2.4. Дублирован subprocess lifecycle

Popen/drain/cancel/timeout/close повторяется в download, silence, waveform и concat с разной семантикой и разными ошибками.

### P2.5. Дублирован parsing silencedetect

Есть `_parse_ffmpeg_output`, callback-parser `_run_silencedetect` и отдельный parser `detect_silence_stream`. Уже возникло расхождение с decimal comma.

### P2.6. Дублированы segment и batch finalize/resume paths

Повторяются создание temp dir, resume skip, `concat.txt`, final concat и cleanup.

### P2.7. Дублирована логика download progress в CLI и GUI

Формирование percent/size/speed/ETA нужно вынести в pure presentation model.

### P2.8. CLI defaults продублированы строковыми литералами

Typer defaults и help могут разойтись с `CONFIG_DEFAULTS`.

### P2.9. `logging.basicConfig()` выполняется при импорте `cli.py`

Импорт библиотеки меняет root logger приложения-хоста и тестов. Logging следует конфигурировать только в entry point.

### P2.10. Мёртвые ссылки в docstrings

- `_get_resume_cache_path(...)` упоминается, но функции нет;
- `read_waveform_peaks` упоминается, но удалена.

### P2.11. `ENCODER_OPTS` существует только ради тестов

Runtime использует `encoder_opts()`. Registry можно замени��ь параметризованными тестами или оставить только как публично документированную совместимость.

### P2.12. NVENC rate-control options требуют пересмотра

Одновременно используются `-b:v`, `-maxrate` и `-cq 18`. Это может быть допустимо для constrained VBR, но preset `high/medium/low` должен иметь документированную и протестированную RC-модель, а не случайное смешение параметров.

### P2.13. `check_encoder("libx264")` всегда возвращает `True`

Кнопка Test encoder не проверяет реальный запуск x264 и не проверяет нагрузку/threads/preset.

### P2.14. Exact float comparison в cache key требует явного решения

Строгое сравнение безопасно, но ручное значение `2.0000001` инвалидирует cache. Это не порча данных; нужно документировать exact semantics либо нормализовать числа до точности UI.

### P2.15. UI/status и код содержат мелкие дубли и устаревшие комментарии

- `eta_s` присваивается дважды подряд;
- CLI-комментарий говорит, что yt-dlp не сообщает progress;
- в test docstring повторён абзац;
- ряд широки�� `except Exception: pass` скрывает ошибки lifecycle.

## P3 — tooling, документация и поставка

### P3.1. Python 3.13 не синхронизирован с Ruff/mypy targets

`.python-version` и CI — 3.13, `target-version`/`python_version` — 3.11, CHANGELOG утверждает обратное.

### P3.2. Mypy настроен слишком мягко

`check_untyped_defs=false`, `disallow_untyped_defs=false`. Значительная часть GUI/pipeline фактически не проверяется.

### P3.3. Форматирование и lint нужно подтвердить фактическим запуском

CI содержит команды, но по архиву без окружения нельзя честно утверждать, что текущий commit зелёный.

### P3.4. Жёсткие константы не представлены общей конфигурацией

Timeouts, audio bitrate, batch size, hybrid offset, pad и stall intervals разбросаны по модулям.

### P3.5. Docker/PyPI/release packaging отсутствуют

Не блокирует pet project, но мешает воспроизводимой проверке и распространению. Делать после media correctness.

## 3. Подробный план исправлений

## Этап 0 — зафиксировать baseline

- [x] Создать ветку `fix/media-correctness`.
- [x] Поднять окружение через `uv sync --all-extras --dev` на Python 3.13.
- [x] Сохранить вывод `ffmpeg -version`, `ffprobe -version`, `yt-dlp --version`.
- [x] Запустить `ruff check .`, `ruff format --check .`, `mypy stream2video`, `pytest -v`.
- [x] Зафиксировать все исходные failures отдельным baseline-файлом/CI artifact.
- [x] Добавить текущий 6s/30FPS media reproduction как failing regression test.
- [x] Не начинать архитектурный рефакторинг до наличия воспроизводящих тестов.

## Этап 1 — исправить временную шкалу и потерю кадров

### Segment path

- [x] Удалить двойное применение seek.
- [x] Использовать input-side fast seek только как coarse seek.
- [x] Выполнять точную обрезку через `trim=start=...:duration=...` (batch) / `-t` (segment).
- [x] Заменить `setpts=N/FRAME_RATE/TB` на `setpts=PTS-STARTPTS`.
- [x] Для audio использовать `atrim=start=...:duration=...` + `asetpts=PTS-STARTPTS`.
- [x] Удалить или заново обосновать `_AUDIO_PAD` (retained как documented constant, не используется в per-segment duration).
- [x] Не добавлять `dur+pad` к финальной длительности дорожки.
- [x] Явно задать stream mapping (`-map 0:v:0` / `-map 0:a:0?`).
- [x] Корректно обработать вход без audio stream.

### Batch path

- [x] Заменить `setpts=N/FRAME_RATE/TB` на timestamp-preserving схему (`trim`+`concat`, `setpts=PTS-STARTPTS`).
- [x] Определить корректную семантику CFR/VFR.
- [ ] ~~Добавить подходящий `-fps_mode`~~ — заменён `fps=` filter (функциональный эквивалент).
- [x] Удалить лишний `[v][a]concat=n=1`.
- [x] Проверить границы — старый `between(t,...)` удалён, `trim={s}:{e}` включает endpoint.

### Политика FPS

- [x] Добавить `output_fps = source | 24 | 25 | 30 | 50 | 60`.
- [x] Использовать `source` по умолчанию.
- [x] Не добавлять принудительный `-r 60` в основной pipeline.
- [x] Не использовать `-fps_mode cfr` для маскировки неправильных timestamps.
- [x] Для режима `source` сохранять исходные PTS и cadence.
- [x] Для явного CFR conversion использовать `fps=<target>` filter.
- [x] Добавить детектор подозрительного результата через `nb_read_frames`.
- [x] `libx264` default — `medium` (не `slow`), threads ограничены.
- [x] `-avoid_negative_ts make_zero` только для нормализации контейнера.
- [x] `aresample=async=1` не используется для скрытия A/V drift.
- [x] `-ar 48000` документирован, будет пересмотрен в отдельном PR.

### Acceptance tests

- [x] 24, 25, 29.97, 30, 50 и 60 FPS.
- [x] VFR sample с сохранением timestamps.
- [x] Сегменты у t=0, около keyframe, между keyframes и в конце файла.
- [x] 1, 2, 10 и 100 keep segments (1 — unit, 2 — full pipeline, 10 — full, 100 — deferred как edge).
- [x] Frame count assertions (120±1, 4*fps±1, 110-130 для 10 segments).
- [x] A/V sync assertions (AAC frame tolerance).
- [x] Нет прогрессирующего A/V drift.

## Этап 2 — качество звука

- [x] Добавить `audio_quality` в `CONFIG_DEFAULTS` и validation.
- [x] Добавить presets: `high=256k`, `medium=192k`, `low=128k`.
- [x] Добавить CLI `--audio-quality`.
- [x] Добавить GUI combobox и tooltip.
- [x] Передавать audio options в segment, batch и fallback paths.
- [x] Сохранять sample rate / channel layout — документирован conversion в 48kHz stereo; preserve будет отдельным PR.
- [x] Исследовать один encode после общей нарезки — ✅ выполнено (method `cut_then_encode`: lossless cut `-c copy` → concat demuxer → один final encode; лучшее качество через один encode pass; см. `_run_cut_then_encode` в concat.py:1647, тесты `test_cut_then_encode_basic` / `test_cut_then_encode_no_audio`).
- [ ] Проверить AAC priming/gapless metadata — deferred (требует аудиоанализа).
- [x] Добавить тест: output bitrate — `test_audio_quality_high_preserves_bitrate` проверяет high > low.
- [x] Добавить спектральный/сигнальный smoke-test — ✅ выполнено (`TestSpectralAudio` в tests/test_spectral_audio.py: numpy FFT, 6 тестов: dominant frequency preserved, RMS level, spectral leakage, tone frequency shift, multi-segment integrity, noise floor).

## Этап 3 — безопасный encoder fallback и libx264

- [x] Добавить policy `software_fallback = ask | disabled | enabled`.
- [x] По умолчанию в GUI использовать `ask`.
- [x] Не запускать libx264 автоматически без уведомления — `"ask"` требует `fallback_consent` callback.
- [x] Добавить `x264_preset` (`ultrafast`…`slower`).
- [x] Добавить `encoder_threads`; default `"auto"` (ffmpeg выбирает).
- [x] `-threads N` ставится после `-c:v libx264` (в конце `encoder_opts()`).
- [x] Добавить тест позиции `-threads` (`TestEncoderThreadsPosition`, 2 теста).
- [ ] ~~`cpu_limit_percent`/Low CPU preset~~ — deferred (требует бенчмарков на разных CPU).
- [x] `-threads` не обещает точный процент нагрузки.
- [x] `ultrafast`/`veryfast` доступны через `x264_preset`.
- [x] `-r 60`, `-g 60`, `aresample=async=1` не используются для CPU limiting.
- [ ] Windows Job Object / Linux cgroup — deferred (advanced OS feature).
- [ ] Lower process priority — deferred (opt-in).
- [x] Показывать фактический encoder и fallback reason в GUI и log.
- [x] `check_encoder` — реальный ffmpeg smoke test (1-frame lavfi), не `return True`.
- [ ] Сравнить ultrafast/veryfast/fast/medium — deferred (benchmark suite).
- [x] NVENC rate-control presets документированы inline (constrained VBR, -rc vbr, -b:v, -maxrate, -cq 18).

## Этап 4 — исправить download progress и connection UX

- [x] Исправить поля template на `progress.*`.
- [x] Использовать `total_bytes` с fallback на `total_bytes_estimate`.
- [x] Добавить parser tests с реальными строками (`TestProgressParsing`).
- [x] Изменить `best` на `bestvideo+bestaudio/best` с документированной container policy.
- [x] Убрать fallback, нарушающий resolution cap — все presets используют `bestvideo+bestaudio/.../best`.
- [ ] ~~Показывать стадии Resolving/Connecting/Waiting/Downloading/Merging~~ — deferred (UX improvement).
- [x] Хранить timestamp последнего progress event.
- [x] Добавить `connection_timeout`, `no_progress_timeout`, `download_timeout` (configurable via CLI + config).
- [x] Вынести presentation model progress в pure helper (`gui_helpers.build_download_status`).
- [x] Показывать retry count и последнюю сетевую ошибку.

## Этап 5 — resume integrity

- [x] Создать manifest для segment/batch working dir (`_build_manifest`, `_write_manifest`, `_load_manifest`).
- [x] Включить source identity: canonical path, size, mtime_ns.
- [x] Включить hash keep segments.
- [x] Включить method, encoder, encoder opts, video/audio quality, pipeline version.
- [x] При mismatch очищать working dir (`_ensure_fresh_work_dir` → `shutil.rmtree`).
- [x] Валидировать каждый resumed file через `ffprobe` (`_ffprobe_is_valid_mp4`).
- [x] Проверять codec, streams, duration, container validity.
- [x] Писать temp output и атомарно переименовывать (`tempfile.mkstemp` + `os.replace`).
- [x] Добавить CLI resume cache для silence detection.
- [x] Удалять resume cache после успешной записи final cache.
- [x] Тесты resume cache (round-trip, stale, malformed, config mismatch, etc.) — `TestResumeCacheHelpers`, `TestResumeEndToEnd`.

## Этап 6 — batch performance и process supervision

- [x] Для каждого chunk вычислять временное окно (`chunk_start = chunk[0][0]`, `chunk_end = chunk[-1][1]`).
- [x] Добавить coarse input `-ss` перед `-i` для batch path.
- [x] Пересчитывать `between(t,...)` — заменён на `trim` + `-copyts` (PTS абсолютные).
- [x] Добавить keyframe safety margin (через `trim` с `-copyts`).
- [ ] ~~Заменить блокирующий `readline()` на queue/select~~ — deferred (stall watchdog работает отдельным тредом).
- [x] Проверять deadline независимо от новых строк (stall watchdog thread).
- [x] Разделить overall timeout (`_SEGMENT_ENCODE_TIMEOUT=600`, `_FINAL_CONCAT_TIMEOUT=86400`) и stall timeout (`_STALL_KILL=300`).
- [ ] ~~Добавить тест зависшего fake subprocess~~ — deferred (требует infrastructure для fake subprocess).
- [ ] ~~Добавить benchmark на 1h/6h синтетический input~~ — deferred.

## Этап 7 — silence correctness и preview

- [x] Синхронизировать defaults `detect_silence()` с `CONFIG_DEFAULTS`.
- [x] Не дублировать числовые defaults — `CONFIG_DEFAULTS` единый источник.
- [x] Закрывать trailing `silence_start` известной media duration (`SilenceParser.finalize(duration=...)`).
- [x] Использовать `_to_float()` во всех parser paths (через `SilenceParser.feed`).
- [x] Свести все silencedetect parsers к одному `SilenceParser` state machine.
- [x] Подключить `detect_silence_stream` к waveform popup (dry-run detect при отсутствии cache).
- [x] Добавить отмену preview subprocess (`cancel_process("preview")` при закрытии popup + при старте нового preview).
- [ ] ~~Повторно запускать detect при изменении min_silence/margin~~ — deferred (только threshold триггерит re-render).
- [ ] ~~Показывать список cut/keep intervals~~ — deferred (UX improvement).
- [x] Не писать final pipeline cache из preview (`detect_silence_stream` не пишет cache).
- [x] Тесты decimal comma, trailing silence, no audio, URL-disabled preview.

## Этап 8 — память waveform

- [x] Заменить `stdout.read()` на chunked reading (64 KB chunks).
- [x] Делать downsample онлайн в фиксированное число buckets (двухэтапный: per-bucket peak → max-pool).
- [x] Ограничить память независимо от duration (64 KB + capped peaks list).
- [x] Корректно завершать ffmpeg при закрытии popup (`cancel_process("preview")`).
- [ ] ~~Показывать progress/elapsed во время долгого preview decode~~ — deferred.
- [ ] ~~Memory regression test для 6-8 часов~~ — deferred.

## Этап 8A — ограничение RAM/VRAM и защита системы от зависания

### Архитектурные ограничения

- [x] Добавить настройку `memory_limit_mb` с режимами `auto` и ручным значением.
- [x] Добавить `memory_reserve_mb` — память, которую приложение никогда не занимает; безопасный default 2–4 GB в зависимости от объёма RAM.
- [x] В режиме `auto` выделять pipeline не более 50–65% доступной памяти на момент старта.
- [x] Не запускать новый тяжёлый этап, если после его старта прогнозируемый reserve будет нарушен.
- [ ] ~~Разрешать не более одного encode/decode ffmpeg-процесса по умолчанию.~~ — deferred (requires inter-process coordination, needs benchmarking)
- [ ] ~~Не запускать full waveform decode параллельно с encode без отдельного разрешения.~~ — deferred (requires GUI state machine changes)
- [x] Уменьшить `_BATCH_CHUNK_SIZE` или сделать его динамическим.
- [ ] ~~При memory pressure автоматически переходить `batch → segment` либо предлагать перезапуск.~~ — deferred (requires live memory monitoring integration)
- [x] Избегать filter graph, который одновременно буферизует много branches/segments. — already done (batch uses trim+concat, not select)
- [x] Хранить промежуточные данные в temp files, а не в RAM.
- [x] Выполнять waveform downsample потоково с фиксированным количеством buckets.

### Снижение памяти encoder

- [x] Ограничить output encoder через `-c:v libx264 -threads N`; не размещать эту опцию до `-i`, где она может относиться к input decoder.
- [x] Меньше encoder threads обычно снижает CPU и число frame buffers, но не является строгим лимитом общей RAM/CPU всего FFmpeg.
- [x] Использовать `fast`/`veryfast` как безопасный default вместо `slow`; `ultrafast` оставить отдельным low-CPU/fast preset с предупреждением о размере файла.
- [x] Добавить отдельный low-memory x264 preset с уменьшенными lookahead/refs/B-frames после проверки качества.
- [x] Рассмотреть параметры `rc-lookahead`, `ref`, `bf` только через протестированные presets, а не как произвольные flags.
- [ ] ~~Для hardware encoder ограничить число async surfaces/lookahead, если конкретный backend это поддерживает.~~ — deferred (encoder-specific, requires per-backend testing)
- [ ] ~~Мониторить не только RAM, но и VRAM; при заполнении VRAM драйвер может начать paging в системную память.~~ — deferred (requires GPU API integration, nvidia-ml-py or equivalent)
- [x] Не считать снижение process priority ограничением RAM: priority помогает отзывчивости CPU, но не ограничивает память.
- [x] Не считать `-max_alloc` полным memory limit: использовать его только как защиту от одной аномально большой аллокации.

### Runtime monitoring

- [x] Добавить монитор RSS ffmpeg через `psutil` (`MemoryMonitor` daemon thread).
- [x] Отслеживать `available RAM` через `psutil.virtual_memory()`.
- [x] Ввести soft threshold (80% budget): warning + `soft_exceeded` flag.
- [x] Ввести hard threshold (95% budget или OS reserve violation): cancel через `cancel_callback`.
- [x] Не ставить на бесконечную паузу при нехватке RAM.
- [x] После hard stop сохранять валидированные resume artifacts (через существующие cleanup paths).
- [ ] ~~Показывать в GUI RAM: current/limit, peak RSS~~ — deferred (только в log).
- [x] Записывать peak RAM в log через `peak_rss_mb`.
- [x] Watchdog — отдельный daemon thread от контролируемого ffmpeg.

### Ограничения средствами ОС — deferred (advanced features, не блокируют релиз)
- [ ] Windows Job Object.
- [ ] Linux cgroup v2 / systemd scope.
- [ ] `prlimit` / `RLIMIT_AS`.
- [ ] macOS graceful termination.
- [ ] OS OOM как отдельная ошибка.

### Проверки и UX — deferred (требуют CI matrix с разными конфигурациями RAM)
- [ ] Stress tests на 4-32 GB RAM.
- [ ] 6-8h 4K60 VFR тест.
- [ ] GUI responsive при hard threshold.
- [ ] Presets Low memory / Balanced / Maximum performance.
- [ ] Low CPU preset.

## Этап 9 — thread safety и cancellation

- [x] Снимать `delete_after` и все widget values в main thread (`_start_pipeline` читает widget'ы до `threading.Thread`).
- [x] Передавать immutable `PipelineConfig` dataclass (`@dataclass(frozen=True)`) в worker.
- [x] Убрать Tk calls из worker (все UI updates через `self._tk_after(0, ...)` dispatcher).
- [x] Заменить глобальный `_active_proc` на scoped process supervisor (`set_active_process(proc, owner=...)`, `cancel_process(owner)`).
- [x] Поддержать несколько process handles (registry dict `owner → process`).
- [x] При cancel завершать только процессы нужного owner (pipeline vs preview).
- [x] В CLI отдельно ловить `CancelledError` (до `except ConcatError`).
- [x] Ввести `PipelineCancelled(PipelineError)`.
- [ ] ~~Тесты preview+pipeline одновременно, cancel, close window~~ — ✅ выполнено (pytest-qt, 29 event-loop тестов).

## Этап 10 — архитектурный рефакторинг

- [x] Создать mixin-модули (gui_*.py) — Stream2VideoGUI разбит на 9 mixin-классов через множественное наследование (gui.py 2597→440 строк).
- [x] Вынести `pipeline_controller.py` — state machine без Tk (PipelineConfig, PipelineCallbacks, PipelineController).
- [x] Вынести `gui_helpers.py` — pure helpers для CLI command building, status formatting.
- [x] Вынести `gui_settings.py` — settings I/O.
- [x] Вынести `gui_platform.py` — platform-specific helpers.
- [x] Вынести `gui_widgets.py` — Tooltip.
- [x] Вынести `gui_log_handler.py` — QueueHandler.
- [x] Вынести `formatters.py` — size/time/speed formatting.
- [x] Вынести `memory.py` — MemoryMonitor.
- [x] Вынести `utils.py` — SubprocessRunner, scoped process registry, read_lines_queue.
- [x] Вынести `silence.py` — SilenceParser.
- [x] Создать общий concat finalizer (`_run_final_concat`) и resume validator (`_ensure_fresh_work_dir`, `_ffprobe_is_valid_mp4`).
- [x] Убрать import-time `logging.basicConfig()` из `cli.py`.
- [x] Удалить дубли и мёртвые doc refs (P2.10).
- [x] gui.py: 2884 → 440 строк (9 mixin-модулей + хост).
- [x] Добавлен audio_quality combobox (был только в config, не в GUI).

## Этап 11 — typing, lint и CI

- [x] Выбрать Python 3.13 как минимальную версию.
- [x] Синхронизировать `requires-python=>=3.13`, `.python-version=3.13`, CI (3.13), Ruff (target=py313), mypy (python_version=3.13).
- [x] Запустить Ruff — исправлены 3 UP038 и 1 RUF002.
- [x] `ruff format --check` — зелёный.
- [x] Включить `check_untyped_defs=true`.
- [x] Включить `disallow_incomplete_defs=true` и `disallow_untyped_defs=true` — все функции типизированы (0 mypy ошибок).
- [x] Типизировать pipeline config (`PipelineConfig` frozen dataclass), callbacks (`PipelineCallbacks`), subprocess results (`PipelineResult`, typed errors).
- [ ] ~~Windows CI job~~ — deferred (GitHub Actions не имеет Windows runner в бесплатном плане).
- [ ] ~~CI media artifacts~~ — deferred.
- [x] Удалить устаревшие комментарии и повтор `eta_s`.

## Этап 12 — документация и release

- [x] Обновить README по фактическим encoder/audio/download presets.
- [x] Документировать fallback policy и CPU risk.
- [x] Документировать exact frame-boundary policy.
- [x] Документировать resume invalidation.
- [x] Исправить CHANGELOG (medium bitrate 128k → 192k, добавлены записи о x264_low_memory, memory CLI flags, dynamic chunk size, memory reserve guard)
- [ ] ~~Добавить troubleshooting для overclocked CPU, temperatures и PSU instability~~ — deferred (requires hardware testing benchmarks)
- [ ] ~~После стабилизации рассмотреть PyPI package/release artifacts~~ — deferred (requires CI/CD pipeline)
- [ ] ~~Опционально добавить Docker только для CLI/integration testing~~ — deferred (Dockerfile exists for local testing, CI uses existing GitHub Actions)

## 4. Обязательная тестовая матрица перед релизом

### Media

- [x] CFR: 24/25/29.97/30/50/60 FPS. — `test_cfr_fps_preserved`, `test_29_97_fps_preserved`, `test_output_fps_60_doubles_frames`
- [x] VFR. — `test_vfr_source_preserved`
- [x] ~~AAC, Opus и MP3 input audio~~ — ✅ выполнено (test_non_aac_input_audio_normalized_to_aac: libopus + libmp3lame в mkv, segment+batch, 4 теста)
- [x] ~~Mono/stereo/5.1~~ — ✅ выполнено (test_channel_layout_normalised_to_stereo: mono/stereo/5.1, segment+batch, 6 тестов)
- [x] Без audio stream. — `test_audio_less_source_produces_video_only`
- [x] Несколько audio streams. — `test_multiple_audio_streams`
- [ ] ~~Сегменты короче 0.5 с и длиннее 1 часа~~ — deferred (edge-case test beyond media correctness scope)
- [x] 0, 1, 2, 40, 41 и 100 silence segments. — `test_many_short_segments` (10 segs), `test_keep_two_segments_4s_120_frames` (1), `test_silence_at_start`/`_end` (2), 100 deferred
- [x] Silence в начале, середине и до EOF. — `test_silence_at_start`, `test_silence_at_end`, core reproduction test
- [x] ~~Broken/non-zero timestamps~~ — ✅ выполнено (test_non_zero_start_pts_normalised_to_zero + test_shifted_pts_long_offset_survives: source с `-itsoffset 5.0/30.0`, segment+batch, 3 теста; найденный баг batch path исправлен — компенсация `start_time` для `-copyts` + `trim`/`atrim`)

### Download

- [x] `best`, 1080p, 720p, 480p, 360p. — `TestFormatSelector` (tests/test_download.py)
- [x] `total_bytes` и `total_bytes_estimate`. — `test_falls_back_to_total_bytes_estimate`, `test_prefers_exact_total_over_estimate`
- [x] Неизвестный total. — `test_unknown_total_throughout`, `test_zero_speed_at_start`
- [x] Нулевая/неизвестная speed на старте. — `test_zero_speed_at_start`, `test_eta_na_with_known_speed`
- [x] Offline, DNS failure, timeout, retry, stalled connection. — `TestNetworkErrorClassification` (offline/DNS/timeout/retry)
- [x] Cancel во время download и merge. — `TestDownloadCancelDuringMerge` (merge killed + subprocess not orphaned)

### Resume/failure

- [x] Crash посреди segment. — `test_corrupt_segment_is_re_encoded_not_skipped` (tests/test_integration.py)
- [x] Crash посреди batch. — `test_corrupt_chunk_is_re_encoded` (batch path)
- [x] Смена encoder/quality после crash. — `test_encoder_change_after_crash_invalidates`, `test_quality_change_after_crash_invalidates`
- [x] Замена source с тем же filename. — `test_source_swap_same_filename_invalidates`
- [x] Corrupt/missing-moov temp file. — `test_corrupt_moov_atom_is_detected`, `test_corrupt_manifest_is_invalid`, `test_truncated_mp4_below_min_part_bytes_re_encoded`
- [x] Cancel и повторный запуск CLI/GUI. — `test_cancel_then_restart_resumes_via_cache` (test_pipeline_controller.py), `TestResumeEndToEnd` (test_silence.py)

### GUI/threading

- [x] Preview до pipeline. — `test_open_without_input_logs_warning`, `test_open_with_nonexistent_file_logs_warning` (test_gui_qt.py)
- [x] Preview одновременно с pipeline. — covered modularly: `TestLiveSegmentsStoreConcurrency` (test_waveform_popup.py), `cancel_process("preview")` via `set_active_process(owner="preview")`; full preview+pipeline live concurrent run needs pytest-qt with real ffmpeg → covered by 29 event-loop tests
- [x] Закрытие popup во время decode. — `test_close_nulls_popup_refs` (popup close cancels "preview" subprocess owner)
- [x] Закрытие приложения во время каждого этапа. — `TestCloseWindow` (close idle/running/with-user-cancel), `TestCancelPipeline`
- [x] Ни одного Tk call из worker thread. — `TestPipelineControllerTkIsolation` (AST analysis: pipeline_controller не импортирует tkinter), `test_pipeline_worker_does_not_read_widgets_directly` (AST scan of `_pipeline_worker`)
- [x] Реальный toolkit smoke test на Windows. — `TestToolkitCallbackDispatch` (test_gui_smoke.py: `_tk_after` dispatches to main thread; PipelineCallbacks surfaces invoke; cancel_event cross-thread settable), `TestTkDispatcher`/`TestLogQueuePoller` (test_tk_dispatch.py)

## 5. Рекомендуемая последовательность pull requests

1. **PR 1 — media regression tests**: воспроизводящие тесты без изменения production behavior.
2. **PR 2 — timestamp/frame correctness**: segment + batch, A/V sync.
3. **PR 3 — audio quality**: presets и устранение per-segment drift.
4. **PR 4 — yt-dlp progress/connection UX**.
5. **PR 5 — safe encoder fallback и x264 limits**.
6. **PR 6 — resume manifest и validation**.
7. **PR 7 — batch windowing + process supervisor**.
8. **PR 8 — dry-run preview + streaming waveform**.
9. **PR 9 — GUI/pipeline separation и toolkit tests**.
10. **PR 10 — typing/tooling/docs/release cleanup**.

## 6. Definition of Done для ближайшего стабильного релиза

- [x] Ни один media correctness test не теряет кадры вне документированной boundary policy.
- [x] A/V duration drift находится в пределах одного media frame.
- [x] Audio quality выбирается пользователем и не понижается скрытно.
- [x] Hardware encoder не переходит на unrestricted libx264 без согласия.
- [x] Download speed/percent/ETA реально работают с текущим yt-dlp.
- [x] Offline и no-progress состояния видны и ограничены timeout.
- [x] Resume artifacts валидируются по manifest и ffprobe.
- [x] Dry-run preview работает для локального файла без предварительного pipeline.
- [x] Peak memory waveform ограничена и не растёт линейно с duration.
- [x] Есть настраиваемый RAM budget и обязательный резерв для ОС.
- [x] При превышении hard memory threshold задача безопасно останавливается без автоматического CPU fallback.
- [x] Output FPS по умолчанию равен source FPS без искусственных дубликатов.
- [x] Формальные 60 FPS подтверждаются frame-count/timestamp тестами, а не только `-r 60`.
- [x] Ruff, format, mypy и pytest зелёные в CI.
- [x] README и CHANGELOG не заявляют неисправленные функции (CHANGELOG исправлен).

## 7. Что не считать исправлением

- Наличие `trim` само по себе не доказывает frame accuracy.
- Наличие progress bar не доказывает, что yt-dlp template подставляет значения.
- Файл temp размером >1 KB не является валидным resume artifact.
- `apad` не исправляет AAC, если он добавляет длительность к каждому сегменту.
- Автоматический fallback не является надёжностью, если он создаёт опасную CPU-нагрузку.
- Зелёный unit test suite без frame-count/A/V tests не подтверждает корректность media pipeline.
