//! Состояние оболочки: каталог данных, токен сайдкара, сам процесс и счётчики перезапусков.
//!
//! Состояние живёт в Tauri managed state: `app.state::<AppState>()`. Внутри — обычный
//! `std::sync::Mutex`, поэтому любую блокировку держим как можно короче и никогда не
//! вызываем пользовательский код (HTTP, emit) под замком.

use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::Mutex;
use std::time::Instant;

/// Имя папки данных. Осознанно совпадает с sidecar/voiceruna/paths.py (TZ §13):
/// `%APPDATA%\VoiceRunaCore`, а НЕ app_data_dir() Tauri (тот подставляет identifier).
pub const APP_DIR_NAME: &str = "VoiceRunaCore";

/// Хоткей по умолчанию (TZ §12: `hotkey.activate` = Ctrl+Alt+V).
pub const DEFAULT_HOTKEY: &str = "ctrl+alt+v";

/// Не более `MAX_RESTARTS` автоперезапусков за `RESTART_WINDOW_SECS` секунд (TZ §3).
pub const MAX_RESTARTS: usize = 3;
pub const RESTART_WINDOW_SECS: u64 = 60;

/// Текущее состояние процесса сайдкара.
pub struct SidecarState {
    /// Живой процесс; `None`, когда он ещё не запущен или уже остановлен.
    pub child: Option<Child>,
    /// PID последнего запущенного процесса (нужен для `taskkill /T`).
    pub pid: u32,
    /// Порт, на котором слушает текущий сайдкар; 0 — процесса нет.
    pub port: u16,
    /// `GET /health` уже ответил 200.
    pub healthy: bool,
    /// Процесс запущен, но /health ещё не подтверждён.
    pub starting: bool,
    /// Приложение завершается: автоперезапуск запрещён.
    pub shutting_down: bool,
    /// Микрофон на паузе (состояние из трея; сайдкар — источник истины).
    pub paused: bool,
    /// Всего автоперезапусков за сессию.
    pub restarts: u32,
    /// Моменты автоперезапусков для окна «3 раза за 60 с».
    pub restart_times: Vec<Instant>,
    /// Последняя ошибка для окна (если есть).
    pub error: Option<String>,
}

impl Default for SidecarState {
    fn default() -> Self {
        Self {
            child: None,
            pid: 0,
            port: 0,
            healthy: false,
            starting: false,
            shutting_down: false,
            paused: false,
            restarts: 0,
            restart_times: Vec::new(),
            error: None,
        }
    }
}

/// Всё, что оболочка знает о себе и о сайдкаре.
pub struct AppState {
    pub sidecar: Mutex<SidecarState>,
    /// Токен для заголовка `X-Vox-Token`. Генерируется один раз за запуск приложения
    /// и переживает перезапуски сайдкара — UI не приходится заново его узнавать.
    pub token: String,
    /// `%APPDATA%\VoiceRunaCore` — общая папка с сайдкаром (TZ §13).
    pub data_dir: PathBuf,
    /// Запуск из автозапуска (`--hidden`): окно показываем только при ошибке.
    pub start_hidden: bool,
}

impl AppState {
    /// Создаёт состояние и папки данных. Вызывается до `Builder::build`.
    pub fn new() -> Self {
        let data_dir = data_dir();
        let _ = std::fs::create_dir_all(&data_dir);
        let _ = std::fs::create_dir_all(data_dir.join("logs"));
        Self {
            sidecar: Mutex::new(SidecarState::default()),
            token: crate::sidecar::generate_token(),
            data_dir,
            start_hidden: std::env::args().any(|arg| arg == "--hidden"),
        }
    }

    pub fn logs_dir(&self) -> PathBuf {
        self.data_dir.join("logs")
    }

    /// Хоткей из `config.json` (если сайдкар его уже создал), иначе дефолт TZ.
    pub fn hotkey(&self) -> String {
        read_hotkey(&self.data_dir).unwrap_or_else(|| DEFAULT_HOTKEY.to_string())
    }

    /// Запоминает ошибку для окна. Не трогает здоровье уже упавшего процесса.
    pub fn mark_error(&self, message: impl Into<String>) {
        let mut inner = self.sidecar.lock().unwrap();
        inner.error = Some(message.into());
        inner.healthy = false;
        inner.starting = false;
    }
}

impl Default for AppState {
    fn default() -> Self {
        Self::new()
    }
}

/// `%APPDATA%\VoiceRunaCore`. `VOX_DATA_DIR` переопределяет путь (как в сайдкаре).
pub fn data_dir() -> PathBuf {
    if let Some(override_dir) = std::env::var_os("VOX_DATA_DIR") {
        return PathBuf::from(override_dir);
    }
    let base = std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("USERPROFILE")
                .map(|home| PathBuf::from(home).join("AppData").join("Roaming"))
        })
        .unwrap_or_else(|| PathBuf::from("."));
    base.join(APP_DIR_NAME)
}

/// Читает `hotkey.activate` из config.json. Ошибки не критичны: есть дефолт.
fn read_hotkey(dir: &Path) -> Option<String> {
    let raw = std::fs::read_to_string(dir.join("config.json")).ok()?;
    let value: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let accelerator = value.get("hotkey")?.get("activate")?.as_str()?.trim();
    if accelerator.is_empty() {
        None
    } else {
        Some(accelerator.to_string())
    }
}
