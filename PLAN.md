# stream2video — план пайплайна

**Цель:** автоматически сжимать видеозаписи стримов (Twitch/YouTube), оставляя только моменты с речью, и генерировать к ним субтитры. Личный CLI-инструмент.

**Статус:** DRAFT
**Дата:** 2026-05-25
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
- ✅ **Стек Фазы 1:** `yt-dlp` + `auto-editor` + Python CLI (`typer`). STT/LLM зависимости — Фаза 2.
- ✅ **GPU нет** — CPU-only. В Фазе 1 GPU не нужен вообще.
- ⚠️ **Live vs VOD:** только VOD (готовая запись). Live capture вне scope.

## 3. Архитектура

### Фаза 1 — MVP (silence-cut)

```
URL (twitch/youtube)
       │
       ▼
┌─────────────┐
│   yt-dlp    │  → input.mp4
└─────────────┘
       │
       ▼
┌─────────────┐
│ auto-editor │  → final.mp4   (silence-cut, без STT)
└─────────────┘
```

**Поток:** download → silence-cut → готово.

### Фаза 2 — STT-расширения (позже)

```
final.mp4 (из Фазы 1)
       │
       ▼
┌─────────────────┐
│ faster-whisper  │  → words.json (word-level timestamps)
│ / Deepgram API  │
└─────────────────┘
       │
       ├──────────────────────────┐
       ▼                          ▼
┌─────────────┐         ┌──────────────────┐
│filler-filter│         │ content-filter   │
│(regex/list) │         │ (Claude/OpenAI)  │
└─────────────┘         └──────────────────┘
       │                          │
       └────────────┬─────────────┘
                    ▼
          ┌──────────────────┐
          │ ffmpeg cut+concat│  → final-v2.mp4
          └──────────────────┘
                    │
                    ▼
          ┌──────────────────┐
          │  timestamp remap │  → final-v2.srt
          └──────────────────┘
```

## 4. Стек / зависимости

### Фаза 1 (MVP)

| Компонент | Назначение | Установка |
|---|---|---|
| Python 3.11+ | runtime | system / `pyenv` |
| `yt-dlp` | загрузка Twitch/YouTube VOD | `pip install yt-dlp` |
| `ffmpeg` | системный пакет | `apt install ffmpeg` |
| `auto-editor` | silence detection + cut | `pip install auto-editor` |
| `typer` | CLI | `pip install typer` |
| `pyyaml` | config file parsing | `pip install pyyaml` |
| `rich` | progress bars + logging | `pip install rich` |

### Системные требования Phase 1

- **RAM:** Фаза 1 loads entire video in auto-editor subprocess. Expect:
  - 1h video (720p, 1.5 GB) → ~3 GB peak RAM
  - 6h video (720p, 9 GB) → ~10 GB peak RAM
  - **Recommendation:** Ensure ≥12 GB RAM available for 6h streams. Larger files will OOM without chunking (Phase 2 feature).
- **Disk:** ~2× size of original video (input + temp files). Cleanup on success/error.
- **CPU:** auto-editor is CPU-bound. 6h video processes ~3-6h realtime on modern CPU.

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

### Фаза 1 (MVP)

```bash
stream2video URL [options]

# Примеры:
stream2video https://twitch.tv/videos/12345
stream2video https://youtube.com/watch?v=abc --threshold -30 --min-silence 0.5
stream2video ./local.mp4 --output ./out/
stream2video https://youtube.com/watch?v=abc --preset aggressive

# Флаги:
  --threshold DBFS        порог тишины в dB (range: -60...-5, default: -30)
  --min-silence FLOAT     минимальная длина паузы (range: 0.1...60, default: 1.0 sec)
  --margin FLOAT          padding до/после речи (range: 0...5, default: 0.2 sec)
  --output DIR            выходная директория (default: ./output/)
  --preset PRESET         готовые профили: gentle|balanced|aggressive (overrides threshold)
  --config FILE           загрузить параметры из YAML/JSON
  --keep-intermediate     не удалять промежуточные файлы (debug режим)
  --dry-run               показать что будет сделано без обработки
```

**Параметр validation:**
- threshold: [-60, -5] dB (вне этого диапазона → exit 1)
- min-silence: [0.1, 60] сек
- margin: [0, 5] сек

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

## 5a. Error Handling Strategy (Phase 1)

Все ошибки должны быть именованы, залогированы + предоставить пользователю actionable message. Структурированное логирование через `rich.logging`.

| Метод | Может пойти не так | Exception class | Обработка | Пользователь видит |
|---|---|---|---|---|
| `cli.download()` | Неверный URL | URLValidationError | reject at parse | "Invalid URL: must be http(s)" |
| | URL timeout | yt-dlpTimeoutError | retry 1× + backoff 5s | "Retrying... (attempt 2/2)" |
| | Video не найдена | VideoNotAvailableError | exit 1 | "Video not found / geo-blocked / private" |
| | Disk full | DiskFullError | exit 1 | "Disk full: need XGB, have YGB" |
| | No write perm | PermissionError | exit 1 | "Cannot write to OUTPUT_DIR. Check permissions." |
| `cli.silence_cut()` | auto-editor not in PATH | AutoEditorNotFoundError | exit 1 + hint | "auto-editor not found. Install: pip install auto-editor" |
| | auto-editor crash | AutoEditorCrashError | capture stderr, retry 1× | "auto-editor failed (retry 1/1): [stderr excerpt]" |
| | Video без аудио | NoAudioTrackError | exit 1 | "Video has no audio track" |
| | Output <1 sec | TinyOutputError | exit 1 | "Output video <1 sec — video is all silence?" |
| | Corrupt video frame | CorruptFrameError | exit 1 + stderr log | "Corruption detected. Try different threshold." |
| All paths | Orphaned temp files | (all) | cleanup in finally block | (none — silent cleanup) |

**Retry strategy:**
- yt-dlp timeout: retry 1× with 5s backoff (total 2 attempts)
- auto-editor crash: retry 1× (total 2 attempts)
- Other errors: fail fast, no retry

**Logging:**
- All exceptions logged with full context (args, user, timestamp)
- Stderr from subprocesses saved to `~/.stream2video/last-run.log`
- User sees summary on stderr; detailed log path shown on error

---

## 6. Чек-листы по этапам

### Этап 0 — Подготовка окружения

- [ ] Установить Python 3.11+ и проверить `python --version`
- [ ] Установить `ffmpeg` (`ffmpeg -version`)
- [ ] Создать venv: `python -m venv .venv && source .venv/bin/activate`
- [ ] Создать `pyproject.toml` или `requirements.txt`
- [ ] Установить: `pip install yt-dlp auto-editor typer`
- [ ] Создать структуру проекта (Фаза 1):
  ```
  /opt/stream2video/
  ├── pyproject.toml
  ├── README.md
  ├── stream2video/
  │   ├── __init__.py
  │   ├── cli.py          # точка входа (typer)
  │   ├── download.py     # yt-dlp обёртка
  │   └── silence.py      # auto-editor wrapper
  └── tests/
  ```
  *(Фаза 2 добавит: `transcribe.py`, `fillers.py`, `content.py`, `concat.py`, `subtitles.py`)*

### Этап 1 — Загрузка видео (`download.py`)

- [ ] Функция `download(url: str, out_dir: Path) -> Path`
- [ ] Через `yt_dlp.YoutubeDL` с параметрами:
  - `format='best[ext=mp4]/best'`
  - `outtmpl='{out_dir}/%(id)s.%(ext)s'`
  - `quiet=True`, `no_warnings=True`
- [ ] Поддержка локальных файлов (если input — путь, не URL — пропустить загрузку)
- [ ] Обработка ошибок: 404, geo-block, private video → понятный exit code
- [ ] Тест: скачать короткий публичный YouTube clip (≤30 сек)
- [ ] Тест: скачать Twitch VOD (короткий)

### Этап 2 — Silence cut через auto-editor (`silence.py`)

- [ ] Функция `silence_cut(input: Path, threshold: float, min_silence: float, margin: float) -> Path`
- [ ] **Решение:** вызывать `auto-editor` как subprocess (не Python API). Причина: проще ловить stderr, не зависим от внутренних изменений auto-editor.
  ```
  auto-editor input.mp4 \
    --edit "audio:threshold={threshold}dB" \
    --margin {margin}sec \
    --frame-rate 30 \
    --output silence-cut.mp4
  ```
- [ ] Захватить stderr/stdout, парсить процент сжатия
- [ ] **Error handling:** try/finally block for cleanup of temp files on exception. Specific exceptions per Section 5a (Error Handling Strategy).
- [ ] Тест: на 1-мин видео с искусственной паузой 10 сек — проверить, что выход короче
- [ ] Тест: empty video (no audio) → reject with clear error
- [ ] Тест: corrupt frame → auto-editor error → captured + logged

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

### Этап 7 — CLI и связка (`cli.py`) — Фаза 1

- [ ] `typer.Typer()` app с command `process`
- [ ] **Parameter validation:** threshold [-60...-5], min-silence [0.1...60], margin [0...5]. Reject with clear errors if out of range.
- [ ] **Config file support:** load from `~/.config/stream2video/config.yaml` or `./stream2video.yaml` (if exists). CLI flags override config.
  ```python
  # Load config.yaml, validate schema, merge with CLI args
  config = load_config()
  params = merge(config, cli_args)
  ```
- [ ] **Presets:** `--preset aggressive|balanced|gentle` (predefined threshold/min-silence combinations)
- [ ] Флаги: `--threshold`, `--min-silence`, `--margin`, `--output`, `--preset`, `--config`, `--keep-intermediate`, `--dry-run`
- [ ] Pipeline orchestration: validate → download → silence-cut → cleanup & output
- [ ] Progress bars (`rich.progress`) для долгих шагов (download %, auto-editor progress)
- [ ] Structured logging (`rich.logging`) to stderr + file (`~/.stream2video/last-run.log`)
- [ ] `--dry-run`: показать plan без выполнения
- [ ] Error messages: actionable, include log path, retry info
- [ ] Точка входа в `pyproject.toml`: `[project.scripts] stream2video = "stream2video.cli:app"`
- [ ] `pip install -e .` для локальной разработки
- [ ] Тест end-to-end: `stream2video <URL> --output ./test-out/`

### Этап 8 — Тесты и edge cases — Фаза 1

- [ ] Test infrastructure:
  - [ ] Fixtures: 10-sec test video with known structure (silence + speech + silence)
  - [ ] Mocks: yt-dlp and auto-editor subprocess calls for unit tests (don't hit real APIs)
  - [ ] Real integration tests: use pre-downloaded ~30s public video (YouTube, Vimeo) stored locally
- [ ] Unit-тесты (with mocks):
  - [ ] `download.py`: valid URL parse, invalid URL reject, mock yt-dlp success/failure paths
  - [ ] `silence.py`: mock auto-editor subprocess, verify args passed correctly, stderr capture
  - [ ] `cli.py`: parameter validation (threshold range, etc), config file loading, preset application
- [ ] Integration tests (with real files, no mocks):
  - [ ] Download public 30-sec video + run silence-cut (end-to-end)
  - [ ] Verify output exists, is playable, is shorter than input
- [ ] Edge cases (all with mocks for speed):
  - [ ] Видео без речи вообще (сплошная тишина) → auto-editor returns empty or pass-through → expect error "all silence"
  - [ ] Видео без пауз (выход = вход) → 0% compression → accept (warn user)
  - [ ] Длинное видео (3+ ч) → auto-editor doesn't OOM/hang in test (use mock)
  - [ ] Локальный файл вместо URL
  - [ ] Twitch VOD недоступен (mock 404) → expect clear error
  - [ ] Corrupt video frame → auto-editor subprocess fails → expect error with stderr
  - [ ] Parameter out-of-range (threshold=0, min-silence=-5) → CLI rejects before subprocess
  - [ ] Config file missing / malformed YAML → graceful fallback to defaults or error
- [ ] CI: GitHub Actions with `pytest` (mocked, fast ~30s)
  - [ ] No real video downloads or processing in CI
  - [ ] Just verify CLI parsing, error paths, mocks work

### Этап 9 — Документация и релиз

- [ ] `README.md`: install, usage, examples, troubleshooting
- [ ] `--help` чистый и понятный
- [ ] Дефолтные параметры подобраны на 3-4 реальных стримах
- [ ] Запуск на 1-часовом стриме — измерить wall-time и сжатие
- [ ] (опц.) Опубликовать на PyPI: `python -m build && twine upload dist/*`
- [ ] (опц.) Docker-образ с ffmpeg и моделью предзагруженной

---

## 7. Distribution plan

- **MVP (v1.0):** GitHub repo + `pip install -e .` локально. На этом останавливаемся.
- **Post-v1.0 (только если будет интерес):** публикация на PyPI (`pip install stream2video`).
- **Post-v1.0 (если нужен portable runtime):** Docker-образ с ffmpeg и предзагруженной whisper моделью.

CI не критичен на старте (личный инструмент). GitHub Actions добавлять только когда репо станет публичным.

## 8. Success criteria

### Фаза 1
- ✅ `stream2video <URL>` скачивает и выдаёт silence-cut видео без ручного вмешательства.
- ✅ На 1-часовом talking-head стриме: выход < 45 мин (≥25% сжатие).
- ✅ Работает на локальном файле и YouTube URL и Twitch VOD URL.

### Фаза 2 (добавится)
- ✅ Сжатие ≥40% на 6ч стриме (filler+silence).
- ✅ .srt сгенерирован, синхронен (sample 5 точек).
- ✅ Filler-cut реально режет «эммм/ну/uh» без потерь соседней речи.

## 9. Открытые вопросы

1. **Transcriber по умолчанию** — `local` (faster-whisper/medium, ~1-2× realtime на CPU). Для 6ч стрима = ~3-6ч обработки. Groq free tier (120 мин/день) не покрывает 6ч стримы. Deepgram — лучший API-вариант ($200 кредит).
2. **Word-level subs vs sentence-level** — дефолт: sentence-level (≤7 слов, ≤3 сек).
3. **Burn-in subs** — достаточно отдельного .srt? Если нужны «вжатые» — добавить флаг `--burn-subs` (ffmpeg `subtitles` filter).
4. **Groq file limit 25 MB** — актуально только если Groq используется. При `local`/`deepgram` — не проблема.

## 10. Что дальше / следующая задача

**Первый конкретный шаг (Фаза 1):** Этап 0 + Этап 1 — поднять окружение, скачать короткий публичный YouTube clip через `yt-dlp` Python API. Это даёт фундамент.

**После Фазы 1 MVP:** решить начинать ли Фазу 2 (STT) и какой transcriber выбрать (local/deepgram/gladia).

## 11. Альтернативы рассмотрены

- **A: Custom ffmpeg+whisper CLI** — отвергнут: переизобретаем silence-cut, который auto-editor уже делает зрело.
- **B: auto-editor + whisper wrapper** — выбран.
- **C: video-use agent backend** — отвергнут: платный ElevenLabs, агентный (не CLI), чёрный ящик.

## 12. CEO Review Notes (2026-05-25)

**Mode:** HOLD SCOPE (rigor, no expansions). Scope already decided (Phase 1 MVP silence-cut only, Phase 2 deferred).

**Key decisions approved:**
1. **Implementation approach:** Wrap auto-editor CLI (not custom silence detection). Rationale: battle-tested, ships in 1-2 days vs 2-3 days for custom. Control gained in Phase 2 when whisper (word-level timestamps) becomes available.

2. **Error handling:** Option C — Full robust error strategy. Define 11+ exception types, recovery paths, structured logging. Retry logic for yt-dlp/auto-editor (1x + 5s backoff). Cleanup on error (try/finally). Log to `~/.stream2video/last-run.log`.

3. **Config file support:** YAML/JSON config with presets ("aggressive", "gentle", "balanced"). Parameter validation ranges in code. CLI flags override config. Saves user typing, improves UX.

4. **Memory constraints:** Documented that Phase 1 expects <4GB RAM for 1h video, ~10GB for 6h. If exceeded, will OOM (Phase 2 adds chunking). User warned in README.

5. **Test strategy:** Mix of mocks (unit tests, CI speed) and real integration tests (public 30-sec video). No real downloads in CI. Edge cases covered (corrupt, no audio, all silence, etc).

**Scope impact:**
- Effort: +2 days for error handling, +1 day for config file = +3 days total vs minimal MVP.
- Rationale: Tech debt avoidance. Better to ship robust than iterate on flakiness.
- Tradeoff: slightly longer Phase 1, but Phase 2 builds on stable foundation.

**No expansions proposed.** Phase 1 stays focused on silence-cut. Next expansions possible in Phase 2 (STT, filler-cut, content-filter).

---

## 13. Замечания об ответах на вопросы

- Ты сразу обозначил конкретные кандидаты (3 репо) — это сильнее, чем «найди что-то». Из них только `video-use` попадал в задачу, и ты получил бы это сразу при чтении README; быстрее всего пробежаться по READMEs кандидатов, прежде чем выбирать.
- Ты согласился добавить filler-cut поверх silence-cut. Это переход от «cut когда тихо» к «cut когда нет смысла» — большая разница в сложности, но и в результате. Filler-cut без word-level транскрипта не сделать, поэтому whisper — обязательная зависимость.
- Выбор CLI (а не watch-dir) сужает scope правильно. Watch-dir можно дописать поверх CLI за час, когда захочется.
