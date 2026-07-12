import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/socket.io": { target: "ws://127.0.0.1:8000", ws: true }
    }
  },
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      workbox: { clientsClaim: true, skipWaiting: true },
      manifest: {
        name: "StraddleUp",
        short_name: "StraddleUp",
        theme_color: "#0b0e14",
        background_color: "#0b0e14",
        display: "standalone",
        icons: [{ src: "/icon.svg", sizes: "any", type: "image/svg+xml", purpose: "any" }]
      }
    })
  ]
});
