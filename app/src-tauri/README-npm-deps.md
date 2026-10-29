# npm-зависимости для app/ (для Lead и UI-агента)

Этот файл создан агентом `tauri-shell`, у которого нет прав менять `app/package.json`
(это зона UI-агента). Здесь точный список того, что нужно добавить в `app/package.json`.

## Уже есть в app/package.json

| Пакет | Версия | Зачем |
|---|---|---|
| `@tauri-apps/api` | `^2.12.4` | `invoke` и `listen` из окна |
| `svelte`, `vite`, `typescript`, `svelte-check` | — | фронтенд |

## Требовалось добавить (уже сделано 2026-10-28)

```powershell
cd app
npm install --save-dev @tauri-apps/cli@^2.12.4
```

На момент отчёта `@tauri-apps/cli ^2.12.4` и скрипты `tauri`/`tauri:dev`/`tauri:build`
уже есть в `app/package.json` — отдельных действий не требуется.
Без `@tauri-apps/cli` не работают `npm run tauri dev` / `npx tauri build`
(`packaging/build-app.ps1` проверяет `app/node_modules/.bin/tauri.cmd` и падает с подсказкой).

## Необязательно (только если UI будет вызывать плагины из JS)

Rust-сторона уже делает трей, хоткей, автозапуск и открытие папки конфига — JS-пакеты
плагинов для этого **не нужны**. Они могут пригодиться настройкам (спринт 8):

| Пакет | Версия | Когда нужен |
|---|---|---|
| `@tauri-apps/plugin-autostart` | `^2.7.0` | тумблер «автозапуск» в настройках |
| `@tauri-apps/plugin-opener` | `^2.7.2` | если UI сам захочет открывать папку конфига вместо `invoke('open_config_folder')` |
| `@tauri-apps/plugin-global-shortcut` | `^2.4.1` | смена хоткея из UI (сейчас регистрируется в Rust) |

`@tauri-apps/plugin-single-instance` не существует — у плагина нет JS-команд.

## Требование ТЗ §4

Версии Tauri фиксируются минорной: ставьте `^2.x` и коммитьте `app/package-lock.json`.
Крейты в `app/src-tauri/Cargo.toml` объявлены как `"2"`, точные версии зафиксирует
`app/src-tauri/Cargo.lock` при первой сборке с Rust (сейчас Rust в среде нет, lock не создан).
