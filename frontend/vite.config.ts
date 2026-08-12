import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");

  // In dev, proxy /ws and /api to the backend container (or localhost:8000).
  const backendOrigin = env.VITE_BACKEND_ORIGIN ?? "http://backend:8000";
  const wsOrigin      = backendOrigin.replace(/^http/, "ws");

  return {
    plugins: [react()],

    server: {
      host:      "0.0.0.0",
      port:       5173,
      strictPort: true,
      proxy: {
        "/ws": {
          target:      wsOrigin,
          ws:           true,
          changeOrigin: true,
          rewrite:      (path) => path,
        },
        "/api": {
          target:       backendOrigin,
          changeOrigin: true,
          rewrite:      (path) => path.replace(/^\/api/, ""),
        },
      },
    },

    build: {
      outDir:    "dist",
      sourcemap:  false,
      // Split vendor bundle from app code for better long-term caching.
      rollupOptions: {
        output: {
          manualChunks: {
            vendor: ["react", "react-dom"],
          },
        },
      },
    },
  };
});
