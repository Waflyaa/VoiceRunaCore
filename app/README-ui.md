# VoiceRunaCore — окно и редактор настроек (frontend)

Svelte 5 (runes) + TypeScript + Vite. Без UI-библиотек и иконочных шрифтов: CSS свой,
иконки — inline SVG (`src/components/Icon.svelte`). Окно рассчитано на 460×640 и тянется.

## Запуск

```powershell
cd app
npm install
npm run build      # прод-сборка в dist/ (без Rust)
npm run dev        # Vite dev-сервер, http://127.0.0.1:1420
npm run check      # svelte-check: TypeScript + a11y + CSS
```

Внутри Tauri ничего из этого не нужно: Rust грузит собранный `dist/` и инжектит
`window.__VOX__ = { port, token }` до загрузки страницы.

Dev-сервер Vite слушает `http://localhost:1420` — ровно как `build.devUrl` в
`app/src-tauri/tauri.conf.json`.

## Как окно находит сайдкар

`src/lib/tauri.ts`, порядок приоритетов:

1. `window.__VOX__` (Tauri-инжект, боевой путь);
2. query-строка `?port=8000&token=...` (отладка в браузере);
3. `localStorage` (`vox.port`, `vox.token`) — запоминается после первого подключения.

Если ничего нет — в Tauri вызывается `invoke('get_connection')` (страховка на случай,
если сайдкар поднялся позже или сменил порт); в браузере показывается форма
«Подключение к сайдкару».
Все REST-запросы идут на `http://127.0.0.1:{port}` с заголовком `X-Vox-Token`.

Контракт оболочки: `packaging/README.md` §5. Используются команды `get_connection`,
`hide_window`, `open_config_folder`, `restart_sidecar`, `sidecar_status` и события:

| Событие | Что делает UI |
|---|---|
| `vox-connection {port, token}` | переключает клиент на новый порт, переподключает WS |
| `vox-sidecar {running, healthy, paused, error, …}` | статус сайдкара, пауза, текст ошибки |
| `vox-error {message}` | toast |
| `vox-paused {paused}` | состояние «Пауза» (тумблер в трее) |
| `open-settings` | открывает редактор настроек из трея |
| `vox-hotkey-error {message}` | toast про занятый Ctrl+Alt+V |

Если сайдкар не отвечает, окно показывает баннер «Сайдкар не отвечает» с кнопкой
«Перезапустить сайдкар» (`invoke('restart_sidecar')`, TZ §3).

Если WS пришёл `error` с `kind="no_api_key"` или `kind="auth"`, под шапкой остаётся
постоянный баннер (`ApiKeyBanner`): «API ключ не найден» / «Ключ не принят провайдером»,
вторая строка — имена переменных из сообщения (например `OPENROUTER_API_KEY`), кнопка
«Открыть настройки» открывает вкладку «Модели». Баннер снимается успешным `answer`
(не совпадающим с фразой ошибки) или сохранением ключа.

## Структура

| Файл | Что делает |
|---|---|
| `src/App.svelte` | каркас окна: шапка, статус, история, ввод, тосты, Esc |
| `src/lib/types.ts` | типы контракта (config, provider, voices, apps, WS-события) |
| `src/lib/api.ts` | типизированный REST-клиент + русские тексты ошибок |
| `src/lib/ws.ts` | `/ws?token=...`, автопереподключение 0.5 → 5 с |
| `src/lib/store.svelte.ts` | состояние UI: события WS → история, действия окна |
| `src/lib/tauri.ts` | `__VOX__`, invoke `hide_window` / `open_config_folder` |
| `src/lib/format.ts` | подписи состояний, время, строки tool_call |
| `src/lib/config-diff.ts` | минимальный патч для `PUT /config` |
| `src/lib/validate.ts` | проверки перед сохранением настроек |
| `src/components/` | `History`, `InputBar`, `StatusPill`, `Toast`, `SettingsDialog`, `ApiKeyBanner`, `SidecarBanner` |
| `src/components/tabs/` | `ModelsTab`, `VoiceTab`, `AudioTab`, `AppsTab`, `OtherTab` |

## Окно (ТЗ §10.1)

- История: реплики пользователя (источник «голос»/«окно», время), ответы, tool_call
  одной строкой («▶ Dota 2» / «✕ Dota 2 — отказ»), ошибки.
- Ввод: Enter → `POST /command`; микрофон → `POST /listen`; «Повторить» → `POST /repeat`.
- У последней реплики пользователя — «править»: текст подставляется в поле ввода
  и отправляется заново (ремонт после ошибки, не гейт — ТЗ §2.4).
- Статус: текст + цвет для idle/listening/thinking/speaking/paused/error/downloading,
  при загрузке модели — прогресс-бар.
- «Настройки» (встроенный редактор), «Открыть папку конфига» (`open_config_folder`),
  «В трей» (`hide_window`), Esc — скрыть окно.
- Постоянный баннер про отсутствующий/отвергнутый API-ключ с переходом в «Модели»
  (см. события ниже), вместо исчезающего тоста.

## Редактор настроек (ТЗ §11)

- **Модели**: карточки провайдеров (id, label, base_url, api_key_env, model,
  supports_tools, priority, enabled), «Добавить»/«Удалить», «Проверить»
  (`POST /providers/{id}/test`), «Загрузить модели» (`GET /providers/{id}/models`)
  с подстановкой выбранной модели. Поле «Ключ» пишет в `.env` через `PUT /env`
  `{name, value}` — в `config.json` ключ не попадает (ТЗ §7.2), под полем это написано
  явно (с путём `%APPDATA%\VoiceRunaCore\.env`). У каждого провайдера бейдж из
  `GET /diagnostics`: «ключ не найден» (`key_present=false`) или «ключ есть»; список
  обновляется при открытии вкладки и после сохранения ключа. «Проверить» показывает
  ответ сервера как есть (например «Ключ не задан: OPENROUTER_API_KEY»).
- **Голос**: «Движок синтеза» (`tts.engine`) — «Авто (онлайн edge-tts, иначе
  офлайн SAPI)» = `auto`, «Только онлайн edge-tts» = `edge`, «Только офлайн SAPI` =
  `sapi`, с подсказкой про установленные голоса Windows; список `GET /voices` +
  `tts.voices` из конфига, скорость (`+10%`), «Прослушать» (`POST /tts/preview`),
  добавление своего голоса `{engine, name, label}`.
  Результат прослушивания показывается строкой: `ok=true` → «Прозвучало: <message>»,
  `ok=false` → красным текст причины («нет устройства вывода», «edge-tts: NoAudioReceived»)
  и кнопка «Проверить устройство вывода» с переходом на вкладку «Звук».
- **Звук**: список устройств из `GET /audio/devices` — микрофон и динамики/наушники
  (`select` + «Обновить список»), под выбором — `hostapi` и число каналов.
  «Проверить звук» → `POST /audio/test {kind:"output"}`, «Проверить микрофон» →
  `POST /audio/test {kind:"input"}`; показывается сообщение сервера и «Уровень: NN%»
  (при `level < 0.01` — «тишина»). Пустой список микрофонов — предупреждение, пустой
  список вывода — красная строка с `playback.message`. «По умолчанию» хранится как
  `default`, конкретное устройство — как `index:<номер>`; `device` для теста уходит
  как `null` (по умолчанию) или `index:<номер>`.
  Блок «Тест распознавания» → `POST /stt/record_test {seconds:3}`: кнопка
  «Записать 3 секунды и распознать» (фазы «Записываю… 3 с» → «Распознаю…», остальной
  UI не блокируется), результат — крупно «Распознано: «…»» / «ничего не распознано»,
  метрики «Уверенность: 0.85 · Обработка: 1.2 с · VAD: 0.4–2.7 с», при `ok=false` —
  красный `message`, ниже полный `wav_path` («Запись сохранена: …», «Пришлите этот
  файл, если текст неверный»). Если эндпоинта нет (404/405/501) — мягкое «Тест пока
  недоступен». Рядом вторичный самотест без микрофона:
  `POST /audio/test {kind:"stt"}` (SAPI синтезирует фразу, её распознаёт тот же STT).
  Сворачиваемый блок «Диагностика» (`GET /diagnostics`) показывает состояние, микрофон,
  sample rate (фактический/родной), VAD, wake и последнее `last_heard`, STT (ready/model/
  loaded/error), модель STT (имя + МБ), последнюю уверенность/время, TTS
  (engine/backend/last.message), провайдеров с наличием ключа, последнюю отладочную
  запись (`debug.last_wav`) и пути к `models`/`logs`.
- **Программы**: `apps.json` — id, display_name, path/uri, алиасы через запятую;
  добавление, удаление (с подтверждением), переименование; сохранение `PUT /apps`.
- **Прочее**: фразы ошибок, город, порог wake, таймауты VAD/wake/LLM
  (устройства ввода/вывода — только на вкладке «Звук», дублей нет).

Сохранение — `PUT /config` только с изменёнными ветками (deep-diff), затем `PUT /apps`
при правках реестра. Аудио-ветка уходит как `{"audio":{"input_device":"…","output_device":"…"}}`
(только изменённые поля). Перезапуск не нужен; после успеха — «Сохранено».

## Проверено

- `npm run build` — Vite собирает `dist/`, TS-ошибок нет.
- `npm run check` — svelte-check: 0 ошибок, 0 предупреждений (TS, a11y, CSS).
- Окно и все вкладки сняты headless-Edge 460×640: горизонтального скролла нет.
- Функциональный прогон против mock-сайдкара (24 проверки, вне репозитория):
  проверка провайдера, список моделей, `PUT /env`, добавление провайдера, голоса и
  `tts/preview`, добавление программы, сохранение `PUT /config` (патч только с
  изменёнными ветками: `llm,tts,weather`) и `PUT /apps`, повторное «Сохранено/Нет изменений»,
  закрытие по Esc. Харнесс: `headless-харнесс`.
- Прогон против **реального** сайдкара из `packaging/.venv-build` (13/13, временный
  `VOX_DATA_DIR`, без внешних ключей): WS-авторизация `?token=`, реальные `/config`,
  `/apps` (10 программ), `/voices`, сохранение `PUT /config` (город изменился, остальные
  секции не тронуты), живой диалог «который час» → «Сейчас 01:50» через WS, строка
  tool_call «▶ время», пауза из `POST /pause` отражается в окне, ошибок CORS/JS нет.
- Аудио-флоу (по требованию владельца — без UI-автоматизации, только HTTP + статика)
  **37/37**: `GET /audio/devices` (input/output/current/playback), тест вывода ok и
  fail (NoAudioReceived), тест микрофона с `level` 0 (тишина) и 0.42, `/tts/preview`
  ok/fail, `PUT /config` только с `audio` и только с изменёнными полями (остальные
  ветки не тронуты), 401 без токена, плюс статические проверки проводки
  `api.ts`/`AudioTab`/`VoiceTab`/`SettingsDialog`. Харнесс:
  `ui-harness/audio-http.mjs` + обновлённый `mock-sidecar.mjs`.
  Браузерные скриншоты новой вкладки в этот раз не снимались (запрет на UI-автоматизацию),
  поэтому визуальную часть и реальный звук проверяет владелец.
- Раунд «ключи/диагностика/движок» (только HTTP + статика) **47/47**:
  `GET /diagnostics` (paths, providers с `key_present`, voice-цепочка, tts.last.message),
  `POST /audio/test {kind:"stt"}` с `text`, WS-события `error` c `kind=no_api_key`
  (в сообщении есть имена ENV) и `kind=auth`, плюс статические проверки
  баннера/эха фразы/бейджей/диагностики/движка. Харнесс:
  `ui-harness/round3-http.mjs`. Регрессия аудио-флоу — **41/41** (`audio-http.mjs`).
- `GET /diagnostics` на **реальном** сайдкаре: ключи верхнего уровня, `paths`,
  `providers` с `key_present`, `voice` (`mic/vad/wake/stt/tts/voices/models_dir`),
  `tts.last = {ok, engine, message, detail}` — совпало с типами UI.
- Раунд «Тест распознавания» (только HTTP + статика) **35/35**:
  `POST /stt/record_test` ok (text/confidence/processing_s/wav_path/vad/mic),
  ok=false с `message` и `confidence=null`, 401 без токена, новые поля
  `/diagnostics` (`voice.stt.model_path/model_size_mb/last_confidence/last_processing_s`,
  `voice.debug.last_wav/save_audio/raw_wav`, `voice.wake.last_heard`,
  `voice.mic.samplerate/native_samplerate`), плюс статические проверки проводки,
  фаз кнопки, метрик, пути WAV и мягкого 404. Харнесс:
  `ui-harness/round4-http.mjs` (mock: `seconds=1` → ошибка распознавания).
  Регрессии: round3 **47/47**, audio **41/41**.
- `GET /audio/devices` на **реальном** сайдкаре (`packaging/.venv-build`, temp
  `VOX_DATA_DIR`): 8 микрофонов и 4 устройства вывода, поля совпадают с типами
  (`index,name,channels,hostapi,default`), `current` отдаёт значения конфига
  (`"default"`/`"index:N"` — UI это учитывает), `playback.ok=false` с причиной:
  на этой машине вывод звука действительно не поднимается — именно поэтому
  «Прослушать» раньше молчал, теперь вкладка показывает красную ошибку и текст причины.

Без Rust/Tauri нельзя проверить: инжект `window.__VOX__`, команды `hide_window` /
`open_config_folder` / `restart_sidecar` / `get_connection`, события `vox-*`, размер
и поведение окна, трей и закрытие крестиком.

## Интеграционные заметки

- Команды и события оболочки — из `packaging/README.md` §5; вне Tauri они дают toast/no-op.
- Порт сайдкара может смениться после автоперезапуска: UI слушает `vox-connection`.
- CORS для origin окна (`http://tauri.localhost`) включён в `sidecar/voiceruna/app.py`
  (`CORSMiddleware`); preflight с `X-Vox-Token` проверен вживую на реальном сайдкаре.
- `PUT /env` — отдельный эндпоинт для ключей; при его отсутствии UI сообщает
  «Сайдкар не поддерживает сохранение ключа».
- WS `error.kind`: `no_api_key` / `auth` → постоянный баннер; `llm` / `runtime` /
  `tts` и остальные — только строкой в истории.
- Важно: при `no_api_key`/`auth` сайдкар после `error` отправляет `answer` с той же
  фразой. UI не считает такой ответ успешным (`isIssueEcho`), иначе баннер исчезал бы
  мгновенно.
- `GET /diagnostics` расширяется по мере готовности voice-pipeline: отсутствующие поля
  UI просто не показывает (значение «—»), `POST /stt/record_test` при 404/405/501
  даёт «Тест пока недоступен», а при серверной ошибке — красный текст, не падение.
