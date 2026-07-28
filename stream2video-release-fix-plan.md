# stream2video — план исправлений перед релизом

> План по итогам код-ревью от 27.07.2026. Приоритеты: **P0** — блокер релиза, **P1** — сильно желательно в этот релиз, **P2** — в следующую версию.
> Рекомендуемый порядок работы: этапы 1 → 2 → 3 выполняются последовательно (блокеры), этапы 4–5 можно делать параллельно, этап 6 — последним.

---

## Этап 0. Подготовка

- [x] Создать ветку `fix/pre-release-audit-2` от актуального `master`
- [x] Прогнать текущий набор тестов (`pytest -q`) и зафиксировать базовую точку: все ~487 тестов зелёные
- [x] Прогнать `ruff check .` и `mypy stream2video/` (если настроены), зафиксировать текущее число предупреждений

---

## Этап 1 (P0). Реестр процессов: preview стирает чужую регистрацию

**Проблема:** `detect_silence_stream` регистрирует процесс как `owner="preview"`, но в `finally` вызывает `set_active_process(None)` без owner — чистится слот `"default"` (где может сидеть ffmpeg пайплайна), а запись `"preview"` остаётся висеть. `waveform.py` регистрирует `"preview"` и не снимает регистрацию вовсе.

### 1.1. Контекст-менеджер регистрации (utils.py)

- [x] Добавить в `utils.py` контекст-менеджер `registered_process(proc, owner="default")`:
  - [x] в `__enter__` — `set_active_process(proc, owner=owner)`
  - [x] в `__exit__` — `set_active_process(None, owner=owner)` (тот же owner по построению)
- [x] Убрать fallback на `"default"` в `get_active_process(owner)` (`_proc_registry.get(owner) or _proc_registry.get("default")`) — запрос несуществующего owner должен возвращать `None`, а не чужой процесс
- [x] Проверить все вызовы `get_active_process()` (в т.ч. `gui_lifecycle.py:244`) — убедиться, что после удаления fallback поведение при закрытии GUI не сломалось

### 1.2. Точечные фиксы вызовов

- [x] `silence.py` `detect_silence_stream` (~строка 551): заменить `set_active_process(None)` на `set_active_process(None, owner="preview")` **или** перевести на `registered_process(proc, owner="preview")`
- [x] `waveform.py` `read_peaks_from_stream` (~строка 115): добавить снятие регистрации `"preview"` во всех путях выхода (успех, `Exception`, `TimeoutExpired`) — лучше через контекст-менеджер
- [x] Перевести остальные пары «регистрация/снятие» на контекст-менеджер: `concat.py` (`_run_ffmpeg`, ~716/865), `download.py` (~381/510), `silence.py` (`_run_silencedetect`, ~678/876 и ~930/965)

### 1.3. Тесты

- [x] Тест: preview завершился во время активного `default`-процесса → регистрация `default` НЕ снята
- [x] Тест: после завершения preview запись `"preview"` удалена из реестра (нет stale-записей)
- [x] Тест: `get_active_process("nonexistent")` возвращает `None`, а не default-процесс
- [x] Регрессионный тест: `cancel_process("preview")` во время preview убивает именно preview-процесс

---

## Этап 2 (P0). Resume в audio extract не работает

**Проблема:** `_run_audio_extract` валидирует сегменты через `_ffprobe_is_valid_mp4`, которая делает `ffprobe -select_streams v` и требует непустой вывод. У аудиофайлов видеопотока нет → валидация всегда `False` → на resume все сегменты молча перекодируются заново.

- [x] Параметризовать валидатор: `_ffprobe_is_valid_media(path, stream_type: str = "v")` (или отдельная `_ffprobe_is_valid_audio`)
  - [x] для аудиоформатов использовать `-select_streams a`
  - [x] сохранить прежнее поведение для видео-путей (`_run_segment_concat`, `_run_cut_then_encode`, raw concat) — там `stream_type="v"`
- [x] В `_run_audio_extract` передавать `stream_type="a"` при проверке `seg_*.mp3/opus/aac/wav/flac`
- [x] Переименовать `_ffprobe_is_valid_mp4` → нейтральное имя (сейчас имя вводит в заблуждение: проверяются и mkv, и аудио)
- [x] Проверить, нет ли других вызовов старого валидатора на не-видео файлах (grep по проекту)

### Тесты

- [x] Тест: валидный аудиофайл (сгенерировать ffmpeg'ом `sine` → mp3/wav) проходит валидацию с `stream_type="a"`
- [x] Тест: пустой/обрезанный аудиофайл валидацию НЕ проходит
- [x] Интеграционный тест resume: первый запуск audio extract прерывается после N сегментов → повторный запуск переиспользует готовые сегменты (проверить по mtime/логу «skipped»), а не перекодирует их

---

## Этап 3 (P0). `_run_cut_then_encode`: фаза cut без защиты

**Проблема:** phase 1 (stream-copy сегментов) использует голый `subprocess.run(check=True, capture_output=True)`: нет timeout, нет `set_active_process` (Cancel не убьёт идущий cut), `CalledProcessError` не входит в `exc_types=(ConcatError, OSError)` у `_with_libx264_fallback` и улетает наверх сырым исключением без stderr ffmpeg.

- [x] Заменить `subprocess.run` на общий раннер:
  - [ ] вариант A (предпочтительно): прогнать cut-фазу через `_run_ffmpeg(..., track_progress=False)` — таймаут, cancel, stall-watchdog и регистрация процесса «бесплатно»
  - [x] вариант B (минимальный): добавить `timeout=` (например, `_SEGMENT_ENCODE_TIMEOUT`), `registered_process(...)`, обернуть `CalledProcessError` в `ConcatError` с усечённым stderr (`_STDERR_TRUNCATE`)
- [x] ⚠️ Если выбран вариант A: сначала выполнить пункт 4.1 (в `track_progress=False`-ветке stall-watchdog убьёт процесс через `stall_kill`, т.к. `last_progress_time` не обновляется — для cut-фазы либо отключать watchdog, либо обновлять таймер по факту чтения stderr)
- [x] Убедиться, что cancel проверяется и ВО ВРЕМЯ cut-а (не только между сегментами)
- [x] Проверить обработку ошибок cut-фазы в `pipeline_controller` / CLI: пользователь должен видеть дружелюбное сообщение `ConcatError` со stderr, а не traceback

### Тесты

- [x] Тест: ошибка ffmpeg в cut-фазе (битый вход) → `ConcatError` с фрагментом stderr, без `CalledProcessError` наружу
- [x] Тест: cancel во время cut-фазы завершает процесс и поднимает `CancelledError`
- [x] Тест: cut-фаза с timeout → корректный `FFmpegError`/`ConcatError`, процесс убит

---

## Этап 4 (P1). Диагностика: SIGKILL ≠ OOM

**Проблема:** `looks_like_oom(rc=-9)` срабатывает на любой SIGKILL. Если процесс убил stall-watchdog (поток-дублёр), главный цикл получает EOF → rc=-9 → пользователю сообщают «ran out of memory», хотя причина — stall (гонка с inline-проверкой).

### 4.1. Флаг stall-kill в `_run_ffmpeg` (concat.py)

- [x] Завести `stall_killed = threading.Event()`; выставлять его в `_stall_watchdog` ПЕРЕД `process.kill()`
- [x] В блоке анализа `returncode != 0`: если `stall_killed.is_set()` — поднимать `FFmpegError("... stalled — no progress for Ns")` ДО проверки `looks_like_oom`
- [x] Аналогично проверить `memory_monitor`: hard-kill по памяти должен репортиться как OOM/budget, а не как generic fail (там как раз OOM-ветка уместна — убедиться, что она срабатывает)
- [x] Проверить симметричную логику в `silence.py` (`_run_silencedetect`) — есть ли там аналогичная гонка

### 4.2. Тесты

- [x] Тест: процесс убит stall-watchdog'ом → сообщение про stall, НЕ про OOM
- [x] Тест: rc=-9 без stall-флага (реальный OOM-kill) → по-прежнему `FFmpegOutOfMemoryError`

---

## Этап 5 (P1). Мелкие баги

### 5.1. `download.py` — legacy-парсинг прогресса

- [x] `_parse_progress_line`: условие `if len(parts) < 5 and len(parts) < 4` — мёртвая логика (эквивалентно `< 4`). Выбрать одно из:
  - [x] честно реализовать 4-полевой fallback: `downloaded|total_estimate|speed|eta` → правильное соответствие полей (сейчас `total_estimate` получает speed, `speed` получает eta)
  - [ ] или удалить мёртвую ветку и устаревший комментарий, ужесточив до `len(parts) < 5: return None`
- [x] Тест на выбранный вариант (строка из 4 полей парсится правильно ИЛИ отвергается)

### 5.2. `detect_silence_stream` может висеть вечно (silence.py)

- [x] Заменить блокирующий `iter(pipe.readline, b"")` на `read_lines_queue` + `get(timeout=CANCEL_POLL_INTERVAL)` (как в `_run_ffmpeg`), чтобы параметр `timeout` реально работал и зависший ffmpeg не вешал preview
- [x] Добавить проверку cancel в цикл чтения (сейчас preview останавливается только через `cancel_process("preview")` снаружи)
- [x] Тест: ffmpeg, не пишущий в stderr и не завершающийся → preview падает по timeout, процесс убит

### 5.3. `-fflags +genpts` в финальном concat (concat.py)

- [x] Проверить на реальном multi-segment файле, влияет ли флаг в текущей позиции (после `-i`, как выходная опция) на PTS результата
- [x] Если нет — перенести `-fflags +genpts` перед `-i` (входная опция демуксера) и перепроверить A/V-синхронизацию на длинном файле
- [x] Обновить комментарий «rebuilds the final PTS» по фактическому поведению

---

## Этап 6 (P1–P2). Улучшения и полировка

### 6.1. Убрать глобальное состояние `_audio_quality` (concat.py)

- [ ] Протащить `audio_quality` параметром в `_audio_bitrate()` / `_audio_opts()` и все места использования
- [ ] Удалить module-level `_audio_quality` и его присваивание в `cut_and_concat`
- [ ] Тест: два последовательных вызова `cut_and_concat` с разным `audio_quality` не влияют друг на друга (и smoke-тест на потокобезопасность, если планируется parallel encode)

### 6.2. Унификация парсера silencedetect (P2.5 доделать)

- [ ] Перевести inline-парсер в `_run_silencedetect` на общий `SilenceParser` (сейчас unified-парсер используется только в stream-версии — риск расхождения логики)
- [ ] Убедиться, что тесты на decimal comma (P1.13) покрывают оба пути

### 6.3. Косметика

- [ ] `concat.py`: удалить дубликат `_MIN_PART_BYTES` (объявлен ~строки 145 и ~880)
- [ ] `concat.py`: исправить устаревший комментарий у `_AUDIO_BITRATE = "128k"` («medium keeps the historical 128k» — medium уже 192k)
- [ ] `utils.py`: исправить битую кодировку в docstring `subprocess_kwargs` (`â¨"preexec_fn"`)
- [ ] Прогнать grep по проекту на другие следы битой кодировки: `rg -n '[â€™Ã]' stream2video/`

### 6.4. CI и релизная гигиена

- [ ] Настроить/проверить GitHub Actions: `pytest` на матрице ОС (Ubuntu + **Windows** — много Windows-специфики: h264_mf, `no_window_kwargs`, priority class; macOS опционально)
- [ ] Добавить в CI `ruff check` и `mypy` (или зафиксировать отказ от mypy осознанно)
- [ ] Зафиксировать секцию Unreleased в `CHANGELOG.md` как версию **0.3**, проставить дату
- [ ] Поднять версию в `pyproject.toml` до 0.3
- [ ] Обновить `stream2video-fix-plan.md`: отметить пункты этого плана как новый раздел аудита

---

## Этап 7. Финальная проверка перед релизом

- [ ] Полный прогон тестов на Linux и Windows: все зелёные
- [ ] Ручной smoke-тест на длинном VOD (2ч+), метод segment: запуск → Cancel в середине encode → процесс убит, temp-файлы на месте → повторный запуск → resume подхватывает готовые сегменты
- [ ] Ручной smoke-тест: preview волны/тишины параллельно с энкодом → после завершения preview кнопка Cancel по-прежнему убивает ffmpeg пайплайна (регрессия этапа 1)
- [ ] Ручной smoke-тест audio-only формата (mp3/opus) с прерыванием и resume (регрессия этапа 2)
- [ ] Ручной smoke-тест метода cut+encode с намеренно битым входом → дружелюбная ошибка, не traceback (регрессия этапа 3)
- [ ] Просмотреть diff целиком, squash/упорядочить коммиты, обновить README при необходимости
- [ ] Тег релиза `v0.3`, сборка, публикация
