import { defineConfig, loadEnv } from "vite";
import { readFileSync } from "node:fs";
import react from "@vitejs/plugin-react";

const projectVersion = readFileSync(new URL("../VERSION", import.meta.url), "utf-8").trim();

export default defineConfig(({ mode }) => {
  // Server-only configuration allows shared-backend development without
  // exposing environment values in the browser or changing local defaults.
  const env = loadEnv(mode, process.cwd(), "QF_DEV_");
  const backendTarget = process.env.QF_DEV_BACKEND_URL || env.QF_DEV_BACKEND_URL || "http://127.0.0.1:8000";
  return {
    define: {
      __QF_VERSION__: JSON.stringify(projectVersion)
    },
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": backendTarget,
        "/docs": backendTarget,
        "/openapi.json": backendTarget,
        "/redoc": backendTarget
      }
    }
  };
});
