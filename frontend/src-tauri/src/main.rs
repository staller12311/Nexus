// 生产构建用 Windows GUI 子系统，避免额外控制台；dev 保留控制台方便看日志。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;
mod logging;
mod splash;
mod state;

use tauri::Manager;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(
            |app, _args: Vec<String>, _cwd: String| {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            },
        ))
        .manage(state::BackendState::default())
        .setup(|app| {
            splash::create(app.handle())?;
            let handle = app.handle().clone();
            std::thread::spawn(move || backend::start(handle));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Nexus desktop")
        .run(|app, event| {
            if matches!(
                event,
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
            ) {
                backend::stop(app);
            }
        });
}
