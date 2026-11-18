import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server proxies /v1 to the API as nginx does in the image, so the browser sees one origin
// and the API needs no CORS change (ADR-0018 decision 1).
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    host: "127.0.0.1",
    port: 3003,
    proxy: { "/v1": process.env.API_URL ?? "http://localhost:8000" },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
} as Parameters<typeof defineConfig>[0]);
