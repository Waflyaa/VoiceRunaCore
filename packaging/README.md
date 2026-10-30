# Сборка VoiceRunaCore

Приложение состоит из двух процессов:

1. **Оболочка** — Tauri 2 (Rust) + Svelte в WebView2: окно, трей, хоткей, автозапуск,
   single instance, жизненный цикл сайдкара. Исходники: `app/src-tauri`.
2. **Сайдкар** — Python 3.12 + FastAPI, собранный PyInstaller **onedir**.
   Исходники: `sidecar/`. В установщике лежит как ресурс `sidecar/`.

Установщик один: NSIS (`bundle.targets = ["nsis"]`), ставится **на текущего пользователя**
без UAC (`installMode: currentUser`).

---

## 1. Требования тулчейна

| Компонент | Версия | Зачем |
|---|---|---|
| Rust | stable, MSVC toolchain (`rustup default stable-x86_64-pc-windows-msvc`) | компиляция Tauri |
| MSVC Build Tools | Visual Studio Build Tools, «Desktop development with C++» + Windows 10/11 SDK | линковка Rust/MSVC |
| Node.js + npm | LTS 20+ / npm 10+ | фронтенд и `@tauri-apps/cli` |
| Python | **3.12** (TZ §4) | PyInstaller-сборка сайдкара |
| NSIS / WebView2 bootstrap | ставит bundler сам | установщик |

Плагины Tauri из workspace v2 требуют свежий stable (**Rust ≥ 1.90** на момент написания);
ставьте последний stable, а не древний пин.

Проверка:

```powershell
rustc --version; cargo --version
node --version; npm --version
py -3.12 --version
```

Если Rust/MSVC нет — `packaging/build-app.ps1` падает с понятным списком того, что доставить
(и не пытается ничего собирать).

---

## 2. Порядок сборки

```powershell
# 0) иконки (уже сгенерированы в app/src-tauri/icons; пересоздать при смене дизайна)
py -3.12 tools/make-icons.py

# 1) сайдкар: venv + зависимости + PyInstaller onedir -> packaging/dist/sidecar/
powershell -ExecutionPolicy Bypass -File packaging/build-sidecar.ps1 -Clean

# 2) установщик: сайдкар в ресурсы -> npm ci && npm run build -> tauri build -> NSIS
powershell -ExecutionPolicy Bypass -File packaging/build-app.ps1
```

`build-app.ps1` умеет сразу и сайдкар: `-BuildSidecar` (тогда шаг 1 не нужен).
Фронтенд уже собран — `-SkipFrontend`.

**Готовый установщик:**

```
app/src-tauri/target/release/bundle/nsis/VoiceRunaCore_1.1.0_x64-setup.exe
```

Dev-режим (сайдкар запускается из исходников через Python, сайдкар собирать не нужно):

```powershell
cd app
npm install          # нужен @tauri-apps/cli (см. app/src-tauri/README-npm-deps.md)
npx tauri dev        # Rust сам поднимет "python -m voiceruna --port <port>"
```

Переменные окружения для dev:

| Переменная | Смысл |
|---|---|
| `VOX_PYTHON` | путь к python.exe с зависимостями сайдкара (по умолчанию ищется `<repo>/.venv\Scripts\python.exe`, затем `packaging/.venv\Scripts\python.exe`, затем `python`) |
| `VOX_SIDECAR_DIR` | каталог `sidecar/` с исходниками (по умолчанию вычисляется от `app/src-tauri`) |
| `VOX_DATA_DIR` | переопределить `%APPDATA%\VoiceRunaCore` (полезно для тестов) |

---

## 3. Почему onedir и как он попадает в установщик (ловушка ТЗ §3)

`externalBin` в Tauri рассчитан на **один** файл. PyInstaller `onedir` — это
`voiceruna-sidecar.exe` **плюс** папка `_internal` с DLL и данными. Поэтому:

* `externalBin` **не используется**;
* папка `packaging/dist/sidecar/` целиком копируется в `app/src-tauri/resources/sidecar/`
  (`build-app.ps1`), а `bundle.resources` отдаёт её в установщик как `sidecar/`;
* Rust запускает `resource_dir()/sidecar/voiceruna-sidecar.exe` (на Windows
  `resource_dir()` — каталог установки, т.е. `<install>\sidecar\voiceruna-sidecar.exe`);
* `onefile` не годится: распаковка в `%TEMP%` на каждом старте даёт 2–4 с холодного старта.

Связка в `app/src-tauri/tauri.conf.json`:

```json
"bundle": {
  "resources": {
    "resources/sidecar/": "sidecar/"
  }
}
```

**Почему копирование, а не `"../../packaging/dist/sidecar/": "sidecar/"` из корня репозитория:**
`tauri-build` копирует `bundle.resources` в `target/<profile>` на **каждой** сборке, включая
`cargo check`/`tauri dev`. Если источником указать собранный сайдкар, то `tauri dev` перестанет
запускаться без стомегабайтной PyInstaller-сборки. Поэтому в репозитории есть пустой каталог
`app/src-tauri/resources/sidecar/.gitkeep`:
— в dev он пустой (сайдкар запускается через Python);
— `build-app.ps1` перед релизной сборкой чистит его и копирует настоящий onedir,
  затем проверяет наличие `voiceruna-sidecar.exe` и `_internal`;
— сами скопированные файлы в git не попадают (`app/src-tauri/.gitignore`).

Структура ресурса после установки:

```
<install>\VoiceRunaCore.exe
<install>\sidecar\voiceruna-sidecar.exe
<install>\sidecar\_internal\...   (python312.dll, onnxruntime, ctranslate2, vosk, ...)
```

Проверка onedir после `build-sidecar.ps1`: скрипт падает, если нет `_internal` — это
признак `onefile` или PyInstaller < 6.

---

## 4. Что делает Rust-оболочка

| Файл | Ответственность |
|---|---|
| `lib.rs` | сборка приложения, порядок инициализации, single instance, хоткей, автозапуск, `RunEvent` |
| `state.rs` | `AppState`: токен X-Vox-Token, `%APPDATA%\VoiceRunaCore`, `Mutex<SidecarState>`, счётчик перезапусков |
| `sidecar.rs` | свободный порт, запуск, мини-HTTP-клиент, `GET /health` (10 с), автоперезапуск (3 раза / 60 с), `taskkill /T /F` |
| `window.rs` | окно из конфига + injection `window.__VOX__`, показ/скрытие, папка конфига |
| `tray.rs` | иконка трея, меню, пауза микрофона, «приглушённая» иконка |
| `commands.rs` | Tauri-команды для UI |

Поведение по ТЗ:

* окно 460×640 (min 380×480), по центру, **не** always-on-top, при показе выходит вперёд
  и получает фокус;
* крестик = скрыть в трей (`CloseRequested` -> `prevent_close` + `hide`); `Esc` — через
  `invoke('hide_window')` (обрабатывает UI);
* выход — только пункт «Выход» в трее: `POST /shutdown` -> убить дерево процессов -> `app.exit(0)`;
* хоткей `Ctrl+Alt+V` (или `hotkey.activate` из `config.json`): показать окно, сфокусировать,
  сразу `POST /listen`; если комбинация занята — лог + событие `vox-hotkey-error`, приложение живёт;
* автозапуск включён по умолчанию с аргументом `--hidden` (в dev-сборке реестр не трогаем);
* второй запуск показывает окно первого (`tauri-plugin-single-instance`, первый плагин);
* при выходе сайдкар убивается: `taskkill /PID <pid> /T /F`, затем `child.kill()` + `wait()`.

---

## 5. Контракт для UI (`app/src`, зона UI-агента)

### 5.1. Подключение к сайдкару

При создании окна Rust выполняет initialization_script, поэтому до скриптов страницы уже есть:

```js
window.__VOX__ // { port: number, token: string, app: "VoiceRunaCore" }
```

Если переменной нет (страховка) — `await invoke('get_connection')` вернёт то же самое.

* REST: `http://127.0.0.1:{port}/...`, заголовок `X-Vox-Token: {token}`
  (исключение — `GET /health`, ему токен не нужен);
* WS: `ws://127.0.0.1:{port}/ws?token={token}`.

`port === 0` означает «сайдкар не запустился»: покажите ошибку и кнопку, вызывающую
`invoke('restart_sidecar')`. Порт **может измениться** после автоперезапуска — подписывайтесь
на `vox-connection` и обновляйте `port/token`.

`Esc` в окне — зона UI (TZ §10.1): обработчик клавиши должен вызвать `invoke('hide_window')`.
В Rust слушателя клавиш нет намеренно: он не нужен, пока окно в фокусе, а UI всё равно
владеет раскладкой и модальными состояниями.

### 5.2. Tauri-команды (`@tauri-apps/api/core` -> `invoke`)

| Команда | Аргументы | Возврат |
|---|---|---|
| `get_connection` | — | `{ port: number, token: string }` |
| `hide_window` | — | `null` |
| `open_config_folder` | — | `null` (создаёт `%APPDATA%\VoiceRunaCore`, открывает проводник) |
| `restart_sidecar` | — | `null`; ждёт `/health` до 10 с, при неудаче отклоняется строкой |
| `sidecar_status` | — | `{ running: boolean, port: number, restarts: number, error: string \| null }` |

### 5.3. События из Rust (`@tauri-apps/api/event` -> `listen`)

| Событие | Payload | Когда |
|---|---|---|
| `vox-connection` | `{ port, token }` | сайдкар поднялся / перезапустился (порт мог смениться) |
| `vox-sidecar` | `{ running, healthy, port, restarts, paused, error }` | любой снимок статуса |
| `vox-error` | `{ message }` | ошибка для тоста (сайдкар не поднялся, пауза недоступна) |
| `vox-paused` | `{ paused }` | переключение «Пауза микрофона» из трея |
| `open-settings` | `null` | трей -> «Настройки»: открыть вкладку настроек |
| `vox-hotkey-error` | `{ message }` | `Ctrl+Alt+V` занят другой программой |

### 5.4. Эндпоинты сайдкара, которые использует оболочка

`GET /health` (без токена), `POST /listen` (хоткей), `POST /pause` и `POST /resume`
(трей, «Пауза микрофона»), `POST /shutdown` (выход). Остальное UI зовёт сам.

---

## 6. Данные, логи, диагностика

* `%APPDATA%\VoiceRunaCore\` — `config.json`, `.env`, `apps.json`, `voices.json`, `models\`, `logs\`.
  Папку создаёт и Rust, и сайдкар (TZ §13).
* `%APPDATA%\VoiceRunaCore\logs\sidecar.out.log` и `sidecar.err.log` — stdout/stderr сайдкара
  (в т.ч. строка `voiceruna-sidecar <version> listening on http://127.0.0.1:<port>`).
  Это не логи диалогов: `logging.enabled` по-прежнему выключен по умолчанию.
* Ошибка «не найден сайдкар» -> не собран `packaging/build-sidecar.ps1` или установщик собран
  без него; окно показывает кнопку «перезапустить сайдкар» (`invoke('restart_sidecar')`).
* Проверка после выхода: `tasklist | findstr voiceruna-sidecar` должен быть пуст.

---

## 7. Ограничения и осознанные решения

1. **Rust здесь не компилировался**: в среде нет Rust/MSVC. Код написан под Tauri 2 API,
   `tauri.conf.json` проверен по официальной схеме `https://schema.tauri.app/config/2`
   (jsonschema, draft-07 — ошибок нет), Rust-файлы разобраны tree-sitter-rust (ошибок синтаксиса
   нет), PS-скрипты проверены `PSParser`. Первая реальная сборка может потребовать мелких правок.
2. **CSP = `null`** (как в шаблоне Tauri): страница локальная. Когда CSP будем включать,
   `connect-src` должен разрешать только `http://127.0.0.1:*` и `ws://127.0.0.1:*`
   плюс `ipc:`/`http://ipc.localhost`.
3. **`single-instance` не имеет разрешений** в capabilities: у плагина нет JS-команд,
   `single-instance:default` не существует (сборка упала бы). Работает целиком в Rust.
4. **Автозапуск в dev не включается** (`cfg!(debug_assertions)`), чтобы не прописать в реестр
   `target\debug\voiceruna.exe`. В release включается один раз на первом запуске и ставит маркер
   `%APPDATA%\VoiceRunaCore\.autostart-initialized`, чтобы не перебивать будущий выбор
   пользователя в настройках.
5. **Хоткей** читается из `config.json` (`hotkey.activate`) на старте; если файла нет —
   `ctrl+alt+v`. Смена хоткея требует перезапуска приложения (как и в ТЗ §11).
6. **Состояние «пауза»** хранится в сайдкаре; трей обновляет его через `POST /pause|/resume`.
   Если пауза включена из UI, иконка трея обновится только после действия из трея —
   двустороннюю синхронизацию добавим в спринте 8 через `GET /state`.
7. **Окно не always-on-top** (ТЗ §10.1): при показе делаем `show()` + `set_focus()`.
8. **Окно описано с `"create": false"`** — его создаёт Rust в setup, чтобы успеть
   подложить `window.__VOX__` до загрузки страницы.
9. **`.ps1` файлы — UTF-8 с BOM**. Windows PowerShell 5.1 иначе читает кириллицу как ANSI
   и ломает разбор. Не сохраняйте их без BOM.
10. Иконки рисует `tools/make-icons.py` (stdlib, без Pillow), результат детерминирован:
    `32x32.png`, `128x128.png`, `128x128@2x.png` (256), `icon.png` (256), `icon.ico`
    (кадры 32/128/256) и `tray-muted.png` для паузы.
11. Иконка трея в паузе — отдельный файл `icons/tray-muted.png` (серая «V» с оранжевой чертой),
    подменяется через `TrayIcon::set_icon`.
12. **Сайдкар копируется дважды** при релизной сборке: сначала `build-app.ps1` в
    `app/src-tauri/resources/sidecar`, затем `tauri-build` в `target/<profile>/sidecar`,
    затем bundler кладёт его в установщик. Это цена за рабочий `tauri dev` без PyInstaller.
    Для 100–150 МБ (цель ТЗ §16) это десятки секунд; если сайдкар заметно распухнет,
    вернёмся к прямому пути `../../packaging/dist/sidecar/` в конфиге.
13. **Вес.** Замер первой реальной onedir-сборки (PyInstaller 6.22.3, Python 3.12):
    `packaging/dist/sidecar` = **316.6 МБ / 1121 файл** (exe + `_internal`). Это больше
    ориентира ТЗ §16 (100–150 МБ на установщик), но ТЗ допускает превышение с фиксацией:
    основной вклад дают onnxruntime, ctranslate2/faster-whisper, vosk и edge-tts.
    Размер **NSIS-установщика** (со сжатием) нужно замерить после первого `build-app.ps1`
    и записать в корневой README. Если понадобится ужать — кандидаты: отказ от vosk
    как wake-движка (если спайк выберет openWakeWord) и `--exclude` неиспользуемых
    submodule'ов onnxruntime.

---

## 8. Быстрый чек-лист приёмки (для этой части)

- [ ] `packaging/build-sidecar.ps1` -> `packaging/dist/sidecar/voiceruna-sidecar.exe` + `_internal\`.
- [ ] `npm run build` в `app/` -> `app/dist/index.html`.
- [ ] `packaging/build-app.ps1` -> NSIS-установщик; после установки есть
      `<install>\sidecar\voiceruna-sidecar.exe` и `<install>\sidecar\_internal\`.
- [ ] После установки: трей, клик по иконке — окно, `Ctrl+Alt+V` — окно + слушание.
- [ ] Крестик скрывает; выход через трей; повторный запуск показывает первое окно.
- [ ] `tasklist | findstr voiceruna-sidecar` пуст после выхода.
