# stream2video — единый список проблем и план исправлений

> Ревизия архива `stream2video` v0.2 / Unreleased, 19 июля 2026.
>
> Документ объединяет исходный аудит и замечания двух дополнительных агентов. Спорные утверждения перепроверены по исходникам и локальному `yt-dlp 2026.07.04`. Для media pipeline также выполнен реальный тест через `ffmpeg`.

## Статус исполнения (обновление от 19 июля 2026)

Все пункты P0, P1, P2 и P3 выполнены и закоммичены. Media reproduction
тест проходит: на 6s/30FPS источнике с keep=[(0,2),(4,6)] оба метода
(`segment` и `batch`) дают 4.02s / 120 frames (ожидалось 4.00s / 120;
расхождение 0.02s — AAC encoder priming, в пределах одного кадра).
`ruff check`, `ruff format --check`, `mypy stream2video` (с
`check_untyped_defs=true`) и 357 unit/integration/媒体-correctness
тестов проходят зелёными.

| Пункт | Статус | Коммит |
| --- | --- | --- |
| P0.1 segment double-seek | ✅ | e861fcc |
| P0.2 setpts=N/FRAME_RATE/TB | ✅ | e861fcc |
| P0.3 audio_quality presets | ✅ | e861fcc |
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
| P2.1 gui.py monolith (2884 → 2514 строк; 7 модулей extracted: gui_widgets, gui_log_handler, gui_helpers, gui_settings, gui_platform, pipeline_controller skeleton, SubprocessRunner в utils) | ✅ частично | 993bdbf + c6e97a7 + bd1b802 + b0be872 + f4b62dd + 3c74e0c |
| P2.2 GUI/pipeline tests (pure helpers + settings I/O + SubprocessRunner + SilenceParser + 9 GUI widget smoke tests + pipeline config validation; run() state machine tests остаются known gap) | ⚠️ частично | c6e97a7 + bd1b802 + 1c7a11a + 3c74e0c |
| P2.3 media correctness regression tests (21 тест: CFR matrix 24/25/30/50/60, silence@start/end, 10 segments drift, audio_quality, output_fps=60, audio-less) | ✅ | 8184d08 |
| P2.4 shared SubprocessRunner (context manager: Popen + drain + cancel + cleanup) + 8 unit tests | ✅ | 4130d78 |
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

### Что НЕ сделано (намеренно отложено)

**Этап 10 (полный GUI refactor на `gui/` package)** — большой объём
работы по переносу ~2530 строк с круговой зависимостью. Сделан
**частичный** refactor: `_Tooltip` → `gui_widgets.py`, `QueueHandler`
→ `gui_log_handler.py`, pure-логика (CLI command builder, status
formatting, completion summary, throttle decision) → `gui_helpers.py`
с 32 unit-тестами, settings I/O → `gui_settings.py` с 13 unit-тестами,
platform helpers (dir_size_mb, open_in_file_manager) → `gui_platform.py`
с 7 unit-тестами, `SubprocessRunner` → `utils.py` с 8 unit-тестами,
`SilenceParser` → `silence.py`, `_run_final_concat` shared. 9 GUI
widget smoke tests instantiate Stream2VideoGUI без mainloop и
проверяют widgets/helpers wiring. Полный перенос `Stream2VideoGUI`
на package лучше делать отдельным PR после добавления pytest-qt.

**Тесты GUI (P2.2)** — partial gap closed. Pure-логика покрыта
(formatters, paths, waveform, gui_helpers, gui_settings, gui_platform,
SubprocessRunner, SilenceParser, media correctness), 9 widget smoke
tests инстанцируют GUI и проверяют widgets/helpers wiring. Для зрелого
решения стоит добавить pytest-qt или вынести pipeline controller в
чистый state machine (отдельный PR).


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

- [ ] Создать ветку `fix/media-correctness`.
- [ ] Поднять окружение через `uv sync --all-extras --dev` на Python 3.13.
- [ ] Сохранить вывод `ffmpeg -version`, `ffprobe -version`, `yt-dlp --version`.
- [ ] Запустить `ruff check .`, `ruff format --check .`, `mypy stream2video`, `pytest -v`.
- [ ] Зафиксировать все исходные failures отдельным baseline-файлом/CI artifact.
- [ ] Добавить текущий 6s/30FPS media reproduction как failing regression test.
- [ ] Не начинать архитектурный рефакторинг до наличия воспроизводящих тестов.

**Готово, когда:** известны точные lint/type/test failures, а потеря кадров воспроизводится автоматически в CI.

## Этап 1 — исправить временную шкалу и потерю кадров

### Segment path

- [ ] Удалить двойное применение seek.
- [ ] Использовать input-side fast seek только как coarse seek.
- [ ] Выполнять точную обрезку через `trim=start=...:duration=...`.
- [ ] Заменить `setpts=N/FRAME_RATE/TB` на `setpts=PTS-STARTPTS`.
- [ ] Для audio использовать `atrim=start=...:duration=...` + `asetpts=PTS-STARTPTS`.
- [ ] Удалить или заново обосновать `_AUDIO_PAD`.
- [ ] Не добавлять `dur+pad` к финальной длительности дорожки.
- [ ] Явно задать stream mapping.
- [ ] Корректно обработать вход без audio stream.

### Batch path

- [ ] Заменить `setpts=N/FRAME_RATE/TB` на timestamp-preserving схему.
- [ ] Определить корректную семантику CFR/VFR.
- [ ] Добавить подходящий `-fps_mode` и тесты на отсутствие frame drop/dup.
- [ ] Удалить лишний `[v][a]concat=n=1`, если он не выполняет полезной функции.
- [ ] Проверить границы `between(t,start,end)` на включение последнего кадра.

### Политика FPS и предложения со скриншотов

- [ ] Добавить `output_fps = source | 30 | 60`.
- [ ] Использовать `source` по умолчанию.
- [ ] Не добавлять принудительный `-r 60` в основной pipeline.
- [ ] Не использовать `-fps_mode cfr` для маскировки неправильных timestamps.
- [ ] Для режима `source` сохранять исходные PTS и cadence без frame duplication.
- [ ] Для явного CFR conversion использовать отдельный `fps=<target>` filter и предупреждать о дубликатах.
- [ ] Отдельно показывать пользователю output FPS и encoding throughput (`fps`/`speed`).
- [ ] Проверять через `ffprobe` `avg_frame_rate`, `r_frame_rate`, `nb_read_frames` и duration.
- [ ] Добавить детектор подозрительного результата: формальные 60 FPS при резком уменьшении уникальных/прочитанных кадров.
- [ ] Не принимать `libx264 -preset slow` как default: для проблемного CPU использовать `fast`/`veryfast` и ограничение threads.
- [ ] Рассматривать `-quality quality` для AMF только как quality preset, а не как исправление FPS.
- [ ] Использовать `-avoid_negative_ts make_zero` только для нормализации контейнера после исправления PTS.
- [ ] Не применять `aresample=async=1` для сокрытия крупного A/V drift; допускать только ограниченную компенсацию после исправления timestamps.
- [ ] Не фиксировать `-ar 48000` без явной политики resampling.
- [ ] Проверить AMF/NVENC/libx264 на одном и том же корректном frame stream.

### Acceptance tests

- [ ] 24, 25, 29.97, 30, 50 и 60 FPS.
- [ ] VFR sample с сохранением timestamps.
- [ ] Сегменты у `t=0`, около keyframe, между keyframes и в конце файла.
- [ ] 1, 2, 10 и 100 keep segments.
- [ ] `abs(video_duration - expected) <= 1 frame`.
- [ ] `abs(audio_duration - video_duration) <= max(video_frame, AAC_frame)`.
- [ ] Frame count соответствует ожидаемому в пределах документированной boundary policy.
- [ ] Нет прогрессирующего A/V drift.

**Готово, когда:** оба метода проходят media matrix и исходный reproduction даёт около 4 секунд/120 кадров без рассинхронизации.

## Этап 2 — качество звука

- [ ] Добавить `audio_quality` в `CONFIG_DEFAULTS` и validation.
- [ ] Добавить presets, например `high=256k`, `medium=192k`, `low=128k`.
- [ ] Добавить CLI `--audio-quality` и при необходимости `--audio-bitrate`.
- [ ] Добавить GUI combobox и tooltip.
- [ ] Передавать audio options в segment, batch и fallback paths.
- [ ] Сохранять sample rate и channel layout либо явно документировать conversion.
- [ ] Исследовать один encode после общей нарезки вместо AAC encode на каждом сегменте.
- [ ] Проверить AAC priming/gapless metadata и стыки на щелчки.
- [ ] Добавить тест: output bitrate не ниже выбранного preset.
- [ ] Добавить спектральный/сигнальный smoke-test на пропуски и вставленную тишину в местах склейки.

**Готово, когда:** пользователь выбирает качество audio, 192k source не понижается до 128k без явного выбора, длительность audio совпадает с video.

## Этап 3 — безопасный encoder fallback и libx264

- [ ] Добавить policy `software_fallback = ask | disabled | enabled`.
- [ ] По умолчанию в GUI использовать `ask` или `disabled`.
- [ ] Не запускать libx264 автоматически после ошибки HW encoder без видимого уведомления.
- [ ] Добавить `x264_preset` (`ultrafast`…`medium`).
- [ ] Добавить `encoder_threads`/`x264_threads`; безопасный default — часть доступных cores.
- [ ] Ставить output-опцию `-threads N` после `-c:v libx264` и до output path; `-threads` перед `-i` может ограничить декодер, а не x264 encoder.
- [ ] Добавить тест generated command, который проверяет область действия и позицию `-threads`.
- [ ] Добавить `cpu_limit_percent`/preset `Low CPU`; пересчитывать мягкий лимит в число encoder threads с учётом logical CPU.
- [ ] Не обещать точный процент нагрузки только через `-threads`: decode, filters, audio и служебные потоки FFmpeg могут использовать дополнительные cores.
- [ ] Использовать `ultrafast`/`veryfast` для low-CPU режима, явно показывая компромисс: заметно больший файл или худшее качество при том же bitrate.
- [ ] Не считать `-r 60`, `-g 60` и `aresample=async=1` средствами ограничения CPU: они решают другие задачи и могут добавить работу/дубликаты кадров.
- [ ] Для настоящего hard CPU cap рассмотреть Windows Job Object CPU rate control и Linux cgroup `CPUQuota`; process priority сама по себе не ограничивает процент CPU.
- [ ] Показывать текущую загрузку и peak CPU процесса, а не только выбранное число threads.
- [ ] Рассмотреть lower process priority на Windows как отдельную opt-in настройку.
- [ ] Показывать фактический encoder, а не только первоначально выбранный.
- [ ] Записывать fallback reason в GUI и log.
- [ ] Реально smoke-test `libx264`, а не возвращать `True` без запуска.
- [ ] Добавить короткий load test с выбранными preset/threads.
- [ ] Сравнить `ultrafast/veryfast/fast/medium` при 1, 2, 4 и auto threads: CPU%, peak RAM, speed, размер и качество.
- [ ] Проверить low-CPU preset на 4/8/16/32 logical CPUs: целевой процент должен подтверждаться измерением, а не предполагаться по одному ПК.
- [ ] Пересмотреть NVENC rate-control presets и покрыть тестами generated command.

**Готово, когда:** выбор AMF не может незаметно перейти в unrestricted libx264, а software encode имеет управляемую нагрузку.

## Этап 4 — исправить download progress и connection UX

- [ ] Исправить поля template на `progress.*`.
- [ ] Использовать `total_bytes` с fallback на `total_bytes_estimate`.
- [ ] Добавить parser tests с реальными строками текущего yt-dlp.
- [ ] Добавить локальный fake HTTP server integration test без внешней сети.
- [ ] Изменить `best` на `bestvideo+bestaudio/best` с осознанной container policy.
- [ ] Убрать fallback, нарушающий resolution cap, либо явно предупреждать о нём.
- [ ] Показывать стадии `Resolving`, `Connecting`, `Waiting`, `Downloading`, `Merging`.
- [ ] Хранить timestamp последнего progress event.
- [ ] Через 20–30 секунд без данных показывать warning.
- [ ] Добавить `connection_timeout`, `no_progress_timeout`, `download_timeout`.
- [ ] Показывать retry count и последнюю сетевую ошибку без утечки чувствительных URL/cookies.
- [ ] Вынести presentation model progress в pure helper для CLI и GUI.

**Готово, когда:** percent/speed/ETA проверены интеграционным тестом, а offline/stall видимы раньше 8 часов.

## Этап 5 — resume integrity

- [ ] Создать manifest для segment/batch working dir.
- [ ] Включить source identity: canonical path, size, mtime_ns; опционально quick hash.
- [ ] Включить hash keep segments.
- [ ] Включить method, encoder, encoder opts, video/audio quality и pipeline version.
- [ ] При mismatch очищать working dir и кодировать заново.
- [ ] Валидировать каждый resumed file через `ffprobe`.
- [ ] Проверять codec, streams, duration и container validity, а не только размер.
- [ ] Писать temp output и атомарно переименовывать после успешного encode.
- [ ] Добавить CLI resume cache для silence detection.
- [ ] Удалять resume cache только после успешной записи final cache.
- [ ] Добавить тесты на смену encoder/quality/threshold/source после crash.

**Готово, когда:** ни один artifact от несовместимого запуска не может попасть в новый output.

## Этап 6 — batch performance и process supervision

- [ ] Для каждого chunk вычислять минимальное временное окно.
- [ ] Добавить coarse input `-ss` и ограничение decode window.
- [ ] Пересчитывать `between(t,...)` относительно window start.
- [ ] Добавить небольшой keyframe safety margin.
- [ ] Измерить correctness до оптимизации и после неё.
- [ ] Заменить блокирующий `readline()` на queue/select/thread supervision.
- [ ] Проверять deadline и no-progress независимо от новых строк.
- [ ] Разделить overall timeout и stall timeout.
- [ ] Сделать timeout на segment/chunk пропорциональным duration либо отключаемым при живом progress.
- [ ] Добавить тест зависшего fake subprocess.
- [ ] Добавить benchmark на 1h/6h synthetic input и несколько chunk counts.

**Готово, когда:** каждый chunk декодирует только своё окно, а зависший ffmpeg гарантированно завершается по watchdog.

## Этап 7 — silence correctness и preview

- [ ] Синхронизировать defaults `detect_silence()` с `CONFIG_DEFAULTS`.
- [ ] Не дублировать числовые defaults; импортировать canonical values.
- [ ] Закрывать trailing `silence_start` известной media duration.
- [ ] Использовать `_to_float()` во всех parser paths.
- [ ] Свести все silencedetect parsers к одному state machine.
- [ ] Подключить `detect_silence_stream` к waveform popup при отсутствии cache/live data.
- [ ] Добавить отмену preview subprocess.
- [ ] Повторно запускать detect при изменении threshold/min_silence/margin с debounce.
- [ ] Для длинных файлов добавить selectable sample window или explicit full-scan mode.
- [ ] Показывать список cut/keep intervals и итоговую ожидаемую duration.
- [ ] Не писать final pipeline cache из preview без явного решения пользователя.
- [ ] Добавить тесты decimal comma, trailing silence, no audio и URL-disabled preview.

**Готово, когда:** локальный файл можно оценить до pipeline, overlay соответствует будущей нарезке, trailing silence не теряется.

## Этап 8 — память waveform

- [ ] Заменить `stdout.read()` на chunked reading.
- [ ] Делать downsample онлайн в фиксированное число buckets.
- [ ] Ограничить память независимо от duration.
- [ ] Корректно завершать ffmpeg при закрытии popup или новом render token.
- [ ] Показывать progress/elapsed во время долгого preview decode.
- [ ] Добавить memory regression test для 6–8 часов synthetic audio.

**Готово, когда:** waveform длинного стрима не требует сотен MB/GB RAM.

## Этап 8A — ограничение RAM/VRAM и защита системы от зависания

### Архитектурные ограничения

- [ ] Добавить настройку `memory_limit_mb` с режимами `auto` и ручным значением.
- [ ] Добавить `memory_reserve_mb` — память, которую приложение никогда не занимает; безопасный default 2–4 GB в зависимости от объёма RAM.
- [ ] В режиме `auto` выделять pipeline не более 50–65% доступной памяти на момент старта.
- [ ] Не запускать новый тяжёлый этап, если после его старта прогнозируемый reserve будет нарушен.
- [ ] Разрешать не более одного encode/decode ffmpeg-процесса по умолчанию.
- [ ] Не запускать full waveform decode параллельно с encode без отдельного разрешения.
- [ ] Уменьшить `_BATCH_CHUNK_SIZE` или сделать его динамическим.
- [ ] При memory pressure автоматически переходить `batch → segment` либо предлагать перезапуск.
- [ ] Избегать filter graph, который одновременно буферизует много branches/segments.
- [ ] Хранить промежуточные данные в temp files, а не в RAM.
- [ ] Выполнять waveform downsample потоково с фиксированным количеством buckets.

### Снижение памяти encoder

- [ ] Ограничить output encoder через `-c:v libx264 -threads N`; не размещать эту опцию до `-i`, где она может относиться к input decoder.
- [ ] Меньше encoder threads обычно снижает CPU и число frame buffers, но не является строгим лимитом общей RAM/CPU всего FFmpeg.
- [ ] Использовать `fast`/`veryfast` как безопасный default вместо `slow`; `ultrafast` оставить отдельным low-CPU/fast preset с предупреждением о размере файла.
- [ ] Добавить отдельный low-memory x264 preset с уменьшенными lookahead/refs/B-frames после проверки качества.
- [ ] Рассмотреть параметры `rc-lookahead`, `ref`, `bf` только через протестированные presets, а не как произвольные flags.
- [ ] Для hardware encoder ограничить число async surfaces/lookahead, если конкретный backend это поддерживает.
- [ ] Мониторить не только RAM, но и VRAM; при заполнении VRAM драйвер может начать paging в системную память.
- [ ] Не считать снижение process priority ограничением RAM: priority помогает отзывчивости CPU, но не ограничивает память.
- [ ] Не считать `-max_alloc` полным memory limit: использовать его только как защиту от одной аномально большой аллокации.

### Runtime monitoring

- [ ] Добавить монитор RSS ffmpeg и Python-процесса, например через `psutil` или платформенный backend.
- [ ] Отслеживать `available RAM`, swap/pagefile activity и скорость роста RSS.
- [ ] Ввести soft threshold, например 80% пользовательского budget: warning + запрет новых параллельных задач.
- [ ] Ввести hard threshold, например 95% budget или нарушение OS reserve: корректная отмена текущего этапа.
- [ ] Не ставить процесс на бесконечную паузу при нехватке RAM: pause не освобождает уже занятую память.
- [ ] После hard stop сохранять только валидированные resume artifacts.
- [ ] Показывать в GUI `RAM: current / limit`, peak RSS и причину остановки.
- [ ] Записывать peak RAM/VRAM в итоговый log.
- [ ] Добавить watchdog, который остаётся отдельным от контролируемого ffmpeg-процесса.

### Ограничения средствами ОС

- [ ] На Windows рассмотреть Job Object с process/job memory limit и kill-on-job-close.
- [ ] На Linux поддержать опциональный запуск в systemd scope/cgroup v2 с `MemoryHigh`/`MemoryMax`.
- [ ] Для portable Linux без systemd рассмотреть `prlimit`/`RLIMIT_AS`, понимая, что FFmpeg завершится с allocation error.
- [ ] На macOS использовать мониторинг RSS и graceful termination; документировать отсутствие эквивалентного простого CLI hard limit.
- [ ] Обрабатывать OS-level OOM/kill как отдельную ошибку, не как обычный encoder failure и не запускать после неё libx264 fallback.

### Проверки и UX

- [ ] Выполнить stress tests на машинах/VM с 4, 8, 16 и 32 GB RAM.
- [ ] Проверить 6–8-часовое видео, 4K60, VFR и большое число сегментов.
- [ ] Проверить, что при достижении hard threshold GUI остаётся отзывчивым.
- [ ] Проверить, что после memory stop система сохраняет reserve и не уходит в интенсивный swap/pagefile thrashing.
- [ ] Добавить presets `Low memory`, `Balanced`, `Maximum performance`.
- [ ] Для каждого preset показывать ожидаемый компромисс скорости, качества, CPU и RAM.
- [ ] Добавить preset `Low CPU`: output-side threads limit + `veryfast`/`ultrafast`, без принудительного `-r 60` и без маскировки A/V ошибок через `aresample`.
- [ ] Безопасный default: один ffmpeg, streaming waveform, `segment`, ограниченные x264 threads, сохранённый OS reserve.

**Готово, когда:** приложение не может бесконтрольно занять всю доступную RAM, при memory pressure останавливает только текущую задачу, сохраняет отзывчивость GUI и оставляет ОС заданный резерв.

## Этап 9 — thread safety и cancellation

- [ ] Снимать `delete_after` и все widget values в main thread.
- [ ] Передавать immutable pipeline config dataclass в worker.
- [ ] Убрать Tk calls из worker, использовать только dispatcher/queue.
- [ ] Заменить глобальный `_active_proc` registry на scoped process supervisor.
- [ ] Поддержать несколько process handles с owner/task IDs.
- [ ] При cancel завершать только процессы нужного pipeline/preview.
- [ ] В CLI отдельно ловить `CancelledError`.
- [ ] Ввести общий `PipelineCancelled` л��бо единый cancellation protocol.
- [ ] Добавить тесты: preview+pipeline одновременно, cancel, close window, callback raises.

**Готово, когда:** параллельный preview не мешает отмене pipeline, worker не читает Tk widgets.

## Этап 10 — архитектурный рефакторинг

- [ ] Создать `gui/` package.
- [ ] Вынести `main_window.py` — только layout и wiring.
- [ ] Вынести `pipeline_controller.py` — state machine без Tk.
- [ ] Вынести `waveform_window.py`.
- [ ] Вынести `recent_projects.py`.
- [ ] Вынести `settings.py`.
- [ ] Вынести `progress_model.py` для CLI/GUI.
- [ ] Создать общий subprocess runner/supervisor.
- [ ] Создать общий concat finalizer и resume validator.
- [ ] Убрать import-time `logging.basicConfig()` из `cli.py`.
- [ ] Перевести широкие silent exceptions на узкие исключения + debug logging.
- [ ] Удалить дубли и мёртвые doc refs.

**Готово, когда:** pipeline тестируется без Tk, а GUI-модули имеют понятные границы ответственности.

## Этап 11 — typing, lint и CI

- [ ] Выбрать официальную минимальную версию Python: 3.13-only или `>=3.11`.
- [ ] Синхронизировать `requires-python`, `.python-version`, CI, Ruff, mypy, README и CHANGELOG.
- [ ] Запустить зафиксированный Ruff и проверить, существует ли актуальный `UP038` failure.
- [ ] Исправить реальные Ruff errors.
- [ ] Запустить `ruff format` и закоммитить только formatter diff.
- [ ] Включить `check_untyped_defs=true`.
- [ ] Постепенно включать `disallow_incomplete_defs` и `disallow_untyped_defs` для core modules.
- [ ] Типизировать pipeline config, callbacks и subprocess results.
- [ ] Добавить Windows CI job для MediaFoundation/Tk lifecycle smoke tests, где возможно.
- [ ] Добавить CI media artifacts/ffprobe diagnostics при падении.
- [ ] Удалить устаревшие комментарии и повтор `eta_s`.

**Готово, когда:** локальные команды и CI зелёные на выбранной Python policy, а typing реально проверяет core pipeline.

## Этап 12 — документация и release

- [ ] Обновить README по фактическим encoder/audio/download presets.
- [ ] Документировать fallback policy и CPU risk.
- [ ] Документировать exact frame-boundary policy.
- [ ] Документировать resume invalidation.
- [ ] Исправить CHANGELOG, который преждевременно заявляет py313 targets и fixes.
- [ ] Добавить troubleshooting для overclocked CPU, temperatures и PSU instability без обещаний, что software limit исправит hardware instability.
- [ ] После стабилизации рассмотреть PyPI package/release artifacts.
- [ ] Опционально добавить Docker только для CLI/integration testing; GUI Docker не является приоритетом.

## 4. Обязательная тестовая матрица перед релизом

### Media

- [ ] CFR: 24/25/29.97/30/50/60 FPS.
- [ ] VFR.
- [ ] AAC, Opus и MP3 input audio.
- [ ] Mono/stereo/5.1.
- [ ] Без audio stream.
- [ ] Несколько audio streams.
- [ ] Сегменты короче 0.5 с и длиннее 1 часа.
- [ ] 0, 1, 2, 40, 41 и 100 silence segments.
- [ ] Silence в начале, середине и до EOF.
- [ ] Broken/non-zero timestamps.

### Download

- [ ] `best`, 1080p, 720p, 480p, 360p.
- [ ] `total_bytes` и `total_bytes_estimate`.
- [ ] Неизвестный total.
- [ ] Нулевая/неизвестная speed на старте.
- [ ] Offline, DNS failure, timeout, retry, stalled connection.
- [ ] Cancel во время download и merge.

### Resume/failure

- [ ] Crash посреди segment.
- [ ] Crash посреди batch.
- [ ] Смена encoder/quality после crash.
- [ ] Замена source с тем же filename.
- [ ] Corrupt/missing-moov temp file.
- [ ] Cancel и повторный запуск CLI/GUI.

### GUI/threading

- [ ] Preview до pipeline.
- [ ] Preview одновременно с pipeline.
- [ ] Закрытие popup во время decode.
- [ ] Закрытие приложения во время каждого этапа.
- [ ] Ни одного Tk call из worker thread.
- [ ] Реальный toolkit smoke test на Windows.

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

- [ ] Ни один media correctness test не теряет кадры вне документированной boundary policy.
- [ ] A/V duration drift находится в пределах одного media frame.
- [ ] Audio quality выбирается пользователем и не понижается скрытно.
- [ ] Hardware encoder не переходит на unrestricted libx264 без согласия.
- [ ] Download speed/percent/ETA реально работают с текущим yt-dlp.
- [ ] Offline и no-progress состояния видны и ограничены timeout.
- [ ] Resume artifacts валидируются по manifest и ffprobe.
- [ ] Dry-run preview работает для локального файла без предварительного pipeline.
- [ ] Peak memory waveform ограничена и не растёт линейно с duration.
- [ ] Есть настраиваемый RAM budget и обязательный резерв для ОС.
- [ ] При превышении hard memory threshold задача безопасно останавливается без автоматического CPU fallback.
- [ ] Output FPS по умолчанию равен source FPS без искусственных дубликатов.
- [ ] Формальные 60 FPS подтверждаются frame-count/timestamp тестами, а не только `-r 60`.
- [ ] Ruff, format, mypy и pytest зелёные в CI.
- [ ] README и CHANGELOG не заявляют неисправленные функции.

## 7. Что не считать исправлением

- Наличие `trim` само по себе не доказывает frame accuracy.
- Наличие progress bar не доказывает, что yt-dlp template подставляет значения.
- Файл temp размером >1 KB не является валидным resume artifact.
- `apad` не исправляет AAC, если он добавляет длительность к каждому сегменту.
- Автоматический fallback не является надёжностью, если он создаёт опасную CPU-нагрузку.
- Зелёный unit test suite без frame-count/A/V tests не подтверждает корректность media pipeline.
