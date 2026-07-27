import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Throwaway SQLite DB for the E2E run (wiped in global-setup so each run is clean).
const E2E_DB = path.join(__dirname, "e2e", ".data", "e2e.db");

// Backend env: dev-mode secrets + the seeded admin the happy-path logs in as.
const backendEnv = {
  DATABASE_PATH: E2E_DB,
  // Every test signs in, and the whole suite runs inside one minute from a single
  // IP — the production 5/minute login budget would 429 the later specs and leave
  // them stranded on /login. Widen it for the throwaway E2E server only.
  LOGIN_RATE_LIMIT: "1000/minute",
  SESSION_SECRET: "e2e-only-secret",
  ADMIN_USERNAME: "admin",
  ADMIN_PASSWORD: "adminpass123",
  ADMIN_EMAIL: "admin@example.com",
};

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://localhost:5273",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  // Dedicated ports (not the dev defaults 8000/5173) so a running dev stack — or
  // another app squatting on :8000 — doesn't collide with the E2E run.
  webServer: [
    {
      // Wipe any prior E2E DB *before* the server opens it (a clean run each time),
      // then start uvicorn. Doing the wipe here — not in a globalSetup — avoids
      // unlinking the SQLite file out from under a running server (which SQLite
      // reports as "attempt to write a readonly database"). Prefer the local venv;
      // fall back to a system python (CI installs deps there).
      command:
        'sh -c "rm -f $DATABASE_PATH $DATABASE_PATH-journal $DATABASE_PATH-wal; cd ../backend && { [ -x .venv/bin/python ] && .venv/bin/python -m uvicorn main:app --port 8123 || python -m uvicorn main:app --port 8123; }"',
      url: "http://localhost:8123/health",
      reuseExistingServer: false,
      timeout: 60_000,
      env: backendEnv,
    },
    {
      command: "npm run dev -- --port 5273 --strictPort",
      url: "http://localhost:5273",
      reuseExistingServer: false,
      timeout: 60_000,
      env: { VITE_PROXY_TARGET: "http://localhost:8123" },
    },
  ],
});
