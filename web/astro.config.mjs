// @ts-check
import { defineConfig } from "astro/config";

// The FastAPI server. Override with API_ORIGIN when it runs elsewhere.
const API = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

export default defineConfig({
  output: "static",
  server: { host: "127.0.0.1", port: 4321 },
  vite: {
    server: {
      proxy: {
        // Same-origin in development, so no CORS and no base URL in the client.
        // Server-sent events need buffering off, hence `ws: false` plus a long timeout.
        "/api": {
          target: API,
          changeOrigin: true,
          timeout: 0,
          proxyTimeout: 0,
        },
      },
    },
  },
});
