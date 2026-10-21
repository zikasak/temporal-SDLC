import { defineConfig } from "vite";

export default defineConfig({
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // The prefix stripped below, so the API can name its public URLs (webhook).
        headers: { "X-Forwarded-Prefix": "/api" },
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
