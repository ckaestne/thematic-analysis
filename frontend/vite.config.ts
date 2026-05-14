import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Build the SPA straight into the Python package so `ta-web` can serve it
// without a separate static asset pipeline.
const outDir = path.resolve(
  __dirname,
  "../src/thematic_analysis_inc/web_static",
);

export default defineConfig({
  plugins: [react()],
  build: {
    outDir,
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
});
