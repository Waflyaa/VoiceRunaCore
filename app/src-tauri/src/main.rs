// Скрывает лишнее консольное окно в release-сборке Windows: приложение живёт в трее.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    voiceruna_lib::run();
}
