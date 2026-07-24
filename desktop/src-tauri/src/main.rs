// Prevents an extra console window on Windows in release. Harmless elsewhere.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::menu::{MenuBuilder, SubmenuBuilder};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

/// Persisted connection state, stored as JSON in the app config dir.
///
/// `url` is remembered even after "Switch server…" so the connect screen can
/// prefill it; `connected` gates whether we boot straight into the app.
#[derive(Serialize, Deserialize, Default)]
struct ServerConfig {
    url: Option<String>,
    connected: bool,
}

static WINDOW_SEQ: AtomicU64 = AtomicU64::new(0);

fn next_window_label() -> String {
    let n = WINDOW_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("win-{n}")
}

fn config_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_config_dir().map_err(|e| e.to_string())?;
    Ok(dir.join("server.json"))
}

fn read_config(app: &tauri::AppHandle) -> ServerConfig {
    config_path(app)
        .ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn write_config(app: &tauri::AppHandle, cfg: &ServerConfig) -> Result<(), String> {
    let path = config_path(app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let body = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
    std::fs::write(path, body).map_err(|e| e.to_string())
}

/// Trim, default to https://, and validate that the input is an http(s) URL with
/// a host. Returns the normalized origin (no trailing slash).
fn normalize_url(input: &str) -> Result<String, String> {
    let trimmed = input.trim().trim_end_matches('/');
    if trimmed.is_empty() {
        return Err("Enter your server's address.".into());
    }
    let with_scheme = if trimmed.contains("://") {
        trimmed.to_string()
    } else {
        format!("https://{trimmed}")
    };
    let parsed = tauri::Url::parse(&with_scheme)
        .map_err(|_| "That doesn't look like a valid address.".to_string())?;
    match parsed.scheme() {
        "http" | "https" => {}
        _ => return Err("Use an http:// or https:// address.".into()),
    }
    if parsed.host_str().is_none() {
        return Err("That address is missing a host name.".into());
    }
    Ok(with_scheme)
}

/// Confirm the server is reachable and answers like a Stingray instance. The web
/// frontend proxies `/api` to the backend, whose `/auth/me` returns 200 (logged
/// in) or 401 (not) — either means we found the right server.
async fn check_reachable(base: &str) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let probe = format!("{base}/api/auth/me");
    let resp = client.get(&probe).send().await.map_err(|_| {
        "Couldn't reach that server. Check the address and that it's online.".to_string()
    })?;
    if resp.status().is_success() || resp.status() == reqwest::StatusCode::UNAUTHORIZED {
        Ok(())
    } else {
        Err(format!(
            "Reached the server but it didn't respond like Stingray Tickets (HTTP {}). Use the same address you open in the browser.",
            resp.status().as_u16()
        ))
    }
}

/// Close every window except the one we just created, so the window count never
/// drops to zero (which would exit the app on Linux/Windows).
fn close_other_windows(app: &tauri::AppHandle, keep: &str) {
    for (label, window) in app.webview_windows() {
        if label != keep {
            let _ = window.close();
        }
    }
}

fn open_connect_window(app: &tauri::AppHandle) -> Result<(), String> {
    let label = next_window_label();
    WebviewWindowBuilder::new(app, label.as_str(), WebviewUrl::App("index.html".into()))
        .title("Stingray Tickets")
        .inner_size(480.0, 560.0)
        .min_inner_size(380.0, 460.0)
        .build()
        .map_err(|e| e.to_string())?;
    close_other_windows(app, &label);
    Ok(())
}

fn open_app_window(app: &tauri::AppHandle, base: &str) -> Result<(), String> {
    let parsed = tauri::Url::parse(base).map_err(|e| e.to_string())?;
    let host = parsed.host_str().unwrap_or_default().to_string();
    let handle = app.clone();
    let label = next_window_label();

    WebviewWindowBuilder::new(app, label.as_str(), WebviewUrl::External(parsed))
        .title("Stingray Tickets")
        .inner_size(1100.0, 760.0)
        .min_inner_size(600.0, 400.0)
        .on_navigation(move |url| {
            // Keep top-level navigation on the server's origin; hand any external
            // link (http/https to another host) to the user's real browser.
            let same_origin = url.host_str() == Some(host.as_str());
            let internal = !matches!(url.scheme(), "http" | "https");
            if same_origin || internal {
                true
            } else {
                let _ = handle.opener().open_url(url.to_string(), None::<&str>);
                false
            }
        })
        .build()
        .map_err(|e| e.to_string())?;
    close_other_windows(app, &label);
    Ok(())
}

#[tauri::command]
fn last_server_url(app: tauri::AppHandle) -> Option<String> {
    read_config(&app).url
}

#[tauri::command]
async fn connect_to_server(app: tauri::AppHandle, url: String) -> Result<(), String> {
    let normalized = normalize_url(&url)?;
    check_reachable(&normalized).await?;
    write_config(
        &app,
        &ServerConfig {
            url: Some(normalized.clone()),
            connected: true,
        },
    )?;
    open_app_window(&app, &normalized)
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![connect_to_server, last_server_url])
        .menu(|handle| {
            let server = SubmenuBuilder::new(handle, "Server")
                .text("switch", "Switch server…")
                .separator()
                .quit()
                .build()?;
            MenuBuilder::new(handle).item(&server).build()
        })
        .on_menu_event(|app, event| {
            if event.id().0.as_str() == "switch" {
                let mut cfg = read_config(app);
                cfg.connected = false; // keep the URL for prefill, drop auto-connect
                let _ = write_config(app, &cfg);
                let _ = open_connect_window(app);
            }
        })
        .setup(|app| {
            let handle = app.handle();
            let cfg = read_config(handle);
            match (cfg.connected, cfg.url) {
                (true, Some(url)) => open_app_window(handle, &url),
                _ => open_connect_window(handle),
            }
            .map_err(|e| -> Box<dyn std::error::Error> { e.into() })?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Stingray Tickets");
}
