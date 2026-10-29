//! Иконка в трее и меню (TZ §10.2): окно, пауза микрофона, настройки, папка конфига, выход.
//!
//! «Выход» — единственный штатный способ закрыть приложение (крестик только скрывает окно).

use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, Wry};

use crate::sidecar;
use crate::state::AppState;
use crate::window;

pub const TRAY_ID: &str = "main";

const ICON_NORMAL: &[u8] = include_bytes!("../icons/32x32.png");
const ICON_MUTED: &[u8] = include_bytes!("../icons/tray-muted.png");

/// Пункт «Пауза микрофона» храним в managed state, чтобы переключать галочку из любого места.
pub struct TrayState {
    pause_item: CheckMenuItem<Wry>,
}

#[derive(Clone, Debug, serde::Serialize)]
struct PausedEvent {
    paused: bool,
}

/// Создаёт иконку трея с меню. Ошибка не фатальна: приложение продолжит работать без трея.
pub fn create(app: &AppHandle) -> tauri::Result<()> {
    let open_item = MenuItem::with_id(app, "open", "Открыть окно", true, None::<&str>)?;
    let pause_item =
        CheckMenuItem::with_id(app, "pause", "Пауза микрофона", true, false, None::<&str>)?;
    let settings_item = MenuItem::with_id(app, "settings", "Настройки", true, None::<&str>)?;
    let folder_item =
        MenuItem::with_id(app, "config-folder", "Открыть папку конфига", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "Выход", true, None::<&str>)?;
    let separator_one = PredefinedMenuItem::separator(app)?;
    let separator_two = PredefinedMenuItem::separator(app)?;

    let menu = Menu::with_items(
        app,
        &[
            &open_item,
            &separator_one,
            &pause_item,
            &settings_item,
            &folder_item,
            &separator_two,
            &quit_item,
        ],
    )?;

    TrayIconBuilder::with_id(TRAY_ID)
        .icon(Image::from_bytes(ICON_NORMAL)?)
        .tooltip("Ассистент")
        .menu(&menu)
        // Правый клик — меню, левый — окно.
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| handle_menu(app, event.id().as_ref()))
        .on_tray_icon_event(|tray, event| match event {
            TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } => window::show_main(tray.app_handle()),
            TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            } => window::show_main(tray.app_handle()),
            _ => {}
        })
        .build(app)?;

    app.manage(TrayState { pause_item });
    Ok(())
}

fn handle_menu(app: &AppHandle, id: &str) {
    match id {
        "open" => window::show_main(app),
        "pause" => toggle_pause(app),
        "settings" => {
            // UI ловит событие и открывает вкладку настроек (§10.1, §11).
            window::show_main(app);
            let _ = app.emit("open-settings", ());
        }
        "config-folder" => {
            if let Err(err) = window::open_config_folder(app) {
                eprintln!("[vox] {err}");
                sidecar::emit_error(app, &err);
            }
        }
        "quit" => quit(app),
        _ => {}
    }
}

/// Toggle «Пауза микрофона»: состояние живёт в сайдкаре (POST /pause | POST /resume).
fn toggle_pause(app: &AppHandle) {
    let next = {
        let state = app.state::<AppState>();
        let inner = state.sidecar.lock().unwrap();
        !inner.paused
    };
    match sidecar::set_paused(app, next) {
        Ok(()) => {
            paint_paused(app, next);
            sidecar::emit_status(app);
        }
        Err(err) => {
            eprintln!("[vox] не удалось переключить паузу: {err}");
            sidecar::emit_error(app, &format!("Не удалось переключить микрофон: {err}"));
        }
    }
}

/// Галочка в меню, «приглушённая» иконка трея и событие в UI.
fn paint_paused(app: &AppHandle, paused: bool) {
    if let Some(tray_state) = app.try_state::<TrayState>() {
        let _ = tray_state.pause_item.set_checked(paused);
    }
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        let bytes = if paused { ICON_MUTED } else { ICON_NORMAL };
        if let Ok(icon) = Image::from_bytes(bytes) {
            let _ = tray.set_icon(Some(icon));
        }
        let tooltip = if paused {
            "Ассистент — микрофон на паузе"
        } else {
            "Ассистент"
        };
        let _ = tray.set_tooltip(Some(tooltip));
    }
    let _ = app.emit("vox-paused", PausedEvent { paused });
}

/// «Выход»: корректно гасим сайдкар и завершаем приложение.
fn quit(app: &AppHandle) {
    sidecar::shutdown(app);
    app.exit(0);
}
