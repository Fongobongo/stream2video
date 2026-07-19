# stream2video — подробный план развития

> Рабочий roadmap, объединяющий исходный `PLAN.md`, аудит текущего кода и полный список предложенных функций.
>
> Порядок обязателен: сначала корректность и безопасность медиаядра, затем интерактивное редактирование, STT и дополнительные продукты.

## Принципы

- Не добавлять новые анализаторы поверх pipeline, который теряет кадры.
- `Source` — режим FPS по умолчанию; конвертация FPS только по явному выбору.
- Любое автоматическое удаление контента должно иметь dry-run и ручной override.
- CPU/RAM/VRAM должны иметь бюджет и оставлять системе резерв.
- Каждый этап заканчивается автоматическими acceptance tests.
- Source не удаляется, пока итоговый файл не прошёл quality gate.

---

# Milestone 0 — baseline и воспроизводимость

## 0.1. Окружение

- [ ] Выбрать Python policy: 3.13-only или `>=3.11`.
- [ ] Синхронизировать `.python-version`, `requires-python`, Ruff, mypy и CI.
- [ ] Выполнить `uv sync --all-extras --dev`.
- [ ] Зафиксировать версии Python, FFmpeg, FFprobe и yt-dlp.
- [ ] Запустить `ruff check .`.
- [ ] Запустить `ruff format --check .`.
- [ ] Запустить `mypy stream2video`.
- [ ] Запустить `pytest -v`.
- [ ] Сохранить baseline failures до изменения production-кода.

## 0.2. Media fixtures

- [ ] Генератор CFR fixtures: 24/25/29.97/30/50/59.94/60 FPS.
- [ ] VFR fixture.
- [ ] Mono, stereo и 5.1 audio fixtures.
- [ ] Fixture без audio stream.
- [ ] Fixture с несколькими audio tracks.
- [ ] Silence в начале, середине и до EOF.
- [ ] Non-zero и broken timestamps.
- [ ] Reproduction: 6 секунд, 30 FPS, keep `0–2` и `3–5`.
- [ ] FFprobe helper: duration, frame count, FPS, streams, bitrate, A/V drift.

## 0.3. Definition of Done

- [ ] Потеря кадров воспроизводится failing regression test.
- [ ] CI сохраняет FFprobe diagnostics и output при падении.
- [ ] Baseline report добавлен в репозиторий.

---

# Milestone 1 — корректность video/audio pipeline

## 1.1. Segment path

- [ ] Удалить двойной input/output seek.
- [ ] Оставить coarse input `-ss` только для ускорения.
- [ ] Точную границу делать через `trim=start=:duration=`.
- [ ] Для audio использовать `atrim=start=:duration=`.
- [ ] Использовать `setpts=PTS-STARTPTS`.
- [ ] Использовать `asetpts=PTS-STARTPTS`.
- [ ] Удалить `setpts=N/FRAME_RATE/TB`.
- [ ] Проверить диапазоны у `t=0`, между keyframes и в конце.
- [ ] Проверить 1, 2, 10 и 100 keep ranges.

## 1.2. Batch path

- [ ] Пересобрать select/aselect graph без принудительного 25 FPS.
- [ ] Удалить лишний `concat=n=1`, если он не нужен.
- [ ] Зафиксировать политику включения последнего кадра range.
- [ ] Поддержать CFR без drop/dup.
- [ ] Поддержать VFR с сохранением PTS.
- [ ] Сравнивать segment и batch на одинаковых ranges.

## 1.3. Output FPS

- [ ] Добавить `output_fps_mode = source | cfr`.
- [ ] Добавить `output_fps`, nullable string/number.
- [ ] GUI: `Source (recommended)`, `30`, `60`, `Custom…`.
- [ ] Advanced presets: 23.976, 24, 25, 29.97, 30, 50, 59.94, 60.
- [ ] Custom принимает integer, decimal и rational (`60000/1001`).
- [ ] Валидация custom FPS: 1–240, без NaN/inf/negative.
- [ ] В режиме Source сохранять timestamps, а не фиксировать average FPS.
- [ ] Для CFR conversion использовать отдельный `fps=<target>` filter.
- [ ] Предупреждать: 30→60 создаёт дубликаты, 60→30 удаляет кадры.
- [ ] Не использовать `-r 60` как исправление потери кадров.
- [ ] Показывать отдельно output FPS и encoding throughput.

## 1.4. Audio timeline

- [ ] Удалить накопление `_AUDIO_PAD` по 100 мс на сегмент.
- [ ] Не делать финальную audio duration равной `dur + pad`.
- [ ] Проверить AAC priming и gapless metadata.
- [ ] Проверить щелчки, пропуски и вставленную тишину.
- [ ] Ограничить A/V drift одним media frame.
- [ ] Не маскировать крупный drift через `aresample=async=1`.

## 1.5. Stream mapping

- [ ] Явно выбирать video stream.
- [ ] Добавить выбор audio track.
- [ ] Определить поведение без audio stream.
- [ ] Определить subtitle/data stream policy.
- [ ] Добавить тест нескольких audio tracks.

## 1.6. Quality gate

- [ ] Output существует и открывается FFprobe.
- [ ] Все ожидаемые streams присутствуют.
- [ ] Фактическая duration совпадает с суммой keep ranges.
- [ ] A/V drift в допуске.
- [ ] FPS не изменился без запроса.
- [ ] Frame count правдоподобен.
- [ ] Файл ненулевой и содержит moov atom.
- [ ] Source не удаляется при провале gate.
- [ ] Diagnostic JSON сохраняется рядом с failed output.

**Готово, когда:** reproduction даёт около 4 секунд и 120 кадров, segment/batch проходят CFR/VFR/A-V matrix.

---

# Milestone 2 — качество и безопасное кодирование

## 2.1. Audio quality

- [ ] `audio_quality`: low=128k, medium=192k, high=256k, custom.
- [ ] CLI `--audio-quality` и `--audio-bitrate`.
- [ ] GUI combobox.
- [ ] `audio_sample_rate=source|44100|48000`.
- [ ] `audio_channels=source|mono|stereo`.
- [ ] Опциональный Opus output.
- [ ] Не понижать source bitrate скрытно.
- [ ] Проверять bitrate/sample rate/channels через FFprobe.

## 2.2. Safe libx264

- [ ] Добавить `x264_preset`.
- [ ] Добавить `x264_threads`.
- [ ] Ставить output `-threads N` после `-c:v libx264`.
- [ ] Generated-command test для позиции `-threads`.
- [ ] Low CPU: `veryfast/ultrafast`, 1–4 threads.
- [ ] Balanced: `fast`, ограниченные threads.
- [ ] Quality: `medium`, предупреждение о нагрузке.
- [ ] Не использовать `slow` по умолчанию.
- [ ] Показывать current/peak CPU.
- [ ] Реально smoke-test libx264.

## 2.3. Encoder fallback

- [ ] `software_fallback=ask|disabled|enabled`.
- [ ] GUI default: ask или disabled.
- [ ] Показывать причину fallback.
- [ ] Не fallback после cancel, OOM или memory stop.
- [ ] Показывать фактический encoder.
- [ ] Очищать только несовместимые partial artifacts.

## 2.4. Hardware profiles

- [ ] AMF profiles: speed/balanced/quality.
- [ ] NVENC profiles и документированный rate control.
- [ ] MediaFoundation profile.
- [ ] Добавить QSV и VideoToolbox как optional.
- [ ] Capability detection.
- [ ] Короткий benchmark profile.
- [ ] Сравнить качество, speed, RAM и VRAM.

---

# Milestone 3 — CPU/RAM/VRAM budget

## 3.1. Настройки

- [ ] `cpu_limit_percent`.
- [ ] `memory_limit_mb=auto|N`.
- [ ] `memory_reserve_mb`.
- [ ] `max_parallel_processes`.
- [ ] Soft `gpu_memory_limit_mb`, где возможно.
- [ ] Presets: Low CPU, Low memory, Balanced, Maximum performance.

## 3.2. Auto budget

- [ ] Определять logical CPU count.
- [ ] Определять available RAM.
- [ ] Оставлять ОС 2–4 GB.
- [ ] Выделять pipeline не более 50–65% доступной RAM.
- [ ] Рассчитывать рекомендуемые encoder threads.
- [ ] Не запускать второй тяжёлый FFmpeg без бюджета.
- [ ] Не обещать точный CPU% только через threads.

## 3.3. Runtime watchdog

- [ ] Мониторить RSS Python и FFmpeg.
- [ ] Мониторить available RAM и swap/pagefile pressure.
- [ ] Мониторить VRAM, если backend доступен.
- [ ] Soft threshold 80%: warning, запрет новых задач, снижение concurrency.
- [ ] Hard threshold 95% или нарушение OS reserve: остановка задачи.
- [ ] Не использовать pause как освобождение памяти.
- [ ] Сохранять только валидные resume artifacts.
- [ ] Показывать CPU/RAM/VRAM и peak values в GUI/log.

## 3.4. OS limits

- [ ] Windows Job Object memory limit.
- [ ] Windows Job Object CPU rate control.
- [ ] Linux cgroup `MemoryHigh/MemoryMax`.
- [ ] Linux cgroup `CPUQuota`.
- [ ] Portable Linux fallback через `RLIMIT_AS` с понятной ошибкой.
- [ ] Не считать process priority строгим лимитом CPU/RAM.

## 3.5. Low-memory pipeline

- [ ] Один encode/decode FFmpeg по умолчанию.
- [ ] Не запускать full waveform decode параллельно с encode.
- [ ] Динамический `_BATCH_CHUNK_SIZE`.
- [ ] При pressure предлагать `batch → segment`.
- [ ] Хранить промежуточные данные на диске.
- [ ] Потоковый waveform downsample.
- [ ] Low-memory x264 profile с ограниченными threads/lookahead/refs.

**Готово, когда:** стресс-тесты 4/8/16/32 GB сохраняют резерв ОС и отзывчивость GUI.

---

# Milestone 4 — download, процессы и resume

## 4.1. yt-dlp progress

- [ ] Исправить template на `%(progress.*)s`.
- [ ] `total_bytes` с fallback `total_bytes_estimate`.
- [ ] Исправить `best` на `bestvideo+bestaudio/best`.
- [ ] Не нарушать resolution cap fallback-форматом.
- [ ] Fake HTTP server integration test.

## 4.2. Connection UX

- [ ] Состояния Resolving/Connecting/Waiting/Downloading/Merging/Retrying.
- [ ] Timestamp последнего progress event.
- [ ] Warning после 20–30 секунд без данных.
- [ ] `connection_timeout`.
- [ ] `no_progress_timeout`.
- [ ] `download_timeout`.
- [ ] Управляемые retries и fragment retries.
- [ ] Показывать retry count и безопасное описание ошибки.
- [ ] Проверять свободный диск до загрузки.

## 4.3. Process supervisor

- [ ] Единый runner для FFmpeg/yt-dlp/STT.
- [ ] Process ownership по task ID.
- [ ] Cross-platform process groups.
- [ ] Cancellation, total timeout и no-progress timeout.
- [ ] Отдельный watchdog, не зависящий от `readline()`.
- [ ] Удалить глобальный single-slot `_active_proc`.
- [ ] Preview и pipeline не должны отменять процессы друг друга.

## 4.4. Resume integrity

- [ ] Manifest для segment/batch working dir.
- [ ] Source identity: path, size, mtime_ns, optional quick hash.
- [ ] Hash keep ranges.
- [ ] Method, encoder, FPS, video/audio quality, pipeline version.
- [ ] FFprobe validation каждого resumed chunk.
- [ ] Atomic temp→final rename.
- [ ] Очистка working dir при manifest mismatch.
- [ ] Silence resume подключить к CLI.
- [ ] Тест смены source/encoder/quality после crash.

---

# Milestone 5 — производительность

## 5.1. Batch windowing

- [ ] Для chunk вычислять min start/max end.
- [ ] Coarse `-ss` перед input.
- [ ] Ограничить decode window.
- [ ] Пересчитать filter timestamps относительно window start.
- [ ] Добавить keyframe safety margin.
- [ ] Сравнить correctness до/после.

## 5.2. GPU decode

- [ ] Capability detection NVDEC/D3D11VA/QSV/VideoToolbox.
- [ ] Optional `hardware_decode`.
- [ ] Проверить совместимость decode→filter→encode.
- [ ] CPU fallback при unsupported filter path.
- [ ] Benchmark speed/RAM/VRAM.

## 5.3. Управляемая параллельность

- [ ] Segment worker pool за feature flag.
- [ ] Concurrency учитывает CPU/RAM/VRAM budget.
- [ ] CPU default: 1 encoder.
- [ ] GPU default: 1; 2–3 только после benchmark.
- [ ] Cancel всех workers конкретной job.
- [ ] Resume manifest совместим с parallel execution.

## 5.4. Hybrid lossless cut

- [ ] Исследовать stream copy для GOP-aligned внутренних областей.
- [ ] Re-encode только пограничных GOP.
- [ ] Проверять codec parameter compatibility перед concat.
- [ ] Полный re-encode fallback.
- [ ] Не обещать frame-exact `-c copy` для произвольных точек.

---

# Milestone 6 — dry-run и интерактивный редактор

## 6.1. Настоящий dry-run

- [ ] Подключить `detect_silence_stream` к waveform popup.
- [ ] Не требовать предварительного pipeline/cache.
- [ ] Preview modes: 60s sample, selected window, full scan.
- [ ] Показывать cut/keep ranges, число склеек, ожидаемую duration и % удаления.
- [ ] Пересчитывать overlay при изменении threshold/min silence/margin.
- [ ] Debounce и cancel старого preview.
- [ ] Не писать final cache без явного решения.

## 6.2. Прослушивание

- [ ] Клик по timeline воспроизводит 5 секунд вокруг точки.
- [ ] Preview перехода между двумя keep ranges.
- [ ] До/после preset A/B.
- [ ] Отдельный volume control.
- [ ] Stop playback при закрытии popup.

## 6.3. Manual ranges

- [ ] Клик по cut range → keep override.
- [ ] Клик по keep range → manual cut.
- [ ] Drag handles границ.
- [ ] Маркеры Keep/Cut/Chapter/Highlight.
- [ ] Undo/redo.
- [ ] Manual keep имеет приоритет над automation.
- [ ] Сохранять overrides в project file.

## 6.4. Presets

- [ ] Вернуть YAML presets.
- [ ] Gentle/Default/Aggressive/Speech only/Low CPU/Low memory/Custom.
- [ ] GUI preset selector.
- [ ] CLI `--preset`.
- [ ] CLI overrides `--threshold`, `--min-silence`, `--margin`.
- [ ] Приоритет: CLI > project > preset > config > defaults.
- [ ] Import/export user presets.

## 6.5. Project format

- [ ] `.s2v-project.json` schema.
- [ ] Source identity и relocatable path.
- [ ] Settings, detected ranges, manual overrides.
- [ ] Transcript, chapters, output profiles.
- [ ] Run history и manifests.
- [ ] Autosave и crash recovery.
- [ ] Schema versioning и migration.

## 6.6. Timeline export

- [ ] JSON/CSV ranges.
- [ ] EDL.
- [ ] FCPXML.
- [ ] DaVinci/Premiere-compatible XML.
- [ ] LosslessCut project.
- [ ] Shotcut/Kdenlive formats.
- [ ] FFmetadata chapters.

---

# Milestone 7 — архитектура и тесты GUI

## 7.1. Разделение GUI

- [ ] `gui/main_window.py`.
- [ ] `gui/pipeline_controller.py` без Tk dependency.
- [ ] `gui/waveform_editor.py`.
- [ ] `gui/project_panel.py`.
- [ ] `gui/queue_panel.py`.
- [ ] `gui/settings.py`.
- [ ] `gui/progress_model.py`.
- [ ] Immutable pipeline config dataclass.
- [ ] Все widget values читать в main thread.
- [ ] Worker общается через events/queue.

## 7.2. Общие модели

- [ ] `Range` с action/source/confidence/reason.
- [ ] `Project`.
- [ ] `Job`.
- [ ] `ResourceBudget`.
- [ ] `ProgressEvent`.
- [ ] `RunManifest`.

## 7.3. Тестирование

- [ ] Pipeline controller tests без Tk.
- [ ] GUI state transition tests.
- [ ] Preview+pipeline concurrency test.
- [ ] Close/cancel на каждом этапе.
- [ ] Download fake-server tests.
- [ ] Crash/resume tests.
- [ ] Memory stress tests.
- [ ] Windows smoke tests.
- [ ] Benchmark 1h/6h real VOD.

---
# Milestone 8 — STT и субтитры

## 8.1. Transcript model

- [ ] Создать `transcribe.py`.
- [ ] Модель `Word(text,start,end,confidence,speaker=None)`.
- [ ] Модель `Transcript(words,language,backend,model,source_id)`.
- [ ] Использовать существующий WAV 16 kHz mono.
- [ ] Cache key: source/model/language/backend/options.
- [ ] Atomic transcript cache.
- [ ] Resume chunked transcription.

## 8.2. Local faster-whisper

- [ ] Optional dependency group `stt-local`.
- [ ] Models tiny/base/small/medium/large-v3.
- [ ] Device auto/cpu/cuda.
- [ ] Compute type auto/int8/float16.
- [ ] Ограничить CPU threads и RAM/VRAM.
- [ ] Word timestamps.
- [ ] Auto language detection.
- [ ] RU/EN/mixed speech tests.
- [ ] Progress по audio duration.

## 8.3. Cloud transcribers

- [ ] Общий `TranscriberBackend` interface.
- [ ] Deepgram backend первым.
- [ ] Gladia backend.
- [ ] Groq backend с chunking ≤ лимита.
- [ ] OpenAI backend.
- [ ] Retry/backoff и resume chunks.
- [ ] Не логировать API keys.
- [ ] Cost estimate до отправки.
- [ ] Явное подтверждение cloud upload.

## 8.4. Subtitles

- [ ] Сначала SRT.
- [ ] Затем WebVTT.
- [ ] Затем ASS styles.
- [ ] Remap timestamps после всех cut ranges.
- [ ] Удалять слова внутри cut ranges.
- [ ] Разбивать по паузам, пунктуации, длине и duration.
- [ ] Ограничение длины строки.
- [ ] Проверить sync в начале/середине/конце.
- [ ] `--burn-subs`.
- [ ] Bilingual/translated subtitles как optional.
- [ ] Transcript TXT/Markdown export.

## 8.5. Privacy/cost

- [ ] Modes: Local only / Cloud STT / Cloud STT+LLM.
- [ ] Показывать размер upload, цену и ожидаемое время.
- [ ] Удалять временные cloud artifacts.
- [ ] Audit: какие данные отправлены и куда.
- [ ] Не отправлять длинный VOD без подтверждения стоимости.

**Готово, когда:** local STT создаёт cache и синхронный SRT, cloud backend подключается через общий интерфейс.

---

# Milestone 9 — умная нарезка речи

## 9.1. Единый range engine

- [ ] `Range(start,end,action,source,confidence,reason)`.
- [ ] Sources: silence, VAD, filler, LLM, manual, chat.
- [ ] Actions: cut, keep, chapter, highlight.
- [ ] Merge overlapping ranges.
- [ ] Настраиваемый padding по source.
- [ ] Manual keep выше automated cut.
- [ ] Conflict resolution и provenance.
- [ ] Возможность выключить detector без повторной STT.

## 9.2. Filler detector

- [ ] Создать `fillers.py`.
- [ ] RU/EN default lists.
- [ ] Multi-word fillers.
- [ ] Нормализация регистра/пунктуации.
- [ ] Context mode.
- [ ] Confidence threshold.
- [ ] Padding вокруг соседней речи.
- [ ] Merge близких filler ranges.
- [ ] Preview и manual approve/reject.
- [ ] Не вырезать содержательные «ну»/`like` без context policy.
- [ ] Отчёт найденных/удалённых fillers.

## 9.3. VAD / Smart Silence

- [ ] `detector=silence|vad|hybrid`.
- [ ] Silero VAD backend.
- [ ] WebRTC VAD как лёгкий backend.
- [ ] Hybrid: silencedetect candidates + VAD verification.
- [ ] Адаптивный noise floor.
- [ ] Двухпороговый hysteresis.
- [ ] Не вырезать речь на фоне игры/музыки.
- [ ] Music/speech classifier.
- [ ] Сохранять смех, аплодисменты и реакции.
- [ ] Автокалибровка на sample окна.
- [ ] Noisy stream preset.

## 9.4. LLM content filter

- [ ] Создать `content.py`.
- [ ] Сегментировать transcript по темам/паузам.
- [ ] Категории low value, repetition, pause, donation, off-topic, high value, highlight.
- [ ] Обязательный dry-run.
- [ ] Reasoning для каждого решения.
- [ ] Audit JSON.
- [ ] Cache LLM responses.
- [ ] Local LLM backend.
- [ ] Anthropic/OpenAI-compatible backend.
- [ ] Custom prompt.
- [ ] Allowlist/denylist категорий.
- [ ] Manual approve/reject перед encode.
- [ ] Никогда не выполнять destructive LLM cut автоматически по умолчанию.

---

# Milestone 10 — главы, хайлайты и дополнительные output

## 10.1. Chapters

- [ ] Границы по теме, паузам и manual markers.
- [ ] Названия глав через transcript/LLM.
- [ ] Минимальная duration главы.
- [ ] Manual rename/merge/split.
- [ ] YouTube chapters.
- [ ] FFmetadata chapters.
- [ ] Markdown outline и JSON.

## 10.2. Highlights

- [ ] Scoring по громкости, эмоциям, смеху, chat density, keywords и LLM.
- [ ] Top N moments.
- [ ] Настраиваемый context до/после события.
- [ ] Preview и manual approve.
- [ ] Не создавать перекрывающиеся дубли.
- [ ] Экспорт отдельных MP4.
- [ ] Highlight reel.
- [ ] Reason для каждого highlight.

## 10.3. Shorts/Reels

- [ ] 9:16 output profile.
- [ ] Smart crop/face tracking.
- [ ] Background blur/fill.
- [ ] Burned captions.
- [ ] Active-word highlighting.
- [ ] Title overlay.
- [ ] Platform duration limits.
- [ ] Использовать существующие transcript/highlight caches.

## 10.4. Podcast/audio-only

- [ ] MP3, M4A/AAC и Opus output.
- [ ] Audio-only silence/filler/content pipeline.
- [ ] Podcast chapters.
- [ ] Embedded cover/metadata.
- [ ] Transcript и SRT рядом.
- [ ] RSS-ready metadata.

## 10.5. Audio processing

- [ ] EBU R128 loudness normalization.
- [ ] YouTube/podcast loudness presets.
- [ ] Denoise через `afftdn`.
- [ ] Optional RNNoise.
- [ ] Compressor/limiter/high-pass/de-esser.
- [ ] A/B preview.
- [ ] Destructive filters выключены по умолчанию.

## 10.6. Modern codecs

- [ ] H.264 compatibility profile.
- [ ] HEVC profiles: NVENC/AMF/QSV/VideoToolbox.
- [ ] AV1 profiles: NVENC/AMF/QSV/SVT-AV1.
- [ ] VP9/Opus WebM.
- [ ] Archive/YouTube/Small file/Fast/Mobile profiles.
- [ ] Capability detection и compatibility warnings.
- [ ] Estimate output size и encode time.

---

# Milestone 11 — очередь и автоматизация

## 11.1. Job queue

- [ ] Несколько URL/файлов.
- [ ] Drag-and-drop списка.
- [ ] CLI `--batch-file`.
- [ ] Playlist import.
- [ ] Twitch latest N VODs.
- [ ] Per-job preset/output profile.
- [ ] Priority, pause/resume/cancel.
- [ ] Retry policy.
- [ ] Общий CPU/RAM/VRAM budget.
- [ ] Job history и повтор failed jobs.

## 11.2. Watch folder

- [ ] Следить за папкой.
- [ ] Ждать завершения копирования файла.
- [ ] Preset rules по папке/имени.
- [ ] Не обрабатывать файл повторно.
- [ ] Архивировать processed input по настройке.
- [ ] Desktop notification по завершении.

## 11.3. Scheduler

- [ ] Ночная обработка.
- [ ] Пауза при активной работе пользователя.
- [ ] CPU budget по расписанию.
- [ ] Resume после сна/перезагрузки.
- [ ] Windows Task Scheduler integration.
- [ ] systemd timer/cron examples.

## 11.4. Remote workers — отдельный поздний трек

- [ ] Capability discovery CPU/RAM/VRAM/encoder/disk.
- [ ] Scheduler по требованиям job.
- [ ] Shared-folder и upload modes.
- [ ] Streaming logs/progress.
- [ ] Manifest и result transfer.
- [ ] Resume после worker disconnect.
- [ ] Реализовывать только после стабильной локальной queue.

---

# Milestone 12 — платформенные интеграции

## 12.1. Twitch

- [ ] Chat replay download.
- [ ] Chat density graph.
- [ ] Chat-based highlight score.
- [ ] Title/game/category metadata.
- [ ] Remap chat timestamps после cut.
- [ ] Удаление starting/ending technical screens.

## 12.2. YouTube

- [ ] Description template.
- [ ] Chapters export.
- [ ] Thumbnail frame picker.
- [ ] Title/tags metadata.
- [ ] Upload-ready output profile.
- [ ] SponsorBlock-like categories только как explicit optional source.

## 12.3. NLE integrations

- [ ] DaVinci Resolve.
- [ ] Premiere Pro.
- [ ] Final Cut.
- [ ] LosslessCut.
- [ ] Shotcut/Kdenlive.

---

# Milestone 13 — CLI, диагностика и отчёты

## 13.1. Команды

- [ ] `stream2video inspect INPUT`.
- [ ] `stream2video preview INPUT`.
- [ ] `stream2video benchmark INPUT`.
- [ ] `stream2video export PROJECT --format ...`.
- [ ] `stream2video doctor`.
- [ ] `stream2video rerun manifest.json`.
- [ ] Shell completion.

## 13.2. Итоговая статистика

- [ ] Duration и size до/после.
- [ ] Процент удалённого.
- [ ] Download/detect/encode wall time.
- [ ] Average/peak CPU/RAM/VRAM.
- [ ] Фактический encoder/fallback.
- [ ] FPS/frame count до/после.
- [ ] Количество ranges по source.
- [ ] JSON run report.

## 13.3. Reproducible manifest

- [ ] Source identity.
- [ ] Полная FFmpeg-команда.
- [ ] Версии tools.
- [ ] Encoder capabilities.
- [ ] Settings и ranges.
- [ ] Output metrics.
- [ ] Повтор запуска по manifest.

---

# Milestone 14 — дистрибуция

## 14.1. Сначала

- [ ] GitHub Release с portable ZIP.
- [ ] Проверка обновления yt-dlp.
- [ ] Безопасное обновление yt-dlp отдельно от приложения.
- [ ] Encoder/FFmpeg diagnostics.
- [ ] One-click diagnostics archive.
- [ ] Versioned cache/project formats.

## 14.2. После стабильного core

- [ ] PyPI package.
- [ ] Windows installer.
- [ ] Windows code signing.
- [ ] Docker CLI image.
- [ ] Docker STT image с optional model download.
- [ ] Linux AppImage/Flatpak.
- [ ] macOS bundle.

## 14.3. Низкий приоритет

- [ ] Website/demo video.
- [ ] Plugin catalog.
- [ ] Opt-in telemetry только при реальной необходимости.

---

# Milestone 15 — plugin architecture

- [ ] `Detector -> Range[]` interface.
- [ ] `Transcriber -> Transcript` interface.
- [ ] `Exporter -> Artifact` interface.
- [ ] `EncoderProfile -> FFmpeg options` interface.
- [ ] Plugin capability/version metadata.
- [ ] Sandboxed config validation.
- [ ] Не вводить публичный plugin API до стабилизации внутренних моделей.

---

# Рекомендуемая последовательность pull requests

1. [ ] PR01 — baseline media fixtures и failing regression.
2. [ ] PR02 — segment timestamps/frame correctness.
3. [ ] PR03 — batch timestamps и FPS policy.
4. [ ] PR04 — audio timeline и quality gate.
5. [ ] PR05 — audio quality presets.
6. [ ] PR06 — safe x264 и fallback policy.
7. [ ] PR07 — resource budget/watchdog.
8. [ ] PR08 — yt-dlp progress и connection UX.
9. [ ] PR09 — process supervisor и resume manifest.
10. [ ] PR10 — batch windowing/performance.
11. [ ] PR11 — true dry-run и streaming waveform.
12. [ ] PR12 — project file/manual ranges/presets.
13. [ ] PR13 — GUI split и tests.
14. [ ] PR14 — local STT + transcript cache.
15. [ ] PR15 — SRT/VTT remap.
16. [ ] PR16 — filler detector.
17. [ ] PR17 — VAD/hybrid detector.
18. [ ] PR18 — optional LLM content filter.
19. [ ] PR19 — chapters/highlights или podcast (выбрать один трек).
20. [ ] PR20 — queue/watch folder.

---

# Release gates

## Stable Core release

- [ ] Нет потери кадров вне документированной boundary policy.
- [ ] A/V drift не больше одного media frame.
- [ ] Source FPS сохраняется по умолчанию.
- [ ] Audio quality не понижается скрытно.
- [ ] HW encoder не fallback на unrestricted x264 без согласия.
- [ ] CPU/RAM/VRAM budget сохраняет резерв ОС.
- [ ] Download speed/ETA/connection states работают.
- [ ] Resume artifacts валидируются manifest+FFprobe.
- [ ] Ruff, format, mypy и pytest зелёные.

## Interactive Editor release

- [ ] Dry-run работает без предварительного pipeline.
- [ ] Пользователь может прослушать и изменить ranges.
- [ ] Project autosave/reopen работает.
- [ ] EDL/JSON export проходит round-trip test.

## STT release

- [ ] Local transcript cache работает и возобновляется.
- [ ] SRT синхронен в начале/середине/конце.
- [ ] Cloud upload требует подтверждения и показывает цену.
- [ ] Filler/VAD ranges доступны в dry-run до encode.

---

# Что не делать до завершения Stable Core

- [ ] Не добавлять автоматический destructive LLM cut.
- [ ] Не добавлять parallel encode без resource budget.
- [ ] Не использовать `-r 60` как маскировку FPS bug.
- [ ] Не использовать `preset=slow` на нестабильном CPU как default.
- [ ] Не делать cloud-only STT.
- [ ] Не начинать полноценный NLE.
- [ ] Не начинать remote workers раньше локальной queue.
- [ ] Не удалять source до quality gate.

---

# Ближайший практический спринт

## Спринт 1

- [ ] Добавить media reproduction tests.
- [ ] Исправить двойной seek.
- [ ] Удалить `N/FRAME_RATE/TB`.
- [ ] Проверить Source FPS.
- [ ] Исправить A/V duration.
- [ ] Добавить FFprobe quality gate.

## Спринт 2

- [ ] Audio quality presets.
- [ ] Safe x264 threads/presets.
- [ ] Fallback ask/disabled/enabled.
- [ ] RAM/CPU watchdog MVP.
- [ ] Исправить yt-dlp progress template.

## Спринт 3

- [ ] Resume manifest.
- [ ] Batch windowing.
- [ ] True dry-run.
- [ ] Streaming waveform peaks.
- [ ] Project JSON skeleton.

**После трёх спринтов:** повторно решить, что важнее — manual editor, STT+SRT или job queue.
