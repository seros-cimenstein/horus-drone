import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const backendTarget = process.env.HORUS_STUDIO_URL || "http://127.0.0.1:8765";

export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": { target: backendTarget, changeOrigin: true },
      "/events": { target: backendTarget, changeOrigin: true },
      "/task": { target: backendTarget, changeOrigin: true },
      "/emergency": { target: backendTarget, changeOrigin: true },
      "/reset": { target: backendTarget, changeOrigin: true },
      "/version": { target: backendTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: "../horus/commander/web",
    emptyOutDir: false,
    cssCodeSplit: false,
    sourcemap: false,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        entryFileNames: "app.js",
        chunkFileNames: "app-[name].js",
        assetFileNames: (assetInfo) => {
          if (assetInfo.name && assetInfo.name.endsWith(".css")) {
            return "app.css";
          }
          return "[name][extname]";
        },
      },
    },
  },
});
