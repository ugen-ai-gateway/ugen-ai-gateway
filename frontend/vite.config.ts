import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import darkTheme from "./darkTheme";

/** Self-hosted fonts list a .woff fallback after each .woff2 file; every
 * browser the dashboard supports reads woff2, so leave the fallbacks out of
 * the CSS and the build. */
const woff2Only: Plugin = {
  name: "ugen-woff2-only",
  enforce: "pre",
  transform(code, id) {
    if (!/\/@fontsource\/.+\.css$/.test(id)) return null;
    return { code: code.replace(/,\s*url\([^)]+\.woff\) format\('woff'\)/g, ""), map: null };
  },
};

declare const process: { env: Record<string, string | undefined> };
const backendPort = process.env.UGEN_BACKEND_PORT ?? "7632";

export default defineConfig({
  plugins: [react(), darkTheme(), woff2Only],
  // The Markdown/KaTeX/highlight chunk is large but loads only with the first reply.
  build: { chunkSizeWarningLimit: 700 },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
