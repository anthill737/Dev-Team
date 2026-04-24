/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 3939,
    open: true,  // Auto-open browser when dev server is ready. Avoids launcher-script timing issues.
    proxy: {
      "/api": {
        target: process.env.VITE_BACKEND_URL ?? "http://localhost:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: (process.env.VITE_BACKEND_URL ?? "http://localhost:8000").replace(
          /^http/,
          "ws",
        ),
        ws: true,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
