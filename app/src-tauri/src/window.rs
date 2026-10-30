//! Главное окно «Ассистент»: создание с инъекцией `window.__VOX__`, показ/скрытие,
//! открытие папки конфига.
//!
//! Окно описано в tauri.conf.json с `"create": false`, поэтому Tauri его не создаёт сам.
//! Мы создаём его в setup через `WebviewWindowBuilder::from_config` — только так можно
//! добавить initialization_script, который выполняется ДО скриптов страницы. К этому
//! моменту порт и токен сайдкара уже известны, и UI не нужно ждать `get_connection`.

use std::process::Command;

use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

use crate::state::AppState;

pub const MAIN_WINDOW: &str = "main";

/// Создаёт главное окно и прокидывает в него `window.__VOX__ = { port, token, app }`.
pub fn create(app: &AppHandle) -> tauri::Result<WebviewWindow> {
    let (port, token) = {
        let state = app.state::<AppState>();
        let port = state.sidecar.lock().unwrap().port;
        (port, state.token.clone())
    };
    let token_json = serde_json::to_string(&token).unwrap_or_else(|_| String::from("\"\""));
    let script = format!(
        "window.__VOX__ = Object.freeze({{ port: {port}, token: {token_json}, app: \"VoiceRunaCore\" }});"
    );

    let config = app
        .config()
        .app
        .windows
        .iter()
        .find(|window| window.label == MAIN_WINDOW);

    let builder = match config {
        // .center() дублирует config.center: страховка на случай, если рантайм
        // проигнорирует центрирование при программном создании окна.
        Some(config) => WebviewWindowBuilder::from_config(app, config)?.center(),
        None => WebviewWindowBuilder::new(app, MAIN_WINDOW, WebviewUrl::App("index.html".into()))
            .title("Ассистент")
            .inner_size(460.0, 640.0)
            .min_inner_size(380.0, 480.0)
            .resizable(true)
            .center()
            .decorations(true)
            .always_on_top(false)
            .visible(false),
    };

    builder.initialization_script(script).build()
}

/// Показать окно поверх остальных (но НЕ always-on-top) и отдать ему фокус — TZ §10.1.
pub fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Toggle for the global hotkey (v2): opened -> hide to tray, hidden -> show.
///
/// Returns true when the window is visible after the call, so the caller only
/// arms listening when the window actually appeared.
pub fn toggle_main(app: &AppHandle) -> bool {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let visible = window.is_visible().unwrap_or(false);
        let minimized = window.is_minimized().unwrap_or(false);
        if visible && !minimized {
            let _ = window.hide();
            return false;
        }
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        return true;
    }
    false
}

/// Скрыть окно в трей (крестик, Esc).
pub fn hide_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.hide();
    }
}

/// Показать окно, потому что сайдкар не поднялся: UI покажет ошибку и кнопку «перезапустить».
pub fn show_error(app: &AppHandle) {
    show_main(app);
}

/// Открыть `%APPDATA%\VoiceRunaCore` в проводнике, создав папку при необходимости.
pub fn open_config_folder(app: &AppHandle) -> Result<(), String> {
    let dir = app.state::<AppState>().data_dir.clone();
    std::fs::create_dir_all(&dir)
        .map_err(|err| format!("не удалось создать {}: {err}", dir.display()))?;
    let path = dir.to_string_lossy().to_string();

    if cfg!(windows) {
        use tauri_plugin_opener::OpenerExt;
        // Rust-API плагина не проверяет scope (в отличие от JS-команды).
        if app.opener().open_path(path.clone(), None::<&str>).is_ok() {
            return Ok(());
        }
        Command::new("explorer.exe")
            .arg(&path)
            .spawn()
            .map_err(|err| format!("не удалось открыть {}: {err}", dir.display()))?;
    } else {
        Command::new("xdg-open")
            .arg(&path)
            .spawn()
            .map_err(|err| format!("не удалось открыть {}: {err}", dir.display()))?;
    }
    Ok(())
}
