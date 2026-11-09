import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Served by the diagnostics app at /diagnostics; assets resolve under the same mount.
  base: "/diagnostics/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1/diagnostics": "http://127.0.0.1:8100",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./vitest.setup.ts",
    css: false,
  },
});
