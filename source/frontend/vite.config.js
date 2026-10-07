import { resolve } from "path";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

function adminPageFallback() {
  const rewriteAdminRoute = (req, _res, next) => {
    if (req.url && req.url.startsWith("/admin") && !req.url.startsWith("/api")) {
      req.url = "/admin.html";
    }
    next();
  };

  return {
    name: "admin-page-fallback",
    configureServer(server) {
      server.middlewares.use(rewriteAdminRoute);
    },
    configurePreviewServer(server) {
      server.middlewares.use(rewriteAdminRoute);
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  return {
    plugins: [react(), adminPageFallback()],
    build: {
      // Keep old hashed chunks so already-open pages can still load lazy modules after a deploy.
      emptyOutDir: false,
      rollupOptions: {
        input: {
          main: resolve(__dirname, "index.html"),
          admin: resolve(__dirname, "admin.html"),
        },
      },
    },
    server: {
      host: "0.0.0.0",
      port: 4173,
      proxy: {
        "/api": {
          target: env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000",
          changeOrigin: true,
          ws: true,
          configure: (proxy) => {
            proxy.on("proxyReq", (proxyReq) => {
              proxyReq.setHeader("Accept-Encoding", "identity");
            });
          }
        },
        "/hub": {
          target: env.VITE_VIBE_HUB_PROXY_TARGET || "http://127.0.0.1:8002",
          changeOrigin: true,
          ws: true
        }
      }
    }
  };
});
