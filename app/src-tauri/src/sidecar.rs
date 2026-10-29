//! Жизненный цикл Python-сайдкара (TZ §3): порт, токен, запуск, /health, автоперезапуск, убийство дерева.
//!
//! Ловушка поставки: PyInstaller `onedir` — это exe + папка `_internal`, поэтому
//! `externalBin` не используется. Папка целиком лежит в ресурсах как `sidecar/`
//! (см. `bundle.resources` в tauri.conf.json), а exe запускается по пути
//! `resource_dir()/sidecar/voiceruna-sidecar.exe` (на Windows resource_dir — папка exe).
//!
//! Транспорт до сайдкара — обычный HTTP по 127.0.0.1. Отдельный HTTP-клиент здесь
//! написан на std::net намеренно: нужны только GET /health и короткие POST, а зависимость
//! (reqwest/ureq) тянет TLS, которого для localhost не требуется.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};

use crate::state::{AppState, MAX_RESTARTS, RESTART_WINDOW_SECS};
use crate::window;

/// Имя exe в ресурсах установщика.
#[cfg(windows)]
const SIDECAR_EXE: &str = "voiceruna-sidecar.exe";
#[cfg(not(windows))]
const SIDECAR_EXE: &str = "voiceruna-sidecar";

/// Сколько ждём `GET /health` (TZ §3: до 10 с), шаг опроса.
const HEALTH_TIMEOUT: Duration = Duration::from_secs(10);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(150);

/// Полезная нагрузка события `vox-connection`.
#[derive(Clone, Debug, Serialize)]
pub struct Connection {
    pub port: u16,
    pub token: String,
}

/// Полезная нагрузка события `vox-sidecar` (снимок статуса для окна).
#[derive(Clone, Debug, Serialize)]
pub struct StatusEvent {
    pub running: bool,
    pub healthy: bool,
    pub port: u16,
    pub restarts: u32,
    pub paused: bool,
    pub error: Option<String>,
}

/// Полезная нагрузка события `vox-error`.
#[derive(Clone, Debug, Serialize)]
pub struct ErrorEvent {
    pub message: String,
}

/// 32 случайных байта в hex — токен `X-Vox-Token`.
pub fn generate_token() -> String {
    use rand::RngCore;
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    let mut token = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        token.push_str(&format!("{byte:02x}"));
    }
    token
}

/// Свободный порт на 127.0.0.1: слушаем :0, забираем порт, закрываем сокет.
fn free_port() -> std::io::Result<u16> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(port)
}

fn log(message: &str) {
    eprintln!("[vox] {message}");
}

// --------------------------------------------------------------------------- //
// мини-HTTP-клиент
// --------------------------------------------------------------------------- //

/// Один запрос без keep-alive: `Connection: close` + чтение ответа до EOF.
/// Возвращает код статуса и тело (тело нужно только для диагностики).
fn http(
    port: u16,
    token: &str,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> Result<(u16, String), String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(700))
        .map_err(|err| format!("connect 127.0.0.1:{port}: {err}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));

    let payload = body.unwrap_or("");
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Vox-Token: {token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
        payload.as_bytes().len()
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|err| format!("write {method} {path}: {err}"))?;
    let _ = stream.flush();

    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    loop {
        match stream.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => raw.extend_from_slice(&buffer[..read]),
            Err(err) => match err.kind() {
                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut => break,
                _ => return Err(format!("read {method} {path}: {err}")),
            },
        }
    }
    parse_response(&raw)
}

fn parse_response(raw: &[u8]) -> Result<(u16, String), String> {
    let text = String::from_utf8_lossy(raw);
    let (head, body) = match text.split_once("\r\n\r\n") {
        Some(parts) => parts,
        None => return Err("пустой ответ HTTP (нет заголовков)".to_string()),
    };
    let status_line = head
        .lines()
        .next()
        .ok_or_else(|| "в ответе нет строки статуса".to_string())?;
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| format!("непонятный статус HTTP: {status_line}"))?;
    Ok((status, body.to_string()))
}

/// `GET /health`. Токен на этом маршруте сайдкару не нужен, но лишний заголовок не мешает.
fn health_ok(port: u16, token: &str) -> bool {
    matches!(http(port, token, "GET", "/health", None), Ok((200, _)))
}

// --------------------------------------------------------------------------- //
// поиск и запуск процесса
// --------------------------------------------------------------------------- //

/// Каталог исходников sidecar/ в dev-режиме (cargo run / tauri dev).
fn dev_sidecar_dir() -> Result<PathBuf, String> {
    if let Some(dir) = std::env::var_os("VOX_SIDECAR_DIR") {
        return Ok(PathBuf::from(dir));
    }
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    // app/src-tauri -> app -> <repo root>
    if let Some(repo) = manifest.ancestors().nth(2) {
        let candidate = repo.join("sidecar");
        if candidate.join("voiceruna").join("__main__.py").exists() {
            return Ok(candidate);
        }
    }
    let cwd_candidate = std::env::current_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("sidecar");
    if cwd_candidate.join("voiceruna").join("__main__.py").exists() {
        return Ok(cwd_candidate);
    }
    Err(format!(
        "dev-режим: не найден sidecar/voiceruna/__main__.py (ожидался {}). Задайте VOX_SIDECAR_DIR.",
        manifest.display()
    ))
}

/// Python для dev-режима: VOX_PYTHON, затем venv репозитория, затем python из PATH.
fn dev_python(sidecar_dir: &Path) -> PathBuf {
    if let Some(python) = std::env::var_os("VOX_PYTHON") {
        return PathBuf::from(python);
    }
    let repo = sidecar_dir.parent().unwrap_or(sidecar_dir);
    for candidate in [
        repo.join(".venv").join("Scripts").join("python.exe"),
        repo.join("packaging").join(".venv").join("Scripts").join("python.exe"),
        // окружение, из которого гоняются смоук-тесты сайдкара (см. TESTS/vox-sidecar-smoke.py)
        repo.join("packaging").join(".venv-build").join("Scripts").join("python.exe"),
    ] {
        if candidate.exists() {
            return candidate;
        }
    }
    PathBuf::from("python")
}

/// Путь к собранному onedir-сайдкару внутри установленных ресурсов.
fn bundled_sidecar_exe(app: &AppHandle) -> Result<PathBuf, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|err| format!("не удалось получить resource_dir(): {err}"))?;
    let exe = resource_dir.join("sidecar").join(SIDECAR_EXE);
    if exe.exists() {
        return Ok(exe);
    }
    // Запасной вариант: рядом с самим exe (портативная распаковка, ручной запуск).
    if let Ok(current) = std::env::current_exe() {
        if let Some(dir) = current.parent() {
            let fallback = dir.join("sidecar").join(SIDECAR_EXE);
            if fallback.exists() {
                return Ok(fallback);
            }
        }
    }
    Err(format!(
        "не найден сайдкар: {}. Соберите его (packaging/build-sidecar.ps1) и пересоберите установщик.",
        exe.display()
    ))
}

/// Куда писать stdout/stderr сайдкара: %APPDATA%\VoiceRunaCore\logs.
fn attach_logs(command: &mut Command, logs: &Path) {
    if std::fs::create_dir_all(logs).is_err() {
        command.stdout(Stdio::null()).stderr(Stdio::null());
        return;
    }
    let open = |name: &str| -> Option<std::fs::File> {
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(logs.join(name))
            .ok()
    };
    match (open("sidecar.out.log"), open("sidecar.err.log")) {
        (Some(out), Some(err)) => {
            command.stdout(Stdio::from(out));
            command.stderr(Stdio::from(err));
        }
        _ => {
            command.stdout(Stdio::null());
            command.stderr(Stdio::null());
        }
    }
}

/// Убирает консольное окно у дочерних процессов (python.exe, taskkill.exe).
fn hide_console(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    {
        let _ = command;
    }
}

/// Запускает процесс сайдкара. Не ждёт /health — это делает spawn_health_watch.
pub fn start(app: &AppHandle) -> Result<(), String> {
    let state = app.state::<AppState>();
    {
        let inner = state.sidecar.lock().unwrap();
        if inner.child.is_some() {
            return Ok(());
        }
    }

    let port = free_port().map_err(|err| format!("не удалось подобрать свободный порт: {err}"))?;

    let (program, args, workdir) = if cfg!(debug_assertions) {
        let dir = dev_sidecar_dir()?;
        let python = dev_python(&dir);
        let args = vec![
            "-m".to_string(),
            "voiceruna".to_string(),
            "--port".to_string(),
            port.to_string(),
        ];
        (python, args, dir)
    } else {
        let exe = bundled_sidecar_exe(app)?;
        let workdir = exe
            .parent()
            .map(Path::to_path_buf)
            .ok_or_else(|| format!("у {} нет родительской папки", exe.display()))?;
        (exe, vec!["--port".to_string(), port.to_string()], workdir)
    };

    let mut command = Command::new(&program);
    command
        .args(&args)
        .current_dir(&workdir)
        .env("VOX_TOKEN", &state.token)
        .stdin(Stdio::null());
    let logs = state.logs_dir();
    attach_logs(&mut command, &logs);
    hide_console(&mut command);

    let child = command.spawn().map_err(|err| {
        format!(
            "не удалось запустить сайдкар {} (cwd {}): {err}",
            program.display(),
            workdir.display()
        )
    })?;
    let pid = child.id();

    {
        let mut inner = state.sidecar.lock().unwrap();
        inner.child = Some(child);
        inner.pid = pid;
        inner.port = port;
        inner.healthy = false;
        inner.starting = true;
        inner.error = None;
    }
    log(&format!(
        "сайдкар запущен: pid={pid}, порт={port}, cwd={}",
        workdir.display()
    ));
    emit_status(app);
    Ok(())
}

// --------------------------------------------------------------------------- //
// /health, мониторинг, перезапуск, остановка
// --------------------------------------------------------------------------- //

/// Ждёт 200 на `GET /health`. Возвращает true, если сайдкар поднялся за timeout.
pub fn wait_healthy(app: &AppHandle, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        let (port, token) = {
            let state = app.state::<AppState>();
            let inner = state.sidecar.lock().unwrap();
            (inner.port, state.token.clone())
        };
        if port != 0 && health_ok(port, &token) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(HEALTH_POLL_INTERVAL);
    }
}

/// Фоновое ожидание /health после запуска: обновляет состояние и уведомляет окно.
pub fn spawn_health_watch(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let ready = wait_healthy(&app, HEALTH_TIMEOUT);
        if ready {
            {
                let state = app.state::<AppState>();
                let mut inner = state.sidecar.lock().unwrap();
                inner.starting = false;
                inner.healthy = true;
                inner.error = None;
            }
            log("сайдкар готов: GET /health -> 200");
            emit_connection(&app);
            emit_status(&app);
        } else {
            let stopping = {
                let state = app.state::<AppState>();
                let inner = state.sidecar.lock().unwrap();
                inner.shutting_down
            };
            if stopping {
                return;
            }
            let message = format!(
                "Сайдкар не ответил на GET /health за {} с",
                HEALTH_TIMEOUT.as_secs()
            );
            log(&message);
            kill_child(&app);
            app.state::<AppState>().mark_error(message.clone());
            emit_error(&app, &message);
            emit_status(&app);
            window::show_error(&app);
        }
    });
}

/// Супервизор: раз в секунду проверяет, жив ли процесс, и поднимает его при падении.
pub fn spawn_monitor(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(1000));
        let exit_status = {
            let state = app.state::<AppState>();
            let mut inner = state.sidecar.lock().unwrap();
            if inner.shutting_down {
                return;
            }
            let mut status = None;
            if let Some(child) = inner.child.as_mut() {
                match child.try_wait() {
                    Ok(Some(code)) => status = Some(code),
                    Ok(None) => {}
                    Err(err) => log(&format!("try_wait(): {err}")),
                }
            }
            if status.is_some() {
                inner.child = None;
                inner.pid = 0;
                inner.healthy = false;
            }
            status
        };
        if let Some(status) = exit_status {
            handle_unexpected_exit(&app, status);
        }
    });
}

fn handle_unexpected_exit(app: &AppHandle, status: ExitStatus) {
    log(&format!("сайдкар завершился неожиданно: {status}"));
    let state = app.state::<AppState>();
    let allowed = {
        let mut inner = state.sidecar.lock().unwrap();
        if inner.shutting_down {
            return;
        }
        let now = Instant::now();
        let restart_window = Duration::from_secs(RESTART_WINDOW_SECS);
        inner
            .restart_times
            .retain(|time| now.saturating_duration_since(*time) < restart_window);
        if inner.restart_times.len() >= MAX_RESTARTS {
            inner.error = Some(format!(
                "Сайдкар падал {MAX_RESTARTS} раза за {RESTART_WINDOW_SECS} с — автоперезапуск остановлен"
            ));
            false
        } else {
            inner.restart_times.push(now);
            inner.restarts += 1;
            inner.error = Some(format!("Сайдкар завершился ({status}), перезапускаю…"));
            true
        }
    };

    if !allowed {
        let message = state
            .sidecar
            .lock()
            .unwrap()
            .error
            .clone()
            .unwrap_or_else(|| "сайдкар недоступен".to_string());
        emit_error(app, &message);
        emit_status(app);
        window::show_error(app);
        return;
    }

    emit_status(app);
    std::thread::sleep(Duration::from_millis(400));
    match start(app) {
        Ok(()) => spawn_health_watch(app),
        Err(err) => {
            log(&format!("автоперезапуск не удался: {err}"));
            state.mark_error(err.clone());
            emit_error(app, &err);
            emit_status(app);
            window::show_error(app);
        }
    }
}

/// Убивает процесс вместе с деревом. На Windows — `taskkill /PID <pid> /T /F`.
fn kill_child(app: &AppHandle) {
    let state = app.state::<AppState>();
    let (pid, child) = {
        let mut inner = state.sidecar.lock().unwrap();
        (inner.pid, inner.child.take())
    };

    if pid != 0 {
        #[cfg(windows)]
        {
            let pid_arg = pid.to_string();
            let mut command = Command::new("taskkill");
            command
                .arg("/PID")
                .arg(&pid_arg)
                .arg("/T")
                .arg("/F")
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            hide_console(&mut command);
            let _ = command.status();
        }
    }
    if let Some(mut child) = child {
        let _ = child.kill();
        let _ = child.wait();
    }

    let mut inner = state.sidecar.lock().unwrap();
    inner.pid = 0;
    inner.port = 0;
    inner.healthy = false;
    inner.starting = false;
}

/// Останавливает сайдкар без флага «приложение завершается» (для restart_sidecar).
fn stop_process(app: &AppHandle) {
    let state = app.state::<AppState>();
    let (port, token) = {
        let inner = state.sidecar.lock().unwrap();
        (inner.port, state.token.clone())
    };
    if port != 0 {
        let _ = http(port, &token, "POST", "/shutdown", None);
        std::thread::sleep(Duration::from_millis(200));
    }
    kill_child(app);
}

/// Ручной перезапуск из окна (кнопка «перезапустить сайдкар»).
pub fn restart(app: &AppHandle) -> Result<(), String> {
    log("перезапуск сайдкара по запросу окна");
    stop_process(app);
    start(app)?;
    if wait_healthy(app, HEALTH_TIMEOUT) {
        {
            let state = app.state::<AppState>();
            let mut inner = state.sidecar.lock().unwrap();
            inner.starting = false;
            inner.healthy = true;
            inner.error = None;
        }
        emit_connection(app);
        emit_status(app);
        Ok(())
    } else {
        let message = "Сайдкар не ответил на GET /health после перезапуска".to_string();
        kill_child(app);
        app.state::<AppState>().mark_error(message.clone());
        emit_error(app, &message);
        emit_status(app);
        Err(message)
    }
}

/// Штатное завершение: попросить /shutdown, затем гарантированно убить дерево.
pub fn shutdown(app: &AppHandle) {
    {
        let state = app.state::<AppState>();
        let mut inner = state.sidecar.lock().unwrap();
        if inner.shutting_down {
            return;
        }
        inner.shutting_down = true;
    }
    stop_process(app);
    log("сайдкар остановлен");
}

// --------------------------------------------------------------------------- //
// внешние команды и события
// --------------------------------------------------------------------------- //

/// Запрос к сайдкару с текущими портом и токеном.
pub fn request(
    app: &AppHandle,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> Result<(u16, String), String> {
    let state = app.state::<AppState>();
    let port = { state.sidecar.lock().unwrap().port };
    if port == 0 {
        return Err("сайдкар не запущен".to_string());
    }
    http(port, &state.token, method, path, body)
}

/// Пауза/возобновление микрофона (POST /pause, POST /resume). Состояние хранит сайдкар.
pub fn set_paused(app: &AppHandle, paused: bool) -> Result<(), String> {
    let path = if paused { "/pause" } else { "/resume" };
    let (status, body) = request(app, "POST", path, None)?;
    if status != 200 {
        return Err(format!("сайдкар ответил {status} на {path}: {body}"));
    }
    {
        let state = app.state::<AppState>();
        state.sidecar.lock().unwrap().paused = paused;
    }
    Ok(())
}

/// Порт и токен для окна. Событие `vox-connection`.
pub fn emit_connection(app: &AppHandle) {
    let state = app.state::<AppState>();
    let port = state.sidecar.lock().unwrap().port;
    let _ = app.emit(
        "vox-connection",
        Connection {
            port,
            token: state.token.clone(),
        },
    );
}

/// Снимок статуса для окна. Событие `vox-sidecar`.
pub fn emit_status(app: &AppHandle) {
    let state = app.state::<AppState>();
    let (running, healthy, port, restarts, paused, error) = {
        let inner = state.sidecar.lock().unwrap();
        (
            inner.child.is_some() || inner.pid != 0,
            inner.healthy,
            inner.port,
            inner.restarts,
            inner.paused,
            inner.error.clone(),
        )
    };
    let _ = app.emit(
        "vox-sidecar",
        StatusEvent {
            running,
            healthy,
            port,
            restarts,
            paused,
            error,
        },
    );
}

/// Ошибка для тоста в окне. Событие `vox-error`.
pub fn emit_error(app: &AppHandle, message: &str) {
    let _ = app.emit(
        "vox-error",
        ErrorEvent {
            message: message.to_string(),
        },
    );
}
