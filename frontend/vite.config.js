/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy /api to the backend so the browser stays same-origin and the
// session cookie works without CORS friction.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // Overridable so the E2E stack can point at its own backend port.
        target: process.env.VITE_PROXY_TARGET || "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.js",
    // Unit/component tests live under src/; Playwright E2E specs under e2e/ are
    // run by Playwright, not Vitest.
    include: ["src/**/*.{test,spec}.{js,jsx}"],
  },
});
