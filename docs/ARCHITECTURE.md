# Архитектура и карта проекта

Этот документ объясняет, **как устроен проект и за что отвечает каждый файл**, чтобы можно
было продолжать разработку самостоятельно. Для правил и текущего статуса смотри
[`PROJECT_STATE.md`](../PROJECT_STATE.md), для контрактов API — [`CONTRACTS.md`](CONTRACTS.md).

---

## 1. Общая картина

Приложение состоит из **двух процессов**:

```
┌──────────────────────────────────────────────────────────────────┐
│ ПРОЦЕСС 1 — voiceruna.exe (Rust + Tauri 2 + WebView2)              │
│  окно Svelte · трей · глобальный хоткей · автозапуск · 1 экземпляр│
│  при старте: находит свободный порт и токен, запускает сайдкар     │
└───────────────┬──────────────────────────────────────────────────┘
                │ HTTP 127.0.0.1:<порт>  +  X-Vox-Token
                │ WebSocket /ws?token=…  (события в UI)
┌───────────────▼──────────────────────────────────────────────────┐
│ ПРОЦЕСС 2 — voiceruna-sidecar.exe (Python + FastAPI)               │
│  УШИ: микрофон → VAD → wake VoiceRunaCore → Whisper → текст              │
│  МОЗГ: Router (без сети) → иначе LLM (каскад провайдеров)         │
│  РУКИ: launch_application, open_folder, open_file, time, weather  │
│  ГОЛОС: Piper → edge-tts → SAPI                                   │
└──────────────────────────────────────────────────────────────────┘
```

- Сайдкар слушает **только 127.0.0.1** и требует токен: UI и внешние запросы без токена
  получают 401. Единственное исключение — `GET /health`.
- Саидкар — единственный владелец микрофона, распознавания, LLM, инструментов и настроек.
  Rust не умеет ни говорить, ни слушать: он только показывает окно и следит за процессом.
- Данные пользователя — в `%APPDATA%\VoiceRunaCore` (см. таблицу в §5).

### Что происходит при запуске

1. Rust (`sidecar.rs`) находит свободный порт, генерирует токен, запускает
   `sidecar\voiceruna-sidecar.exe --port <порт>` с переменной `VOX_TOKEN`.
2. Rust опрашивает `GET /health`, пока сайдкар не ответит (до 10 с); при падении —
   не больше 3 перезапусков за 60 с.
3. Сайдкар загружает `config.json`, поднимает FastAPI, запускает голосовой pipeline
   (микрофон, VAD, wake), при необходимости скачивает модели, отдаёт `/health`.
4. Окно получает порт и токен через Tauri-команду `get_connection` и открывает WebSocket.

---

## 2. Дерево проекта

```
app/                              интерфейс и оболочка
  index.html                      точка входа Vite
  package.json                    npm-скрипты: dev / build
  vite.config.ts                  конфиг сборки UI
  src/
    main.ts                       монтирование Svelte
    App.svelte                    главное окно: шапка, история, поле ввода, настройки
    app.css                       стили окна
    components/
      History.svelte              лента сообщений диалога
      InputBar.svelte             поле ввода + кнопка микрофона (POST /listen)
      StatusPill.svelte           индикатор «слушаю / думаю / говорю / пауза»
      SettingsDialog.svelte       модальные настройки: 5 вкладок + сохранение
      ApiKeyBanner.svelte         предупреждение «нет API-ключа»
      ConnectionGate.svelte       экран ожидания сайдкара
      SidecarBanner.svelte        ошибки сайдкара + «перезапустить»
      ToastHost.svelte            всплывающие уведомления
      Icon.svelte                 SVG-иконки
      tabs/
        AudioTab.svelte           устройства ввода/вывода, тесты, калибровка, диагностика
        VoiceTab.svelte           движок TTS, голос, скорость, прослушивание, папка голосов
        ModelsTab.svelte          провайдеры LLM, ключи, проверка ключа, модели
        AppsTab.svelte            реестр программ (apps.json)
        OtherTab.svelte           хоткей, фразы ошибок, город погоды, логи
    lib/
      api.ts                      HTTP-клиент сайдкара (токен, таймауты, ошибки)
      ws.ts                       WebSocket-подписка на события
      tauri.ts                    доступ к Tauri-командам
      store.svelte.ts             глобальное состояние UI (история, статус, тосты)
      types.ts                    типы ответов API
      validate.ts                 проверки полей перед сохранением
      config-diff.ts              минимальный patch при сохранении настроек
      format.ts                   форматирование текста/времени
  src-tauri/
    Cargo.toml, build.rs          сборка Rust
    tauri.conf.json               окно, трей, ресурсы (resources/sidecar → сайдкар)
    capabilities/default.json     разрешения Tauri
    src/
      main.rs                     запуск приложения
      lib.rs                      сборка Tauri: плагины, трей, окно, команды, автостарт
      sidecar.rs                  порт/токен, спавн сайдкара, health, рестарты, shutdown
      commands.rs                 Tauri-команды для UI (get_connection, hide_window, …)
      tray.rs                     иконка и меню трея
      window.rs                   показ/скрытие окна (крестик → в трей)
      state.rs                    общее состояние (порт, токен, процесс сайдкара)
    resources/sidecar/            сюда build-app.ps1 копирует собранный сайдкар
sidecar/                          Python-бэкенд
  requirements.txt                зависимости
  pyproject.toml                  метаданные/линтеры
  voiceruna/
    __main__.py                   точка входа: uvicorn на 127.0.0.1 + токен
    app.py                        все REST- и WS-маршруты FastAPI
    runtime.py                    оркестратор: текст → Router/LLM → инструмент → ответ
    config.py                     config.json: схема по умолчанию, миграции, .env
    paths.py                      все пути (%APPDATA%, модели, логи, голоса)
    state.py                      состояния ассистента (idle/listening/thinking/…)
    events.py                     формат событий для UI
    ws.py                         рассылка событий подписчикам WebSocket
    logging_setup.py              файловые логи, ротация
    apps.py                       реестр программ apps.json (whitelist)
    security.py                   валидация путей, файлов, папок, URI, .lnk
    llm/
      adapter.py                  OpenAI-совместимый клиент (DeepSeek/OpenRouter/…)
      cascade.py                  перебор провайдеров по priority + типы ошибок
      prompt.py                   сборка системного промпта
    router/
      normalize.py                очистка текста («вокс, открой доту» → «доту»)
      engine.py                   быстрые правила: алиасы, шаблоны, фаззи
    tools/
      base.py                     ToolResult (ok/speech/payload/error)
      executor.py                 инструменты (+ ToolRegistry) и проверка аргументов
      weather.py                  погода: Open-Meteo + резерв wttr.in
    voice/
      audio.py                    захват микрофона, выбор формата, воспроизведение
      vad.py                      Silero VAD + нарезка фраз (UtteranceSegmenter)
      wake.py                     wake word: Vosk KWS (опционально)
      stt.py                      faster-whisper + анти-галлюцинации
      pipeline.py                 сердце голоса: петля, состояния, wake, калибровка
      tts.py                      синтез: edge-tts/SAPI, плееры, выбор движка
      piper_tts.py                локальный нейроголос Piper (офлайн)
      download.py                 скачивание моделей с прогрессом
  tests/                          pytest: 567 тестов на всё вышеперечисленное
packaging/
  pyinstaller/launcher.py         точка входа для PyInstaller
  pyinstaller/voiceruna-sidecar.spec  сборка onedir
  build-sidecar.ps1               собрать сайдкар
  build-app.ps1                   собрать установщик (сайдкар + UI + Rust + NSIS)
docs/                             контракты, приёмка, заметки, этот файл
spike/, tools/                    эксперименты и утилиты (не входят в сборку)
```

---

## 3. Поток голосовой команды

```
микрофон (sounddevice)
   └─► audio.SoundDeviceMic: 16 кГц (или родная частота + ресемпл), очередь кадров
        └─► pipeline._run(): читает кадры в отдельном потоке
             ├─► vad.SileroVad.is_speech()      «это речь?»
             ├─► pipeline._energy_speech()      уровень выше шума + спектральная проверка
             └─► UtteranceSegmenter             начало фразы / 1.5 с тишины / до 30 с
                  └─► pipeline._run_wake_check() (режим vad_stt)
                       ├─► stt.WhisperTranscriber (strip_wake=False)
                       ├─► VoiceRunaCore в первых трёх словах? нет → игнор
                       └─► да → команда без слова VoiceRunaCore
                            └─► runtime.handle_text()
                                 ├─► router.Router.route(): алиас/шаблон → инструмент
                                 └─► иначе LLM → tool_call или текст
                                      └─► tools.executor: запуск/папка/файл/время/погода/громкость/файл в песочнице/громкость/файл в песочнице
                                           └─► tts: Piper → edge → SAPI → динамики
```

Текстовые команды из окна идут короче: `POST /command {text}` → `runtime.handle_text()`
(тот же Router/LLM/инструменты), ответ возвращается в UI и озвучивается.

### Состояния

| Состояние | Когда |
|---|---|
| `idle` | ждёт wake-слово или команду |
| `listening` | фраза (wake/кнопка/хоткей), после ответа — follow-up |
| `thinking` | распознаёт или ждёт LLM |
| `speaking` | озвучивает ответ |
| `paused` | микрофон выключен из трея |
| `downloading` | качает модели |
| `error` | микрофон/STT недоступны |

---

## 4. Поток текстовой команды и инструментов

1. UI (`InputBar`) → `api.ts` → `POST /command`.
2. `runtime.py`:
   - нормализует текст и отдаёт `Router`;
   - Router вернул инструмент → `tools.executor` выполняет;
   - Router не уверен → `llm.cascade.complete()`: провайдер по `priority`, при 401/403
     провайдер отключается, при отсутствии ключей — «не найден API ключ», при сети — «нет связи»;
   - LLM вернул `tool_call` → тот же executor; иначе текст ответа.
3. Ответ: событие в UI (`transcript`, `answer`, `state`) + озвучка через `tts`.

Инструменты (единственные, что существуют; новые регистрируются через ToolRegistry):

| Инструмент | Аргументы | Что делает |
|---|---|---|
| `launch_application` | `application_id` | запускает программу из `apps.json` (path или uri) |
| `open_folder` | `folder_id` | открывает desktop/downloads/documents |
| `open_file` | `folder_id`, `filename` | открывает файл в известной папке |
| `get_time` | — | локальное время |
| `get_weather` | `city?` | погода (Open-Meteo, резерв wttr.in) |
| `set_volume` | `action`, `level?` | громкость через pycaw |
| `create_file` | `filename`, `content?` | .txt/.md только в Documents\VoiceRunaCore |

---

## 5. Настройки и данные

`%APPDATA%\VoiceRunaCore`:

| Путь | Что внутри |
|---|---|
| `config.json` | все настройки (см. ключи ниже) |
| `.env` | ключи API (`DEEPSEEK_API_KEY=`, `OPENROUTER_API_KEY=`) — **не коммитить** |
| `apps.json` | реестр программ/папок (whitelist) |
| `voices/` | сторонние голоса Piper (`*.onnx` + `*.onnx.json`) |
| `models/` | whisper-small, silero-vad, piper, vosk |
| `logs/sidecar.log` | основной лог |
| `logs/debug_mic.wav` | что ушло в Whisper (при `debug.save_audio=true`) |
| `logs/debug_mic_raw.wav` | что поймал микрофон |

Основные ключи `config.json`:

| Ключ | Смысл |
|---|---|
| `audio.input_device` / `output_device` | `"default"` или `"index:N"` |
| `audio.calibration` | шум/порог по каждому устройству `{"index:N": {...}}` |
| `stt.model` / `beam_size` / `min_peak_dbfs` / `max_no_speech` | распознавание |
| `vad.silence_ms` (1500) / `max_utterance_sec` (30) / `wake_max_sec` | endpointing и длина фразы |
| `wakeword.engine` (`vad_stt`/`vosk`/`none`), `phrase` | имя/слово-будильник |
| `wakeword.followup_window_sec` | окно продолжения диалога без слова-будильника |
| `tts.engine` (`auto`/`piper`/`edge`/`sapi`), `voice`, `rate` | голос |
| `llm.providers[]` | `id`, `enabled`, `base_url`, `api_key_env`, `model`, `priority` |
| `llm.timeout_sec` | таймаут запроса к LLM |
| `errors.*` | фразы: не расслышал, нет ключа, нет связи и т. п. |
| `logging.enabled/level` | файловые логи |
| `debug.save_audio` / `raw_seconds` | отладочные WAV |

`.env` читается при старте; ключи также можно задать в «Настройки → Модели» — UI пишет их
в `.env`, а не в `config.json`.

---

## 6. REST API сайдкара

Все маршруты, кроме `/health`, требуют заголовок `X-Vox-Token: <токен>`.

| Метод и путь | Назначение |
|---|---|
| `GET /health` | живость + состояние |
| `GET /state`, `POST /pause`, `POST /resume` | состояние и пауза микрофона |
| `POST /command {text}` | текстовая команда (Router/LLM/инструменты) |
| `POST /listen`, `POST /repeat`, `POST /stop_speaking` | управление голосом |
| `GET/PUT /config`, `PUT /env` | настройки и ключи |
| `GET /providers/{id}/models`, `POST /providers/{id}/test` | LLM-провайдеры |
| `GET /voices`, `POST /voices/open_folder`, `POST /tts/preview` | голоса и озвучка |
| `GET /audio/devices`, `POST /audio/test`, `POST /audio/calibrate` | устройства, проверка, калибровка |
| `POST /stt/record_test {seconds}` | записать и распознать (кнопка «Тест распознавания») |
| `GET /diagnostics` | всё для отладки «почему не слышит» |
| `GET/PUT /apps` | реестр программ |
| `POST /shutdown` | корректно завершить сайдкар |
| `WS /ws?token=…` | события: state, transcript, answer, download, error |

---

## 7. Как продолжить самому: типовые задачи

### Добавить программу в запуск
«Настройки → Программы» → путь (`%LOCALAPPDATA%\...`) или URI (`steam://rungameid/570`)
и алиасы («доту», «dota»). Можно править `%APPDATA%\VoiceRunaCore\apps.json` вручную.
Запускается **только** то, что есть в этом файле.

### Добавить новый инструмент (новое действие)
1. `sidecar/voiceruna/tools/executor.py`: имя в `TOOL_NAMES`, описание в `TOOL_DEFINITIONS`,
   ветка в `execute()` и реализация `_my_tool()`.
2. `sidecar/voiceruna/llm/prompt.py`: добавить имя в список инструментов.
3. При необходимости — правило в `router/engine.py`.
4. Тесты в `sidecar/tests/test_tools.py`; затем `pytest -q`.

### Научить Router новой фразе без LLM
`router/normalize.py` (какие слова-команды выкидывать) и `router/engine.py`
(алиасы, регулярки `TIME_RE`, `WEATHER_RE`, `FOLDER_RE`). Проверка: `tests/test_router*.py`.

### Изменить характер ответов (промпт)
`llm/prompt.py`, константа `_BASE_PROMPT`. Проверяется `tests/test_llm.py::test_build_system_prompt`.

### Поменять модель распознавания
«Настройки → Модели» (если есть выбор) или `config.json → stt.model` = `small`/`base`/`medium`
(модель скачается автоматически, это сотни МБ). Скорость против точности.

### Подстроить чувствительность микрофона
Кнопка **«Калибровка микрофона»** (Настройки → Звук) — лучший путь: пороги станут
`шум + 10 dB`. Ручные ключи: `stt.min_peak_dbfs`, `vad.silence_ms`, `wakeword.listen_timeout_sec`.
Код: `voice/pipeline.py` (`_apply_noise_floor`, `calibrate`), `voice/stt.py` (фильтры).

### Добавить/сменить голос
Свой Piper-голос: файлы `имя.onnx` + `имя.onnx.json` в `%APPDATA%\VoiceRunaCore\voices\`,
затем «Настройки → Голос» → «Сохранить» и выбрать «Piper: имя».
Код: `voice/piper_tts.py` (`local_voices`, `voice_path`), `voice/tts.py` (`list_voices`).

### Добавить настройку в UI
1. `config.py` → `DEFAULTS` (ключ и тип).
2. `app/src/lib/types.ts` → тип, `app/src/lib/validate.ts` → проверка.
3. Вкладка в `app/src/components/tabs/*.svelte` → поле.
4. Сохранение уже работает через `PUT /config` (diff в `config-diff.ts`).

### Добавить REST-эндпоинт
`sidecar/voiceruna/app.py` (Pydantic-модель тела + `@app.post(..., dependencies=[Depends(require_token)])`),
затем метод в `app/src/lib/api.ts` и тест в `tests/test_api.py`.

### Поменять хоткей / поведение окна
Хоткей: `config.json → hotkey` (и чтение в Rust `lib.rs`/`tray.rs`).
Крестик → трей: `app/src-tauri/src/window.rs`. Трей: `tray.rs`. Спавн сайдкара: `sidecar.rs`.

---

## 8. Отладка: где смотреть

1. **Лог:** `%APPDATA%\VoiceRunaCore\logs\sidecar.log` (включить: `logging.enabled=true`,
   `logging.level="debug"`).
2. **Диагностика:** `GET /diagnostics` → `voice.mic`, `voice.vad`, `voice.stt`, `voice.wake`,
   `voice.tts`, `providers`. В UI — Настройки → Звук → «Диагностика».
3. **Отладочные WAV:** `logs/debug_mic.wav` (что ушло в Whisper) и `debug_mic_raw.wav`
   (что поймал микрофон) — включаются `debug.save_audio=true`.

Частые строки лога и что они значат:

| Строка | Значит |
|---|---|
| `microphone opened: … at N Hz, M ch` | какой формат захвата выбран |
| `calibration: noise floor … gate …` | пороги применены (шум + 10/+8 dB) |
| `calibration rejected: peak …` | калибровка на мёртвом потоке отклонена |
| `wake: heard '…'` | что услышал wake-детектор |
| `wake: ignored speech without the wake word` | фраза без VoiceRunaCore — правильно проигнорирована |
| `stt: buffer does not look like speech` | шум/эхо, инференс не запускался |
| `stt: dropping a segment with no_speech=…` | фильтр галлюцинаций сработал |
| `stt: peak … below …, skipping` | тише порога тишины |
| `input level: peak … rms … zeros …` | живой уровень микрофона каждые 2 с |
| `microphone unavailable: …` | устройство не открылось (занято/выключено) |

Типичные причины «не слышит»: выбрано не то устройство ввода; микрофон выключен/заглушён
в Windows; тихий вход (низкий уровень); калибровка сделана в шуме; микрофон камеры с
аппаратным гейтом. Лечится: «Проверить микрофон» → «Калибровка» → тест распознавания.

---

## 9. Сборка, тесты, установка

```powershell
# из корня репозитория (C:\Dev\VoiceRunaCore)
# тесты бэкенда
cd sidecar
python -m pytest -q

# фронтенд
cd ..\app
npm run build
npx svelte-check --threshold error

# сайдкар (frozen)
cd ..\packaging
.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist --workpath build pyinstaller\voiceruna-sidecar.spec

# установщик (сайдкар + UI + Rust + NSIS)
powershell -ExecutionPolicy Bypass -File .\build-app.ps1 -BuildSidecar
# или без пересборки фронта:
powershell -ExecutionPolicy Bypass -File .\build-app.ps1 -SkipFrontend
```

Результат: `app\src-tauri\target\release\bundle\nsis\VoiceRunaCore_0.1.0_x64-setup.exe`.
Установка: `...setup.exe /S` (тихо). Установленное приложение:
`%LOCALAPPDATA%\VoiceRunaCore\voiceruna.exe`.

Для сборки Rust нужен MSVC: запускай из «x64 Native Tools Command Prompt» или через
bootstrap-скрипт, который вызывает `vcvars64.bat`.

---

## 10. Подводные камни (проверено на практике)

- **`pipeline.py` большой и чувствительный.** Один писатель на файл; после правок проверяй
  `python -c "import voiceruna.voice.pipeline"` и `pytest`.
- **UTF-8 и PowerShell 5.1.** Не гоняй `config.json` через `Get-Content | ConvertTo-Json |
  Set-Content` — получишь mojibake. Правь Python'ом с `encoding="utf-8"`. Скрипты с
  кириллицей сохраняй с BOM.
- **Ключи только в `.env`.** `config.json` не должен содержать ключей; в git их тоже быть не должно.
- **Не коммитить модели/логи:** `models/`, `logs/`, `*.bin`, `*.onnx`, отладочные `*.wav`.
- **Порт и токен.** Без `X-Vox-Token` все запросы к сайдкару — 401. Токен приходит из Rust
  через `get_connection`.
- **Whisper галлюцинирует на шуме.** Не отключай фильтры в `stt.py` — на тишине он «слышит»
  «Смотрите на видео», «PS5» и т. п.
- **UIA и эмуляция кликов запрещены** — проверяй через логи и HTTP API, UI тестирует владелец.
- **Релизный репозиторий публичный** — пуш только с разрешения владельца.
