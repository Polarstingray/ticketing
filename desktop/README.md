# Stingray Tickets — Desktop app

A small cross-platform desktop client (Ubuntu Linux + macOS) for a **self-hosted
Stingray Tickets** server, built with [Tauri 2](https://tauri.app). On first
launch it asks for your server's address, then opens the normal web app in a
native window and logs you in exactly as the browser does — so there are **no
backend or frontend changes** required to use it.

```
┌─────────────────────────┐        ┌──────────────────────────────┐
│  Server URL: https://…   │  ───▶  │  the full Stingray web app,   │
│        [ Connect ]       │        │  running against your server  │
└─────────────────────────┘        └──────────────────────────────┘
```

## How it works

- The bundled connect screen (`src/`) collects a server URL. The Rust shell
  (`src-tauri/src/main.rs`) normalizes it, verifies the server answers like a
  Stingray instance (`GET /api/auth/me` → 200 or 401), remembers it, then opens
  a window pointed **directly at that origin**.
- Because the window's document origin *is* the server, the existing session
  cookie (`httponly`, `SameSite=Lax`) and CORS rules work unchanged. The system
  webview (WebKitGTK / WKWebView) persists the cookie, so you stay logged in
  across restarts.
- **Server ▸ Switch server…** returns to the connect screen (your last URL is
  prefilled). External links open in your real browser.

Point the app at the **same address you use in the browser** (the web
frontend, which proxies `/api` to the backend) — not the backend port directly.

## Develop

Prerequisites:

- [Rust toolchain](https://rustup.rs) (stable) and Node 20.
- **Ubuntu system dependencies:**
  ```bash
  sudo apt-get install -y \
    libwebkit2gtk-4.1-dev build-essential curl wget file \
    libssl-dev libayatana-appindicator3-dev librsvg2-dev
  ```
- **macOS:** Xcode Command Line Tools (`xcode-select --install`).

Run in dev (hot-reloads the connect screen; the app window loads your live
server):

```bash
cd desktop
npm install
npm run tauri:dev
```

## Build installers

```bash
cd desktop
npm run tauri:build
```

Artifacts land in `src-tauri/target/release/bundle/`:

- **Linux:** `deb/*.deb` and `appimage/*.AppImage`
- **macOS:** `dmg/*.dmg` and `macos/*.app`

> The macOS `.dmg` is **unsigned** for now — on first open, right-click the app
> and choose *Open* to get past Gatekeeper. Code signing / notarization is
> planned for a later iteration.

### Published releases

Pushing a `desktop-v*` tag builds the installers on Ubuntu + macOS and attaches
them to a GitHub Release (`.github/workflows/release-desktop.yml`), separate from
the app/image release (`v*`):

```bash
git tag desktop-v0.1.0
git push origin desktop-v0.1.0
```

## Icons

Placeholder icons live in `src-tauri/icons/`. To regenerate the full set from a
1024×1024 source, run:

```bash
npm run tauri icon src-tauri/icons/app-icon.png
```

## Not yet included (planned)

Configurable AI-resolver settings, native OS notifications, a system tray,
auto-update, and macOS code signing.
