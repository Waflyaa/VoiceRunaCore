//! Tauri-команды для UI (Svelte вызывает их через `invoke` из @tauri-apps/api/core).
//!
//! Контракт для UI-агента описан в packaging/README.md:
//!   get_connection    -> { port, token }
//!   hide_window       -> void
//!   open_config_folder-> void (создаёт %APPDATA%\VoiceRunaCore и открывает проводник)
//!   restart_sidecar   -> void (ждёт /health до 10 с; ошибка = Err(String))
//!   sidecar_status    -> { running, port, restarts, error }

use serde::Serialize;
use tauri::{AppHandle, Manager};

use crate::sidecar;
use crate::state::AppState;

#[derive(Clone, Debug, Serialize)]
pub struct ConnectionInfo {
    pub port: u16,
    pub token: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct SidecarStatus {
    pub running: bool,
    pub port: u16,
    pub restarts: u32,
    pub error: Option<String>,
}

/// Порт и токен сайдкара на текущий момент. Страховка на случай, если `window.__VOX__`
/// устарел (например, сайдкар перезапустился на другом порту — см. событие `vox-connection`).
#[tauri::command]
pub fn get_connection(app: AppHandle) -> ConnectionInfo {
    let state = app.state::<AppState>();
    let port = state.sidecar.lock().unwrap().port;
    ConnectionInfo {
        port,
        token: state.token.clone(),
    }
}

/// Скрыть окно в трей (то же, что крестик; Esc обрабатывает UI).
#[tauri::command]
pub fn hide_window(app: AppHandle) {
    crate::window::hide_main(&app);
}

/// Создать `%APPDATA%\VoiceRunaCore` (если нет) и открыть её в проводнике.
#[tauri::command]
pub fn open_config_folder(app: AppHandle) -> Result<(), String> {
    crate::window::open_config_folder(&app)
}

/// Кнопка «перезапустить сайдкар» на экране ошибки. Блокирующая работа — в отдельном потоке.
#[tauri::command]
pub async fn restart_sidecar(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || sidecar::restart(&app))
        .await
        .map_err(|err| format!("перезапуск сайдкара не выполнен: {err}"))?
}

/// Состояние сайдкара для окна.
#[tauri::command]
pub fn sidecar_status(app: AppHandle) -> SidecarStatus {
    let state = app.state::<AppState>();
    let inner = state.sidecar.lock().unwrap();
    SidecarStatus {
        running: inner.child.is_some() || inner.pid != 0,
        port: inner.port,
        restarts: inner.restarts,
        error: inner.error.clone(),
    }
}
