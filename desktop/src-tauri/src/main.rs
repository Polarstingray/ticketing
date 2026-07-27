// Prevents an extra console window on Windows in release. Harmless elsewhere.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::menu::{CheckMenuItemBuilder, MenuBuilder, MenuItemBuilder, SubmenuBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_opener::OpenerExt;

/// One saved server the user can switch between.
#[derive(Serialize, Deserialize, Clone, Default)]
struct ServerProfile {
    /// Stable id (the normalized URL doubles as the id — it's already unique).
    id: String,
    /// Human label shown in menus; defaults to the host.
    label: String,
    url: String,
}

/// Last-known window geometry, restored on the next launch so the app reopens
/// where the user left it instead of the hardcoded default size.
#[derive(Serialize, Deserialize, Clone)]
struct WindowState {
    w: f64,
    h: f64,
    x: Option<f64>,
    y: Option<f64>,
}

impl Default for WindowState {
    fn default() -> Self {
        WindowState { w: 1100.0, h: 760.0, x: None, y: None }
    }
}

/// Persisted native preferences, stored as JSON in the app config dir.
///
/// Extends the original single-URL `server.json`. Old files (with just
/// `url`/`connected`) still deserialize thanks to `#[serde(default)]`, and are
/// migrated into `servers`/`active` on first load (see `migrate_legacy`).
#[derive(Serialize, Deserialize, Default)]
struct DesktopConfig {
    #[serde(default)]
    servers: Vec<ServerProfile>,
    /// The active profile's id (== its url). None = show the connect screen.
    #[serde(default)]
    active: Option<String>,
    #[serde(default)]
    launch_on_login: bool,
    #[serde(default)]
    start_minimized: bool,
    #[serde(default)]
    window: WindowState,

    // --- legacy fields, kept one release for migration ---
    #[serde(default)]
    url: Option<String>,
    #[serde(default)]
    connected: bool,
}

impl DesktopConfig {
    /// Fold a pre-profiles `{url, connected}` file into the profile list so an
    /// upgrading user keeps their server and auto-connect state.
    fn migrate_legacy(&mut self) {
        if let Some(raw) = self.url.take() {
            // Normalize first: every other profile id is a normalized URL, and the
            // id doubles as the identity. A legacy file holding "http://box:3000/"
            // (or a scheme-less "box:3000") would otherwise migrate to an id that
            // never matches the normalized form the connect flow produces — the
            // next connect would add a *second* profile for the same server and
            // the menu checkmark would stop tracking `active`. Fall back to the
            // raw string if it can't be parsed, so a broken value still migrates.
            let url = normalize_url(&raw).unwrap_or(raw);
            if !self.servers.iter().any(|p| p.id == url) {
                self.servers.push(ServerProfile {
                    label: host_label(&url),
                    id: url.clone(),
                    url: url.clone(),
                });
            }
            if self.connected && self.active.is_none() {
                self.active = Some(url);
            }
        }
        self.connected = false; // field retired; active drives auto-connect now
    }

    fn active_url(&self) -> Option<String> {
        self.active.clone()
    }

    /// Insert (or move-to-front) a profile for `url` and mark it active.
    fn upsert_active(&mut self, url: &str) {
        self.servers.retain(|p| p.id != url);
        self.servers.insert(
            0,
            ServerProfile { id: url.to_string(), label: host_label(url), url: url.to_string() },
        );
        self.active = Some(url.to_string());
    }
}

static WINDOW_SEQ: AtomicU64 = AtomicU64::new(0);

fn next_window_label() -> String {
    let n = WINDOW_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("win-{n}")
}

/// A short label for a server URL — its host, or the raw string if unparseable.
fn host_label(url: &str) -> String {
    tauri::Url::parse(url)
        .ok()
        .and_then(|u| u.host_str().map(|h| h.to_string()))
        .unwrap_or_else(|| url.to_string())
}

fn config_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_config_dir().map_err(|e| e.to_string())?;
    Ok(dir.join("server.json"))
}

fn read_config(app: &tauri::AppHandle) -> DesktopConfig {
    let mut cfg: DesktopConfig = config_path(app)
        .ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();
    cfg.migrate_legacy();
    cfg
}

fn write_config(app: &tauri::AppHandle, cfg: &DesktopConfig) -> Result<(), String> {
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

    let cfg = read_config(app);
    let ws = cfg.window;
    let mut builder =
        WebviewWindowBuilder::new(app, label.as_str(), WebviewUrl::External(parsed))
            .title("Stingray Tickets")
            .inner_size(ws.w, ws.h)
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
            });
    if let (Some(x), Some(y)) = (ws.x, ws.y) {
        builder = builder.position(x, y);
    }
    if cfg.start_minimized {
        builder = builder.visible(false);
    }
    let window = builder.build().map_err(|e| e.to_string())?;
    attach_window_state_saver(&window);
    close_other_windows(app, &label);
    Ok(())
}

/// Persist window geometry as the user resizes/moves it, so the next launch
/// restores it. Writes are cheap (a small JSON file) and only touch the
/// `window` field, leaving profiles/prefs untouched.
fn attach_window_state_saver(window: &WebviewWindow) {
    let app = window.app_handle().clone();
    let win = window.clone();
    window.on_window_event(move |event| {
        if matches!(event, WindowEvent::Resized(_) | WindowEvent::Moved(_)) {
            let size = win.outer_size().ok();
            let pos = win.outer_position().ok();
            let scale = win.scale_factor().unwrap_or(1.0);
            let mut cfg = read_config(&app);
            if let Some(s) = size {
                let logical = s.to_logical::<f64>(scale);
                // Ignore the (0,0) that some platforms report while minimizing.
                if logical.width > 200.0 && logical.height > 200.0 {
                    cfg.window.w = logical.width;
                    cfg.window.h = logical.height;
                }
            }
            if let Some(p) = pos {
                let logical = p.to_logical::<f64>(scale);
                cfg.window.x = Some(logical.x);
                cfg.window.y = Some(logical.y);
            }
            let _ = write_config(&app, &cfg);
        }
    });
}

/// Build the app menu from the current config: a Server submenu that lists saved
/// profiles (checkmark on the active one) plus add/switch, a Preferences submenu
/// with native toggles, and a top-level "Resolver settings…" that deep-links the
/// webview to the SPA admin page.
fn build_menu(app: &tauri::AppHandle) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    let cfg = read_config(app);

    let mut server = SubmenuBuilder::new(app, "Server");
    for profile in &cfg.servers {
        let checked = cfg.active.as_deref() == Some(profile.id.as_str());
        let item = CheckMenuItemBuilder::new(&profile.label)
            .id(format!("profile:{}", profile.id))
            .checked(checked)
            .build(app)?;
        server = server.item(&item);
    }
    let server = server
        .separator()
        .text("add", "Add server…")
        .text("switch", "Switch server…")
        .separator()
        .quit()
        .build()?;

    let launch = CheckMenuItemBuilder::new("Launch at login")
        .id("toggle:launch_on_login")
        .checked(cfg.launch_on_login)
        .build(app)?;
    let minimized = CheckMenuItemBuilder::new("Start minimized")
        .id("toggle:start_minimized")
        .checked(cfg.start_minimized)
        .build(app)?;
    let prefs = SubmenuBuilder::new(app, "Preferences")
        .item(&launch)
        .item(&minimized)
        .build()?;

    let resolver = MenuItemBuilder::new("Resolver settings…")
        .id("resolver_settings")
        .build(app)?;

    MenuBuilder::new(app)
        .item(&server)
        .item(&prefs)
        .item(&resolver)
        .build()
}

/// Rebuild and reinstall the app menu + tray menu after config changes so the
/// profile list and toggle checkmarks stay in sync.
fn refresh_menu(app: &tauri::AppHandle) {
    if let Ok(menu) = build_menu(app) {
        let _ = app.set_menu(menu);
    }
}

/// Navigate the app window to a path on the current server (e.g. the resolver
/// admin page). Runs the navigation from *inside* the page via `eval` with a
/// **root-relative** path (`/admin/...`), so it always resolves against the origin
/// the webview is already showing — no dependence on the stored `active` URL
/// matching, and no absolute URL to get out of sync. Only the
/// window(s) actually loaded on an http(s) origin are touched (never the bundled
/// connect screen, whose document is a local `tauri://` page).
fn navigate_active_window(app: &tauri::AppHandle, path: &str) {
    // JSON-encode the path so it's a safe, correctly-quoted JS string literal.
    let literal = serde_json::to_string(path).unwrap_or_else(|_| "\"/\"".to_string());
    let js = format!("window.location.assign({literal});");
    for (_, window) in app.webview_windows() {
        let on_server = window
            .url()
            .map(|u| matches!(u.scheme(), "http" | "https"))
            .unwrap_or(false);
        if on_server {
            let _ = window.eval(&js);
            let _ = window.show();
            let _ = window.set_focus();
        }
    }
}

/// Reconcile the OS autostart entry with the stored preference.
fn apply_autostart(app: &tauri::AppHandle, enabled: bool) {
    let manager = app.autolaunch();
    let _ = if enabled { manager.enable() } else { manager.disable() };
}

fn handle_menu_event(app: &tauri::AppHandle, id: &str) {
    match id {
        "switch" => {
            let mut cfg = read_config(app);
            cfg.active = None; // keep profiles for prefill, drop auto-connect
            let _ = write_config(app, &cfg);
            let _ = open_connect_window(app);
            refresh_menu(app);
        }
        "add" => {
            let _ = open_connect_window(app);
        }
        "resolver_settings" => {
            navigate_active_window(app, "/admin/resolver-settings");
        }
        "toggle:launch_on_login" => {
            let mut cfg = read_config(app);
            cfg.launch_on_login = !cfg.launch_on_login;
            let enabled = cfg.launch_on_login;
            let _ = write_config(app, &cfg);
            apply_autostart(app, enabled);
            refresh_menu(app);
        }
        "toggle:start_minimized" => {
            let mut cfg = read_config(app);
            cfg.start_minimized = !cfg.start_minimized;
            let _ = write_config(app, &cfg);
            refresh_menu(app);
        }
        "tray:open" => {
            // Bring the app window to the front (or reconnect if none).
            let cfg = read_config(app);
            match cfg.active_url() {
                Some(url) if app.webview_windows().len() > 0 => {
                    for (_, w) in app.webview_windows() {
                        let _ = w.show();
                        let _ = w.set_focus();
                    }
                    let _ = url; // window already on the right origin
                }
                Some(url) => {
                    let _ = open_app_window(app, &url);
                }
                None => {
                    let _ = open_connect_window(app);
                }
            }
        }
        other => {
            if let Some(url) = other.strip_prefix("profile:") {
                // Switch to the chosen saved server.
                let mut cfg = read_config(app);
                cfg.active = Some(url.to_string());
                let _ = write_config(app, &cfg);
                let _ = open_app_window(app, url);
                refresh_menu(app);
            }
        }
    }
}

#[tauri::command]
fn last_server_url(app: tauri::AppHandle) -> Option<String> {
    let cfg = read_config(&app);
    // Prefer the active server, else the most recently used profile.
    cfg.active.clone().or_else(|| cfg.servers.first().map(|p| p.url.clone()))
}

#[tauri::command]
async fn connect_to_server(app: tauri::AppHandle, url: String) -> Result<(), String> {
    let normalized = normalize_url(&url)?;
    check_reachable(&normalized).await?;
    let mut cfg = read_config(&app);
    cfg.upsert_active(&normalized);
    write_config(&app, &cfg)?;
    open_app_window(&app, &normalized)?;
    refresh_menu(&app);
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .invoke_handler(tauri::generate_handler![connect_to_server, last_server_url])
        .on_menu_event(|app, event| handle_menu_event(app, event.id().0.as_str()))
        .setup(|app| {
            let handle = app.handle();

            // Install the config-driven menu.
            let menu = build_menu(handle)?;
            app.set_menu(menu)?;

            // System tray: quick access to the dashboard and the resolver page.
            // `?` here is deliberate — a tray we can't build is a broken install,
            // so fail startup loudly rather than launch a half-wired app.
            let tray_menu = MenuBuilder::new(handle)
                .text("tray:open", "Open Stingray Tickets")
                .item(
                    &MenuItemBuilder::new("Resolver settings…")
                        .id("resolver_settings")
                        .build(handle)?,
                )
                .separator()
                .quit()
                .build()?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Stingray Tickets")
                .menu(&tray_menu)
                .on_menu_event(|app, event| handle_menu_event(app, event.id().0.as_str()))
                .build(handle)?;

            // Reconcile autostart with the saved preference on every launch.
            let cfg = read_config(handle);
            apply_autostart(handle, cfg.launch_on_login);

            // Boot into the active server, else the connect screen.
            match cfg.active_url() {
                Some(url) => open_app_window(handle, &url),
                None => open_connect_window(handle),
            }
            .map_err(|e| -> Box<dyn std::error::Error> { e.into() })?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running Stingray Tickets")
        .run(|_app, event| {
            // Tauri's default exit behavior is what we want: closing the last
            // window quits, and the tray does not hold the process open. Matched
            // explicitly so the intent is visible if that ever needs to change.
            if let RunEvent::ExitRequested { .. } = event {
                // no-op: allow normal exit
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn legacy(url: &str) -> DesktopConfig {
        DesktopConfig { url: Some(url.into()), connected: true, ..Default::default() }
    }

    #[test]
    fn migrate_legacy_normalizes_the_url_it_adopts() {
        // A trailing slash (or a missing scheme) in the old single-URL file must not
        // survive into the profile id — ids are normalized URLs everywhere else.
        let mut cfg = legacy("http://box:3000/");
        cfg.migrate_legacy();
        assert_eq!(cfg.servers.len(), 1);
        assert_eq!(cfg.servers[0].id, "http://box:3000");
        assert_eq!(cfg.active.as_deref(), Some("http://box:3000"));
        assert!(cfg.url.is_none() && !cfg.connected);

        let mut cfg = legacy("box:3000");
        cfg.migrate_legacy();
        assert_eq!(cfg.servers[0].id, "https://box:3000");
    }

    #[test]
    fn migrated_profile_is_reused_not_duplicated_on_reconnect() {
        // The regression this guards: connecting again to the same server (which
        // goes through normalize_url) used to append a second profile and orphan
        // the menu checkmark, because the migrated id kept its trailing slash.
        let mut cfg = legacy("http://box:3000/");
        cfg.migrate_legacy();
        cfg.upsert_active(&normalize_url("http://box:3000/").unwrap());
        assert_eq!(cfg.servers.len(), 1);
        assert_eq!(cfg.active.as_deref(), Some(cfg.servers[0].id.as_str()));
    }

    #[test]
    fn migrate_legacy_keeps_an_unparseable_url() {
        let mut cfg = legacy("   ");
        cfg.migrate_legacy();
        assert_eq!(cfg.servers[0].id, "   ");   // still migrated, just not normalized
    }
}
