import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA calls /api/*; in dev Vite proxies that to the backend so the browser
// sees one origin and cookies work without CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
