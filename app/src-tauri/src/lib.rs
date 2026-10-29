//! Оболочка «Ассистента» на Tauri 2: окно, трей, хоткей, автозапуск, single instance
//! и жизненный цикл Python-сайдкара (TZ §3, §10).
//!
//! Порядок инициализации в setup:
//!   1. найти порт, сгенерировать токен, запустить сайдкар (процесс), /health — в фоне;
//!   2. создать окно и подложить в него `window.__VOX__` до загрузки страницы;
//!   3. трей, глобальный хоткей, автозапуск;
//!   4. показать окно, если это не автозапуск с `--hidden`.

mod commands;
mod sidecar;
mod state;
mod tray;
mod window;

use tauri::{AppHandle, Emitter, Manager, RunEvent};
use tauri_plugin_global_shortcut::{
    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutEvent, ShortcutState,
};

use crate::state::AppState;

#[derive(Clone, Debug, serde::Serialize)]
struct HotkeyError {
    message: String,
}

pub fn run() {
    let app = tauri::Builder::default()
        // single-instance регистрируется первым: второй запуск только показывает окно первого.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            window::show_main(app);
        }))
        // Автозапуск: Windows/macOS запускают приложение с `--hidden` (TZ §10.3).
        .plugin(tauri_plugin_autostart::Builder::new().arg("--hidden").build())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        // opener: открытие папки конфига (Rust-API, без scope-проверок JS-команды).
        .plugin(tauri_plugin_opener::init())
        .manage(AppState::new())
        .invoke_handler(tauri::generate_handler![
            commands::get_connection,
            commands::hide_window,
            commands::open_config_folder,
            commands::restart_sidecar,
            commands::sidecar_status,
        ])
        .setup(|app| {
            let handle = app.handle().clone();

            // 1. Сайдкар: порт + токен + процесс. /health ждём в фоне, чтобы окно не ждало 10 с.
            let sidecar_started = match sidecar::start(&handle) {
                Ok(()) => {
                    sidecar::spawn_health_watch(&handle);
                    true
                }
                Err(err) => {
                    eprintln!("[vox] {err}");
                    handle.state::<AppState>().mark_error(err.clone());
                    sidecar::emit_error(&handle, &err);
                    sidecar::emit_status(&handle);
                    false
                }
            };
            // Супервизор живёт всегда: если сайдкар поднимут кнопкой из окна, он тоже под надзором.
            sidecar::spawn_monitor(&handle);

            // 2. Окно: в нём уже есть window.__VOX__ = { port, token }.
            if let Err(err) = window::create(&handle) {
                return Err(Box::<dyn std::error::Error>::from(format!(
                    "не удалось создать главное окно: {err}"
                )));
            }

            // 3. Трей и хоткей: их отсутствие не должно убивать приложение.
            if let Err(err) = tray::create(&handle) {
                eprintln!("[vox] трей не создан: {err}");
            }
            register_hotkey(&handle);
            ensure_autostart(&handle);

            // 4. Обычный запуск показывает окно; автозапуск (--hidden) — только трей.
            //    Если сайдкар не поднялся, окно показываем даже при --hidden: иначе ошибку не увидеть.
            let hidden = handle.state::<AppState>().start_hidden;
            if !hidden || !sidecar_started {
                window::show_main(&handle);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // Крестик = скрыть в трей; выход — только пункт «Выход» в трее (TZ §10).
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("не удалось создать приложение VoiceRunaCore");

    app.run(|app_handle, event| match event {
        // Сайдкар убивается вместе с деревом процессов; повторный вызов безопасен.
        RunEvent::ExitRequested { .. } => sidecar::shutdown(app_handle),
        RunEvent::Exit => sidecar::shutdown(app_handle),
        _ => {}
    });
}

/// Ctrl+Alt+V (по умолчанию) из `config.json`; если строка битая или клавиши заняты —
/// пробуем жёсткий дефолт и, при неудаче, только логируем/шлём тост. Падать нельзя.
fn register_hotkey(app: &AppHandle) {
    let accelerator = app.state::<AppState>().hotkey();
    if let Err(err) = app
        .global_shortcut()
        .on_shortcut(accelerator.as_str(), on_hotkey)
    {
        eprintln!("[vox] хоткей {accelerator:?} не зарегистрирован: {err}");
        let fallback = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyV);
        if let Err(err) = app.global_shortcut().on_shortcut(fallback, on_hotkey) {
            let message = format!("Хоткей Ctrl+Alt+V занят другой программой: {err}");
            eprintln!("[vox] {message}");
            let _ = app.emit("vox-hotkey-error", HotkeyError { message });
        }
    }
}

/// Хоткей: показать окно, сфокусировать и сразу начать слушать (TZ §5.1).
/// HTTP-запрос уводим в поток: обработчик хоткея крутится на главном потоке окна.
fn on_hotkey(app: &AppHandle, _shortcut: &Shortcut, event: ShortcutEvent) {
    if event.state() != ShortcutState::Pressed {
        return;
    }
    window::show_main(app);
    let app = app.clone();
    std::thread::spawn(move || {
        if let Err(err) = sidecar::request(&app, "POST", "/listen", None) {
            eprintln!("[vox] хоткей: POST /listen не удался: {err}");
        }
    });
}

/// Автозапуск включён по умолчанию (TZ §10.3), но ровно один раз — на первом запуске.
/// Маркер в папке данных защищает выбор пользователя: если он выключит автозапуск
/// в настройках (спринт 8), мы не включим его обратно при следующем старте.
/// В dev-сборке реестр не трогаем: иначе автозапуск указывал бы на target/debug.
fn ensure_autostart(app: &AppHandle) {
    if cfg!(debug_assertions) {
        return;
    }
    let marker = app.state::<AppState>().data_dir.join(".autostart-initialized");
    if marker.exists() {
        return;
    }
    use tauri_plugin_autostart::ManagerExt;
    match app.autolaunch().enable() {
        Ok(()) => {
            if let Err(err) = std::fs::write(&marker, b"1\n") {
                eprintln!("[vox] не удалось записать маркер автозапуска: {err}");
            }
        }
        Err(err) => {
            // Маркер не ставим: попробуем в следующий раз.
            eprintln!("[vox] автозапуск не включён: {err}");
        }
    }
}
