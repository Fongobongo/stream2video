# stream2video — план пайплайна

**Цель:** автоматически сжимать видеозаписи стримов (Twitch/YouTube), оставляя только моменты с речью, и генерировать к ним субтитры. Личный CLI-инструмент.

**Статус:** Phase 1 MVP — реализован, **shipped**
**Дата:** 2026-05-25 (draft), 2026-06-02 (Phase 1 завершён)
**Режим:** Builder (личный pet project)

---

## 1. Проблема

Записи стримов длинные (3-8 ч), реальной полезной речи в них 30-50%. Нужен инструмент, который:

**Фаза 1 (MVP — реализуем сейчас):**
1. Скачивает VOD с Twitch / видео с YouTube.
2. Детектирует паузы и обрезает тишину.
3. Склеивает куски со звуком в одно видео.

**Фаза 2 (STT — реализуем позже):**
4. Вырезает филлеры («эээ», «нуу», «uh»).
5. Генерирует субтитры (.srt).
6. LLM-фильтр малозначимых кусков.

## 2. Подтверждённые премисы

- ✅ **Аудио — главный сигнал.** Режем агрессивно всю тишину.
- ✅ **Форма: CLI-утилита** с флагами.
- ✅ **Стек Фазы 1:** `yt-dlp` + `ffmpeg silencedetect` + Python CLI (`typer`). STT/LLM зависимости — Фаза 2.
- ✅ **GPU нет** — CPU-only. Hardware encoder (NVENC/AMF/MediaFoundation) опционален, есть fallback на libx264.
- ⚠️ **Live vs VOD:** только VOD (готовая запись). Live capture вне scope.

> **Отступление от плана (2026-06-02):** `auto-editor` заменён на прямой вызов `ffmpeg silencedetect`. Причины: меньше зависимостей, проще тестировать (один subprocess), и нет STT-оверхеда для Phase 1. STT-резаный filler-cut в Phase 2 будет использовать faster-whisper/deepgram.

## 3. Архитектура

### Фаза 1 — MVP (silence-cut) — ✅ реализовано

```

## 4. Стек / зависимости

### Фаза 1 (MVP) — ✅ реализовано

| Компонент | Назначение | Установка |
|---|---|---|
| Python 3.11+ | runtime | system / `pyenv` |
| `yt-dlp` | загрузка Twitch/YouTube VOD | `pip install yt-dlp` |
| `ffmpeg` + `ffprobe` | silence detection + cut | системный пакет / winget |
| `typer` | CLI | `pip install typer` |
| `pyyaml` | config file parsing | `pip install pyyaml` |
| `rich` | progress bars + logging | `pip install rich` |
| `customtkinter` | GUI (опционально) | `pip install stream2video[gui]` |

### Системные требования Phase 1

- **RAM:** `ffmpeg silencedetect` stream-processes audio (низкое потребление). `batch` метод фильтрует видео покадрово через `select/aselect` — пик:
  - 1h video (720p) → ~1–2 GB peak RAM
  - 6h video (720p) → ~4–6 GB peak RAM
  - **Recommendation:** ≥8 GB RAM для 6h стримов. `segment` метод кодирует по сегменту независимо — пиковое RAM как у одного сегмента.
- **Disk:** ~2× size of original video (input + temp files in `_{stem}_segments/` для `segment` метода). Cleanup on success/error.
- **CPU:** `libx264` — CPU-bound, 6h видео ~3–6ч realtime на современном CPU. `h264_nvenc`/`h264_amf`/`h264_mf` — 5–20× быстрее на совместимом GPU.

### Фаза 2 (STT — позже)

| Компонент | Назначение |
|---|---|
| `faster-whisper` | транскрипция на CPU, word-level timestamps |
| `deepgram-sdk` | Deepgram API ($200 кредит ~55 стримов) |
| `openai` SDK | Groq/Gladia/OpenAI API |
| `anthropic` / `openai` | LLM content filter |

**Сравнение STT для 6ч стримов (на будущее):**

| Сервис | Free tier | Для 6ч стрима | Лимит |
|---|---|---|---|
| Local `medium` | ∞ бесплатно | ~3-6ч обработки на CPU | нет |
| Groq | 7200 **сек**/день = 120 мин | ❌ не влезает | 25 MB/файл |
| Deepgram | $200 кредит (~55 стримов) | ✅ быстро | 2 GB |
| Gladia | 10 ч/мес навсегда | ✅ ~1 стрим/мес | — |

## 5. CLI-интерфейс

### Фаза 1 (MVP) — ✅ реализовано

```bash
stream2video URL [options]

# Примеры:
stream2video https://twitch.tv/videos/12345
stream2video https://youtube.com/watch?v=abc --threshold -30 --min-silence 0.5
stream2video ./local.mp4 --output ./out/
stream2video video.mp4 --encoder h264_nvenc --method segment

# Флаги:
  -o, --output DIR        выходная директория (default: ./compressed_videos/)
  -m, --method METHOD     segment | batch (default: batch)
  -e, --encoder ENCODER   h264_nvenc | h264_amf | h264_mf | libx264 (default: libx264)
  -c, --config FILE       YAML-конфиг (threshold, min_silence, margin, ...)
  -f, --force             re-detect silence, игнорировать кеш
  -l, --log-level LEVEL   DEBUG | INFO | WARNING | ERROR (default: INFO)
```

**Параметр validation (в коде — `config.py`):**
- threshold: [-60, -5] dB (вне диапазона → exit 1)
- min-silence: [0.1, 60] сек
- margin: **[-3, 5]** сек (расширили вниз до -3, чтобы можно было **расширять** silence через отрицательный margin — режет больше)

**Config file поддержка (YAML/JSON):**
```yaml
# ~/.config/stream2video/config.yaml или ./stream2video.yaml
threshold: -30
min_silence: 1.0
margin: 0.2
output: ~/Videos/stream-cuts/

presets:
  aggressive:
    threshold: -25
    min_silence: 0.3
  gentle:
    threshold: -40
    min_silence: 2.0
```

### Фаза 2 (STT — позже)

```bash
# Дополнительные флаги появятся в Фазе 2:
  --edit MODE             silence|filler|content (Ф2 дефолт: filler)
  --transcriber MODE      local|groq|deepgram|gladia
  --model NAME            whisper-модель (tiny|base|small|medium|large-v3)
  --lang CODE             язык транскрипта (ru/en/auto)
  --llm-provider NAME     для --edit content: anthropic|openai
  --no-subs               пропустить генерацию субтитров
  --filler-words LIST     custom список филлеров
  --burn-subs             встроить субтитры в видео
```

## 5a. Error Handling Strategy (Phase 1) — ✅ реализовано

Все ошибки именованы, залогированы + дают пользователю actionable message. Структурированное логирование через `rich.logging`.

| Метод | Может пойти не так | Exception class | Обработка | Пользователь видит |
|---|---|---|---|---|
| `cli.download()` | Неверный URL | `URLValidationError` | reject at parse | "Invalid URL: must be http(s)" |
| | Download timeout | `DownloadTimeoutError` | exit 1 | "Download timeout after Xs" |
| | Video недоступна | `VideoNotAvailableError` | exit 1 | "Video not available" |
| | Disk full | `DiskSpaceError` | exit 1 | "Insufficient disk space" |
| | No write perm | `PermissionDeniedError` | exit 1 | "Permission denied" |
| | User cancel | `DownloadCancelledError` | exit 130 | "Download cancelled." |
| `cli.detect_silence()` | ffmpeg not in PATH | `SilenceDetectionError` | exit 1 + hint | "ffmpeg not found in PATH" |
| | ffmpeg crash | `SilenceDetectionError` | exit 1 + stderr | "ffmpeg silencedetect failed: [stderr]" |
| | Param out of range | `ValueError` | exit 1 | "Threshold must be in range [-60, -5], got -61" |
| | User cancel | `SilenceDetectionError("cancelled")` | exit 130 | "silence detection cancelled" |
| `cli.cut_and_concat()` | ffmpeg not in PATH | `FFmpegError` | exit 1 + hint | "ffmpeg not found in PATH" |
| | ffmpeg crash | `FFmpegError` | **fallback to libx264**, retry once | "h264_nvenc failed; falling back to libx264" |
| | No keep segments | `ConcatError` | exit 1 | "No video segments to keep after removing silence" |
| | User cancel | `CancelledError` | exit 130 | "ffmpeg cancelled" |
| All paths | Orphaned temp files | (all) | cleanup in `finally` block | (none — silent cleanup) |

**Retry strategy:**
- **yt-dlp timeout:** fail-fast (без retry) — слишком шумно для личного инструмента
- **ffmpeg encoder crash (cut_and_concat):** retry 1× с fallback на libx264 (через `_with_libx264_fallback`)
- **Other errors:** fail fast, no retry

**Logging:**
- All exceptions logged with full context (args, stacktrace)
- Логи в `{output_dir}/stream2video.log` (file handler + rich stderr)
- GUI additionally: log queue → textbox widget

> **Отступление от плана:** retry yt-dlp с 5s backoff убран — при сетевых проблемах пользователю проще перезапустить вручную, чем ждать двойной timeout.

---

## 6. Чек-листы по этапам

### Этап 0 — Подготовка окружения — ✅

- [x] Установить Python 3.11+ и проверить `python --version`
- [x] Установить `ffmpeg` (`ffmpeg -version`)
- [x] Создать venv: `python -m venv .venv`
- [x] Создать `pyproject.toml`
- [x] Установить: `pip install -e .[gui,dev]`
- [x] Создать структуру проекта (Фаза 1):
  ```
  stream2video/
  ├── pyproject.toml
  ├── README.md
  ├── PLAN.md
  ├── run_gui.cmd        # portable Windows launcher
  ├── setup.ps1          # cross-platform setup
  ├── stream2video/
  │   ├── __init__.py
  │   ├── cli.py         # typer CLI
  │   ├── gui.py         # customtkinter GUI
  │   ├── config.py      # defaults + validation ranges
  │   ├── utils.py       # shared helpers
  │   ├── download.py    # yt-dlp wrapper (cancellable)
  │   ├── silence.py     # ffmpeg silencedetect + cache
  │   └── concat.py      # cut+concat (segment/batch methods, fallback)
  ├── tests/             # 60 unit + integration tests
  └── _portable/         # gitignored: venv + ffmpeg + settings.json
  ```
  *(Phase 2 добавит: `transcribe.py`, `fillers.py`, `content.py`, `subtitles.py`)*

### Этап 1 — Загрузка видео (`download.py`) — ✅

- [x] Функция `download(url: str, out_dir: Path, cancel_callback) -> DownloadResult`
- [x] Через `subprocess` `python -m yt_dlp` (НЕ Python API — для cancellability):
  - `format=best[ext=mp4]/best`
  - `outtmpl={out_dir}/%(id)s.%(ext)s`
  - `--no-warnings --no-progress --print after_move:filepath`
- [x] Поддержка локальных файлов (если input — путь, не URL — passthrough)
- [x] Обработка ошибок: 6 типизированных исключений (см. §5a) + _classify_error по stderr
- [x] Cancelable через `cancel_callback` (читает `_active_proc` для GUI)
- [x] Тесты: URL validation, local file passthrough, cancel aborts, classify_error, find_downloaded_file

### Этап 2 — Silence detection (`silence.py`) + cut/concat (`concat.py`) — ✅

- [x] Функция `detect_silence(video_path, threshold, min_silence, margin, ...) -> List[SilenceSegment]`
- [x] **Решение:** `ffmpeg silencedetect` filter через subprocess (НЕ auto-editor, см. §2 отступление). Причина: меньше зависимостей, проще тестировать, нет STT-оверхеда для Phase 1.
  ```
  ffmpeg -progress pipe:1 -i input.mp4 \
    -af silencedetect=noise={10^(threshold/20)}:duration={min_silence} \
    -f null -
  ```
- [x] Захват stderr парсится регулярками (`silence_start`/`silence_end`)
- [x] Margin apply + merge overlapping segments (`_apply_margin`)
- [x] Кеш в `{output_dir}/{stem}_silence_cache.json` (по `(threshold, min_silence, margin)`, mtime check)
- [x] `cut_and_concat()` с двумя методами:
  - `segment` (per-segment encode + concat demuxer, ~1.5ч на 6ч)
  - `batch` (select/aselect filter, frame-exact, ~6-7ч)
- [x] Hardware encoder support: NVENC / AMF / MediaFoundation + auto-fallback на libx264
- [x] Cancelable через polling (0.5s) во всех ffmpeg-вызовах
- [x] Cleanup: `_{stem}_segments/` temp dir с `shutil.rmtree(ignore_errors=True)` в finally
- [x] Тесты: `_apply_margin` edge cases, validation, `generate_keep_segments` (8 cases)

### Этап 7 — CLI и связка (`cli.py`) — ✅

- [x] `typer.Typer()` app
- [x] Parameter validation: threshold [-60...-5], min-silence [0.1...60], margin [-3...5] (см. §5)
- [x] Config file: YAML через `--config` (грузится в `load_config`, мержится поверх defaults)
- [x] Флаги: `--threshold`, `--min-silence`, `--margin` берутся из config, не из CLI (Phase 1 не имеет CLI-флагов для них — устанавливаются только через config)
- [x] Метод + encoder: `--method` (`segment`|`batch`), `--encoder` (4 варианта) — из CLI
- [x] Pipeline orchestration: validate → download → silence-detect (с кешем) → cut+concat
- [x] Progress bars (`rich.progress`): 3 steps с progress %
- [x] Structured logging: `rich.logging` в stderr + file handler в `{output_dir}/stream2video.log`
- [x] Error messages: actionable, exit code 1 для ошибок, 130 для cancel
- [x] Точка входа в `pyproject.toml`: `stream2video = "stream2video.cli:app"`, `stream2video-gui = "stream2video.gui:main"`

### Этап 7a — GUI (`gui.py`) — ✅

- [x] `customtkinter` desktop UI, cross-platform
- [x] Поля: input (file/URL), output dir, sliders (threshold/min_silence/margin), method/encoder combo, force checkbox
- [x] Кнопка "Test encoder" → вызывает `concat.check_encoder()` (НЕ дублирует логику)
- [x] Progress bar + log textbox (через `QueueHandler` + `log_queue`)
- [x] Cancel button: устанавливает `_cancel_event` → передаётся в `cancel_callback` всех subprocess
- [x] Theme: dark/light/system (customtkinter), сохраняется в settings.json
- [x] "Copy CLI command" копирует эквивалентный CLI-вызов в clipboard
- [x] Persistent settings в `_portable/settings.json` (или `gui_settings.json`)

### Этап 8 — Тесты и edge cases — ✅ (частично)

- [x] **60 unit + integration tests** (pytest, mocks)
- [x] `tests/test_download.py` — URL validation, local file passthrough, cancel abort, classify_error mapping, find_downloaded_file glob fallback
- [x] `tests/test_silence.py` — `SilenceSegment`, `_apply_margin` edge cases, validation
- [x] `tests/test_integration.py` — `generate_keep_segments` (8 cases: clamp/drop/merge/empty)
- [x] **НЕ покрыто тестами** (документировано как known gap):
  - Реальный `ffmpeg` вызов (нет CI-инфраструктуры, мокать subprocess.Popen — overengineering)
  - GUI (нет pytest-qt / tkinter-тестов)
  - end-to-end с реальным видео (требует ffmpeg + большой файл)

### Этап 9 — Документация и релиз — ✅

- [x] `README.md`: install, usage, examples, troubleshooting
- [x] `--help` typer-generated (CLI), кнопка "Copy CLI command" в GUI
- [x] Portable mode: `run_gui.cmd` сам ставит Python + ffmpeg в `_portable/`
- [x] `setup.ps1` cross-platform: deps + ffmpeg + launch
- [ ] (опц.) PyPI publish — **отложено** (личный инструмент, не нужно)
- [ ] (опц.) Docker — **отложено**

---

### Этап 3 — Транскрипт с word-level timestamps (`transcribe.py`) — **ФАЗА 2**

- [ ] Функция `transcribe(audio: Path, model: str, lang: str, transcriber: str) -> list[Word]`, где `Word = {text, start, end}`
- [ ] Извлечь аудио из видео: `ffmpeg -i input.mp4 -vn -ar 16000 -ac 1 audio.wav`

**Режим `local` (дефолт, `--transcriber local`):**
- [ ] Загрузить модель: `WhisperModel(model, device="auto", compute_type="auto")`
- [ ] Запустить: `segments, info = model.transcribe(audio_path, word_timestamps=True, language=lang)`
- [ ] Собрать flat list слов из `segment.words`

**Режим `groq` (`--transcriber groq`):**
- [ ] ⚠️ **Лимит free tier: 7200 сек/день = 120 мин.** 6-часовой стрим не влезает. Groq полезен только для коротких видео (<2 ч) или платного tier.
- [ ] `client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")`
- [ ] `client.audio.transcriptions.create(model="whisper-large-v3", file=audio, response_format="verbose_json", timestamp_granularities=["word"])`
- [ ] Бить аудио на чанки ≤25 MB (~50 мин mono 16kHz) — автобатчинг обязателен.

**Режим `deepgram` (`--transcriber deepgram`):**
- [ ] Лучший платный вариант для длинных стримов: $200 кредит разово (~55 стримов по 6ч), потом $0.0043/мин.
- [ ] Файл до 2 GB — **чанкинг не нужен** даже для 6ч стрима.
- [ ] `client = DeepgramClient(api_key=DEEPGRAM_API_KEY)`
- [ ] `client.listen.prerecorded.v("1").transcribe_file(audio, {"model":"nova-3","language":"ru","words":True})`
- [ ] Маппинг response: `result.results.channels[0].alternatives[0].words` → `list[Word]`

**Режим `gladia` (`--transcriber gladia`):**
- [ ] 10 ч/мес бесплатно навсегда. Подходит если 1-2 стрима в месяц.
- [ ] REST API: `POST https://api.gladia.io/v2/pre-recorded` с `word_timestamps: true`
- [ ] Маппинг response → `list[Word]`

**Режим `openai` (`--transcriber openai`):**
- [ ] `gpt-4o-transcribe` с timestamps или `whisper-1`. Платный, нет free tier.

**Общее:**
- [ ] Сохранить JSON-кэш транскрипта (чтобы повторные запуски не транскрибили заново)
- [ ] Тест local: на 30-сек аудио — count слов > 0, у всех слов есть start/end
- [ ] Тест deepgram: mock HTTP — проверить маппинг response → `list[Word]`
- [ ] Добавить `deepgram` как `pip install deepgram-sdk` в зависимости (опционально)

### Этап 4 — Filler filter (`fillers.py`) — **ФАЗА 2**

- [ ] Дефолтный список (русский + английский):
  - RU: `эээ, нуу, ну, эм, эмм, короче, типа, как бы, это самое, ну вот`
  - EN: `uh, um, uhh, umm, like, you know, sort of, kind of, basically`
- [ ] Функция `find_filler_ranges(words: list[Word], filler_words: set[str]) -> list[(start, end)]`
- [ ] Нормализация: lowercase, strip пунктуации
- [ ] **Контекстная фильтрация:** по умолчанию **выключена**. Режем все вхождения филлеров. Причина: проще, false-positives на изолированных «эммм» приемлемы. Включается флагом `--filler-context`.
- [ ] Слить близкие ranges (gap < 50 ms)
- [ ] Тест: на синтетическом транскрипте — проверить, что филлеры найдены, остальные слова сохранены

### Этап 4.5 — Content filter (`content.py`) — **ФАЗА 2**

**Идея:** LLM читает сегменты транскрипта (не слова, а фразы по 10-30 сек), помечает «малозначимые» с обоснованием.

**Что считать малозначимым:**
- Читка донатов/подписок без комментария
- Технические паузы («секунду, щас найду...» → 2-минутная пауза)
- Off-topic чат-взаимодействие без контента
- Повторное изложение только что сказанного

**Что НЕ трогать:**
- Содержательные ответы на вопросы чата
- Демонстрации, технические объяснения
- Эмоциональные реакции (часть характера стрима)

Реализация:
- [ ] Функция `filter_content(segments: list[Segment], provider: str, model: str) -> list[(start, end)]`, где `Segment = {text, start, end}`
- [ ] Сгруппировать words → сегменты по ≤30 сек / ≤200 слов
- [ ] **Промпт LLM** (системный):
  ```
  You analyze transcript segments from a stream recording.
  Mark segments as LOW_VALUE if they contain: filler transitions
  ("one moment", "let me find"), donation/sub readings without
  commentary, exact repetition, off-topic meta-chat.
  Do NOT mark: technical explanations, Q&A, demos, reactions.
  Return JSON: [{"start": float, "end": float, "reason": str,
  "value": "low"|"high"}]
  Keep responses terse. Segment timestamps are absolute (seconds).
  ```
- [ ] Батчинг: посылать по 5-10 сегментов за запрос (экономия токенов)
- [ ] Дефолт LLM: `claude-haiku-4-5-20251001` (дёшево ~$0.001 за час стрима)
- [ ] `--dry-run` выводит список сегментов с reasoning ПЕРЕД cut — критично, иначе потеряем нужный контент
- [ ] Сохранить `content-filter-log.json` в output — аудит что было вырезано
- [ ] Тест: синтетические сегменты (5 low + 5 high) — LLM должен пометить ≥4/5 low корректно

### Этап 5 — Filler+content cut+concat (`concat.py`) — **ФАЗА 2**

- [ ] Функция `cut_and_concat(input: Path, ranges_to_remove: list[(float, float)], output: Path)`
- [ ] Инвертировать «range_to_remove» в «range_to_keep»
- [ ] Использовать `ffmpeg` с filter_complex:
  - Для коротких списков (<50): `select='between(t,start,end)+...'`
  - Для длинных списков: нарезать через `-ss/-to` + concat demuxer (через temp файлы)
- [ ] **Стратегия кодирования:** сначала пытаемся `-c copy` (быстро, lossless). Если cut-точки не I-frame aligned и копирование даёт артефакты/desync — fallback на re-encode: `-c:v libx264 -preset fast -c:a aac`. Предупредить пользователя: re-encode 2-3× медленнее.
- [ ] Тест: на 60-сек видео с 3 указанными ranges — длина выхода = сумма keep-ranges (±200 ms)

### Этап 6 — Subtitles (`subtitles.py`) — **ФАЗА 2**

- [ ] **Решение:** пересчитать timestamps из исходного транскрипта с учётом вырезанных ranges (быстро, не требует повторной транскрипции). Для каждого слова в исходном transcript: если попадает в keep-range → пересчитать `t' = t_orig - sum_of_removed_before(t_orig)`; если в cut-range — выбросить.
- [ ] Альтернатива (медленнее, точнее на edge cases): перетранскрибировать финальный файл. Использовать только если remap даёт ошибки рассинхрона.
- [ ] Сгенерировать `.srt` файл:
  ```
  1
  00:00:00,000 --> 00:00:02,500
  Текст реплики

  2
  ...
  ```
- [ ] Разбить длинные сегменты на куски по ≤7 слов или ≤3 сек
- [ ] Сохранить `final.srt` рядом с `final.mp4`
- [ ] Тест: проверить валидность .srt парсером (`srt` библиотека)

---

## 7. Distribution plan — ✅

- **MVP (v1.0):** GitHub repo + `pip install -e .` локально. ✅ **shipped**
- **Portable:** `run_gui.cmd` ставит Python + ffmpeg в `_portable/`, никаких admin-прав не нужно. ✅
- **Post-v1.0 (если будет интерес):** публикация на PyPI (`pip install stream2video`). ⏸ отложено
- **Post-v1.0 (если нужен portable runtime):** Docker-образ с ffmpeg и предзагруженной whisper моделью. ⏸ отложено

CI не критичен на старте (личный инструмент). GitHub Actions добавлять только когда репо станет публичным.

## 8. Success criteria

### Фаза 1 — ✅ достигнуто
- ✅ `stream2video <URL>` скачивает и выдаёт silence-cut видео без ручного вмешательства.
- ✅ На 1-часовом talking-head стриме: выход < 45 мин (≥25% сжатие) — *нуждается в реальном benchmark на 1-2 стримах для подтверждения*
- ✅ Работает на локальном файле, YouTube URL и Twitch VOD URL.
- ✅ CLI + GUI на одном ядре (cross-platform).

### Фаза 2 (добавится) — ⏸
- ⏸ Сжатие ≥40% на 6ч стриме (filler+silence).
- ⏸ .srt сгенерирован, синхронен (sample 5 точек).
- ⏸ Filler-cut реально режет «эммм/ну/uh» без потерь соседней речи.

## 9. Открытые вопросы

1. **Transcriber по умолчанию** — `local` (faster-whisper/medium, ~1-2× realtime на CPU). Для 6ч стрима = ~3-6ч обработки. Groq free tier (120 мин/день) не покрывает 6ч стримы. Deepgram — лучший API-вариант ($200 кредит).
2. **Word-level subs vs sentence-level** — дефолт: sentence-level (≤7 слов, ≤3 сек).
3. **Burn-in subs** — достаточно отдельного .srt? Если нужны «вжатые» — добавить флаг `--burn-subs` (ffmpeg `subtitles` filter).
4. **Groq file limit 25 MB** — актуально только если Groq используется. При `local`/`deepgram` — не проблема.

## 10. Что дальше / следующая задача

**Phase 1 MVP — shipped (2026-06-02).** Phase 2 (STT) начнётся когда появится реальная потребность.

**Перед началом Phase 2:**
- [ ] Реальный benchmark на 2-3 стримах (1ч + 6ч): замерить wall-time, % сжатия, качество для разных threshold/margin
- [ ] Задокументировать реальный RAM-пик (асимптотики в §4 — теоретические)
- [ ] Решить, нужен ли `batch` метод как default — на практике `segment` ощутимо быстрее

## 11. Альтернативы рассмотрены

- **A: Custom ffmpeg+whisper CLI** — отвергнут: переизобретаем silence-cut, который auto-editor уже делает зрело. *Позже отвергнут и auto-editor в пользу прямого ffmpeg (см. §2).*
- **B: ffmpeg silencedetect + (будущий) whisper wrapper** — ✅ **выбран** для Phase 1.
- **C: video-use agent backend** — отвергнут: платный ElevenLabs, агентный (не CLI), чёрный ящик.

## 12. CEO Review Notes

### 2026-05-25 — initial sign-off
**Mode:** HOLD SCOPE (rigor, no expansions). Scope already decided (Phase 1 MVP silence-cut only, Phase 2 deferred).

**Key decisions approved:**
1. **Implementation approach:** Wrap auto-editor CLI (not custom silence detection). Rationale: battle-tested, ships in 1-2 days vs 2-3 days for custom. Control gained in Phase 2 when whisper (word-level timestamps) becomes available.

2. **Error handling:** Option C — Full robust error strategy. Define 11+ exception types, recovery paths, structured logging. Retry logic for yt-dlp/auto-editor (1x + 5s backoff). Cleanup on error (try/finally). Log to `~/.stream2video/last-run.log`.

3. **Config file support:** YAML/JSON config. Parameter validation ranges in code. CLI flags override config.

4. **Test strategy:** Mix of mocks (unit tests, CI speed) and real integration tests (public 30-sec video). No real downloads in CI.

### 2026-06-02 — Phase 1 ship review

**Decisions reversed / changed:**
1. **auto-editor → ffmpeg silencedetect.** Прямой ffmpeg проще (1 зависимость меньше, проще тестировать, проще читать stderr). Минус: пришлось самим реализовать cut+concat (Phase 1 = `concat.py`). Плюс: контроль над encoder-fallback (auto-editor libx264-fallback менее прозрачен).
2. **CLI-флаги для threshold/min_silence/margin убраны.** Параметры берутся только из YAML-конфига (`--config`). Причина: 3 уровня (default/config/CLI) для 3 параметров — overkill для личного инструмента. Encoder/method остались CLI-флагами, потому что переключаются чаще.
3. **Retry yt-dlp убран.** Fail-fast на сетевых ошибках — проще для пользователя, чем двойной timeout.
4. **Margin range расширен `[-3, 5]`.** План говорил `[0, 5]`, но на практике отрицательный margin (расширяет silence) полезен — режет агрессивнее. Дефолт `-0.5` работает лучше дефолта `0` на тестовых стримах.
5. **Default method = `batch`** (НЕ `segment` как в плане). Frame-exact важнее для чистоты реза. Сегмент-метод — escape hatch, когда `batch` слишком долог.

**No expansions proposed.** Phase 1 stays focused on silence-cut. Phase 2 (STT) начнётся отдельно.

**Known gaps при ship:**
- Реальные ffmpeg-вызовы (`cut_and_concat` интеграция) не покрыты тестами — мокать subprocess.Popen overengineering для текущего scope
- GUI без автоматизированных тестов (нет pytest-qt)
- Нет CI (GitHub Actions)

**Scope impact:**
- Actual: +1 day (auto-editor → ffmpeg refactor)
- Net: -1 day (removed retry, removed CLI-флаги для params, упростил presets)
- Total Phase 1: ~4 дня работы.
