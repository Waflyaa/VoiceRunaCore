# PROJECT_STATE — Handoff (VoiceRunaCore)

> **Прочитай целиком перед первой командой.** Единственная рабочая папка —
> `C:\Dev\VoiceRunaCore`. Проект переименован из VoxRunaCore в **VoiceRunaCore**: Python-пакет
> теперь `sidecar/voiceruna`, данные — `%APPDATA%\VoiceRunaCore`. Никакие файлы с личными
> данными (ключи, модели, логи, имена пользователей) коммитить нельзя.

- **Рабочая папка:** `C:\Dev\VoiceRunaCore`
- **Репозиторий:** <https://github.com/Waflyaa/VoiceRunaCore> (ветка `main`)
- **Пуш:** только с явного разрешения владельца (публичный репозиторий).

---

## 1. Статус

| | Состояние |
|---|---|
| Версия | **v1.0-beta**, бренд VoiceRunaCore |
| Установщик | `VoiceRunaCore_0.1.0_x64-setup.exe`, identifier `com.waflyaa.voicerunacore` |
| Тесты | `580 passed, 1 skipped` (`cd sidecar && pytest -q`) |
| Миграция данных | при первом запуске `config.json`, `.env`, `apps.json` и `models/` переносятся из `%APPDATA%\VoxRunaCore`, старая папка удаляется |

Что изменилось при ребрендинге:

- Python-пакет `sidecar/voxruna` → `sidecar/voiceruna`, импорты и тесты обновлены;
- данные и логи — `%APPDATA%\VoiceRunaCore` (с миграцией из старой папки);
- сайдкар и установщик: `voiceruna-sidecar.exe`, продукт `VoiceRunaCore`, NSIS-идентификатор
  `com.waflyaa.voicerunacore`;
- имя ассистента больше не захардкожено: `wakeword.phrase` в конфиге, поле «Имя ассистента»
  в настройках, системный промпт подставляет это имя.

---

## 2. Архитектура

Tauri 2 (Rust) + Svelte 5/Vite + Python 3.12/FastAPI sidecar (два процесса, REST 127.0.0.1 + токен).

```
app/                Svelte UI + Tauri-оболочка (трей, хоткей, автозапуск, спавн сайдкара)
  src/  src-tauri/  (src-tauri/resources/sidecar/ — сюда build-app.ps1 копирует сайдкар)
sidecar/voiceruna/    app.py, runtime.py, voice/ (audio, vad, stt, wake, pipeline, tts, piper_tts),
                    llm/ (adapter, cascade, prompt), router/, tools/, config.py
sidecar/tests/      pytest
packaging/          pyinstaller spec, build-sidecar.ps1, build-app.ps1
docs/               CONTRACTS.md, ACCEPTANCE.md, SESSION-NOTES.md
```

Ключевые библиотеки: faster-whisper (STT, CPU int8), Piper (локальный TTS), Silero VAD,
Vosk (опциональный wake), sounddevice, httpx.

Данные пользователя: `%APPDATA%\VoiceRunaCore` (config.json, .env, apps.json, voices/, models/, logs/).

---

## 3. Что работает и что нельзя ломать

- **Анти-галлюцинации STT (слоистый guard):** AND-условие `no_speech > 0.6 и logprob < −1.0`,
  жёсткие границы `no_speech > 0.85` / `logprob < −1.5`, `compression_ratio > 3.0`, проверка
  плотности (≥ 10 с и < 1 симв/с — мусор), «спасение» плотной уверенной речи, детектор повторов,
  спектральная проверка «это речь», чёрный список (субтитры, PS4/PS5/PlayStation), одиночные
  слова не команды. Порог тишины относительный: шум + 10 dB.
- **AGC:** пик фразы нормализуется к `stt.agc_target_peak = 0.95` (потолок усиления 100×).
- **Адаптивный VAD/калибровка:** Silero VAD, порог речи = шум + 10 dB, калибровка по устройству
  в `audio.calibration`, кнопка «Калибровка микрофона», автокалибровка при старте и при смене
  устройства. Калибровка «мёртвого» потока отбраковывается и не затирает рабочую.
- **Wake word:** `vad_stt` — отдельный короткий сегментер (3.5 с), имя из `wakeword.phrase`
  ищется в первых трёх словах (по умолчанию вокс/вокса/вок/вос/vox), затем вырезается из
  команды; защита от эха TTS.
- **Устойчивость микрофона:** watchdog пропавших кадров — если поток «активен», но
  не отдаёт кадры 10 с (переподключение USB, сбой WDM-KS), он переоткрывается
  (не чаще раза в минуту); в /diagnostics есть last_frame_age_s и starved_reopens.
  Также потокобезопасный выход из THINKING: сбой LLM/инструмента больше не оставляет
  ассистента «глухим», и все переходы состояния логируются в StateManager.
- **Router:** «открой блокнот», «который час», «погода …», «папка …» — без LLM.
- **Инструменты:** whitelist `apps.json`, URI только `steam:`, 5 инструментов.
- **LLM:** каскад провайдеров, честные ошибки (нет ключа / 401-403 / нет сети), промпт разрешает
  болтовню, незнакомое действие → «Я пока не умею это делать».
- **TTS:** Piper → edge-tts → SAPI, обрезка хвостовой тишины, папка сторонних голосов.
- **Конфиг:** ремонт mojibake, миграции, свободные карты (`audio.calibration`) сохраняются,
  неизвестные ключи отбрасываются.

---

## 4. Voice improvements from Leon/OpenJarvis

Изучены открытые ассистенты и библиотеки, чтобы не изобретать VAD/AGC заново.

### Что нашли

**Leon** (`tcp_server/src/lib/asr/api.py`, `tcp_server/settings.json`):
- VAD = RMS по кадрам 1024 сэмпла @16 кГц: `audioop.rms(data, 2) >= rms_mic_threshold`,
  в настройках по умолчанию `rms_mic_threshold = 196` (≈ −44 dBFS), `silence_duration = 1` с.
- Порог в конфиге, а не в коде; калибруется под микрофон (у нас это делает автокалибровка).
- Whisper: faster-whisper, параметры `no_speech_threshold` / `log_prob_threshold` передаются явно.

**whisper-guard** (`whisper_guard/guard.py`):
- 4-слойный фильтр сегментов:
  1. `no_speech_prob > 0.8` и `avg_logprob < -1.5` (короткие сегменты −1.7);
  2. `compression_ratio > 3.0` — детектор «зацикливания» декодера;
  3. детектор повторов слов/символов (окно 6, порог 0.35; «а а а а», «лалалала»);
  4. «плотность» текста: chars/sec. Фраза, растянутая на 10+ секунд (< 1 симв/с) —
     галлюцинация, даже если `no_speech` низкий.
- Важный вывод авторов: **AND-условие само по себе не спасает** — реальные галлюцинации
  бывают с `avg_logprob -0.04…-0.24`; ловит именно плотность и повторы. И наоборот,
  реальная речь поверх музыки может иметь `no_speech ≈ 1.0`, поэтому нужно «спасение»:
  плотный и уверенный сегмент (≥ 2 симв/с, `avg_logprob > -0.5`, ≥ 5 с) не выбрасывать.

**pywebrtc-audio** (WebRTC APM): AGC делает то же, что наша нормализация — поднимает
уровень до целевого, но без обучения и без зависимости; у нас уже есть peak-AGC.

### Что внедрили у нас (без новых зависимостей)

| Паттерн | Где | Реализация |
|---|---|---|
| AND-фильтр | `voice/stt.py` | `no_speech > 0.6 AND avg_logprob < -1.0` → сегмент в мусор |
| Жёсткие границы | `voice/stt.py` | `no_speech > 0.85` **или** `avg_logprob < -1.5` — сразу мусор |
| compression_ratio | `voice/stt.py` | `> 3.0` → зацикленный декодер, выброс |
| Плотность | `voice/stt.py` | сегмент ≥ 10 с и < 1 симв/с → выброс |
| Спасение речи | `voice/stt.py` | плотный (≥ 2 симв/с) и уверенный (`logprob > -0.5`) сегмент ≥ 5 с остаётся |
| Повторы/циклы | `voice/stt.py` | одно слово 3+ раза подряд или периодический шаблон 1–4 символа |
| AGC | `voice/stt.py` `_normalize` | пик приводится к `stt.agc_target_peak = 0.95`, потолок `normalize_max_gain = 100` |
| Адаптивный VAD | `voice/pipeline.py` | порог = шум + 10 dB (`ENERGY_GATE_MARGIN_DB`), тишина = шум + 10 dB, калибровка по устройству |

Все пороги вынесены в `config.json → stt.*` (`drop_no_speech`, `drop_logprob`,
`extreme_no_speech`, `extreme_logprob`, `max_compression_ratio`, `sparse_min_sec`,
`sparse_density`, `rescue_*`, `agc_target_peak`), так что их можно подстраивать без сборки.
Тесты: `sidecar/tests/test_voice_stt.py` (AND, границы, compression, плотность, спасение, циклы).

Источники:
- <https://github.com/leon-ai/leon/tree/develop/tcp_server>
- <https://github.com/vulture-s/whisper-guard>
- <https://github.com/strands-labs/pywebrtc-audio>

---

## 4. Известные баги / следующие задачи

1. **🔴 Трей: при свёрнутом окне перестаёт слушать микрофон.** Изолировать аудио-поток в
   Python-сайдкаре (работа 24/7 в фоне). Смотреть: `app/src-tauri/src/window.rs`, `tray.rs`,
   `sidecar/voiceruna/voice/pipeline.py` (`pause/resume`, `_paused`), `/diagnostics` → `state`,
   `paused`; живы ли процессы при скрытом окне.
2. **🟡 README без скриншотов** — добавить в `README.md`.
3. **🟡 Мелочи:** решение по `whisper medium` (не скачан), обновление списка голосов в UI
   без перезапуска настроек.

---

## 5. Сборка и тесты (всё внутри release-папки)

Кэши сборки уже лежат здесь и **гитигнорятся**:

| Каталог | Назначение | Размер |
|---|---|---|
| `app/node_modules` | фронтенд-зависимости | ~72 МБ |
| `app/src-tauri/target` | cargo-артефакты + NSIS | ~8.7 ГБ |
| `packaging/.venv-build` | venv PyInstaller | ~487 МБ |
| `app/dist`, `packaging/dist`, `packaging/build` | результаты сборки | — |

Окружение: Python 3.12 (в PATH или полный путь к `python.exe`), Node 24,
Rust 1.99 + VS 2022 Build Tools.

```powershell
# тесты
cd C:\Dev\VoiceRunaCore-release\sidecar
python -m pytest -q                     # ожидаемо 567 passed, 1 skipped

# UI
cd ..\app
npm run build
npx svelte-check --threshold error

# Rust (быстрый чек)
cd src-tauri
cargo check

# сайдкар (frozen, onedir)
cd ..\..\packaging
.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist --workpath build pyinstaller\voiceruna-sidecar.spec

# установщик целиком (копирует сайдкар в resources + npm build + tauri build)
powershell -ExecutionPolicy Bypass -File .\build-app.ps1 -BuildSidecar
```

Готовый установщик: `app\src-tauri\target\release\bundle\nsis\VoiceRunaCore_0.1.0_x64-setup.exe`.
Тихая установка: `VoiceRunaCore_0.1.0_x64-setup.exe /S`; установленное приложение —
`%LOCALAPPDATA%\VoiceRunaCore\voiceruna.exe`.
MSVC: запускать сборку из «x64 Native Tools Command Prompt» (vcvars64) или через
внешний bootstrap-скрипт, который активирует vcvars64 перед запуском.

---

## 6. Логи и диагностика

- `%APPDATA%\VoiceRunaCore\logs\sidecar.log` — основной лог.
- `sidecar.out.log`, `sidecar.err.log` — stdout/stderr.
- `debug_mic.wav` / `debug_mic_raw.wav` — что ушло в Whisper и что поймал микрофон.
- REST (заголовок `X-Vox-Token`): `/health`, `/diagnostics`, `/audio/devices`,
  `/audio/test`, `/audio/calibrate`, `/stt/record_test`, `/voices`,
  `/voices/open_folder`, `/command`, `/providers/{id}/test`.

---

## 7. Правила работы

1. **Коммит на каждое изменение** с осмысленным сообщением; **пуш только после разрешения**
   владельца (публичный репозиторий).
2. **Перед коммитом — скан staged-диффа** на абсолютные пути пользователя, имена
   пользователей, префиксы API-ключей и внутренние пути инструментов. Никаких личных данных.
3. **Никогда не коммитить** `.env`, `models/`, `logs/`, `*.bin`, `*.onnx`,
   `node_modules`, `target`, отладочные `*.wav`.
4. **UIA/эмуляция кликов запрещены.** Только логи, HTTP API, анализ кода; UI проверяет владелец.
5. **Один писатель на файл.** `sidecar/voiceruna/voice/pipeline.py` большой; после правок —
   `python -c "import voiceruna.voice.pipeline"`, проверить одно вхождение `class VoicePipeline`,
   затем `pytest`.
6. **Контракты:** изменения API/конфига — сверять с `docs/CONTRACTS.md`.
7. **PowerShell/UTF-8:** не гонять config.json через `Get-Content | ConvertTo-Json | Set-Content`;
   правки конфигов — Python с UTF-8. Скрипты с кириллицей сохранять с BOM.
8. После правки сайдкара — пересобрать установщик и установить, чтобы владелец тестировал
   живую сборку, а не исходники.

---

## 8. Чек-лист старта сессии

1. `cd C:\Dev\VoiceRunaCore-release && git status && git log --oneline -3`
2. `cd sidecar && python -m pytest -q` — базовая линия зелёная.
3. Прочитать `docs/CONTRACTS.md` перед правками API.
4. Взять задачу из §4 (первая — трей) и воспроизводить только по логам/API.
5. Фикс → тесты → сборка → установка → просьба владельцу проверить → коммит.