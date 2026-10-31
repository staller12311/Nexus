use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

use crate::logging;

/// 先显示一个无边框 splash，等 backend 健康检查通过后再创建主窗口。
pub fn create(app: &AppHandle) -> tauri::Result<()> {
    if app.get_webview_window("splash").is_some() {
        return Ok(());
    }
    let result = WebviewWindowBuilder::new(app, "splash", WebviewUrl::App("splash.html".into()))
        .title("Nexus 智枢")
        .inner_size(520.0, 320.0)
        .resizable(false)
        .decorations(false)
        .always_on_top(true)
        .center()
        .data_directory(webview_data_dir("splash"))
        .visible(true)
        .build();
    if let Err(error) = &result {
        logging::log(format!("splash window failed: {error:?}"));
    } else {
        logging::log("splash window created");
    }
    result?;
    Ok(())
}

/// 后端就绪：创建主窗口，成功后再关 splash；失败时把错误留在 splash 上。
pub fn show_main(app: &AppHandle, port: u16) {
    let app = app.clone();
    let app_for_thread = app.clone();
    let _ = app.run_on_main_thread(move || {
        if let Some(existing) = app_for_thread.get_webview_window("main") {
            let _ = existing.show();
            let _ = existing.set_focus();
            return;
        }

        let script = format!("window.__NEXUS_API_BASE__ = 'http://127.0.0.1:{}';", port);
        let result = WebviewWindowBuilder::new(
            &app_for_thread,
            "main",
            WebviewUrl::App("index.html".into()),
        )
        .title("Nexus 智枢")
        .inner_size(1440.0, 900.0)
        .min_inner_size(960.0, 640.0)
        .center()
        .data_directory(webview_data_dir("main"))
        .visible(true)
        .initialization_script(script)
        .build();

        match result {
            Ok(window) => {
                logging::log("main window created");
                let _ = window.show();
                let _ = window.set_focus();
                if let Some(splash) = app_for_thread.get_webview_window("splash") {
                    let _ = splash.close();
                }
            }
            Err(error) => {
                logging::log(format!("main window failed: {error:?}"));
                if let Some(splash) = app_for_thread.get_webview_window("splash") {
                    if let Ok(json) = serde_json::to_string(&format!("主窗口创建失败：{error}"))
                    {
                        let _ = splash.eval(format!("window.setSplashError({json});"));
                    }
                }
            }
        }
    });
}

/// 启动失败时把错误写回 splash，而不是留下一个永远转圈的黑屏。
pub fn fail(app: &AppHandle, message: &str) {
    logging::log(format!("startup failure: {message}"));
    let app = app.clone();
    let app_for_thread = app.clone();
    let message = message.to_string();
    let _ = app.run_on_main_thread(move || {
        if let Some(splash) = app_for_thread.get_webview_window("splash") {
            if let Ok(json) = serde_json::to_string(&message) {
                let _ = splash.eval(format!("window.setSplashError({json});"));
            }
        }
    });
}

/// 不依赖 Tauri 默认的 `%LOCALAPPDATA%\<identifier>` 路径：
/// 某些受限环境/宿主策略下该目录创建会返回 os error 5，这里逐级回退到临时目录。
fn webview_data_dir(label: &str) -> std::path::PathBuf {
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    if let Some(base) = std::env::var_os("NEXUS_WEBVIEW_DATA_DIR") {
        candidates.push(std::path::PathBuf::from(base).join(label));
    }
    // dev：优先放仓库内，避免受限终端无法写 LocalAppData；打包后走 LocalAppData。
    if cfg!(debug_assertions) {
        candidates.push(
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .and_then(|frontend| frontend.parent())
                .map(|repo| repo.join(".webview").join(label))
                .unwrap_or_else(|| {
                    std::env::temp_dir()
                        .join("Nexus")
                        .join("webview")
                        .join(label)
                }),
        );
    }
    if let Some(base) = std::env::var_os("LOCALAPPDATA") {
        candidates.push(
            std::path::PathBuf::from(base)
                .join("Nexus")
                .join("webview")
                .join(label),
        );
    }
    candidates.push(
        std::env::temp_dir()
            .join("Nexus")
            .join("webview")
            .join(label),
    );

    for dir in candidates {
        match std::fs::create_dir_all(&dir) {
            Ok(()) => return dir,
            Err(error) => logging::log(format!(
                "webview data dir unavailable {}: {error}",
                dir.display()
            )),
        }
    }
    std::env::temp_dir()
}
