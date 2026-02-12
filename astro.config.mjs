// @ts-check
import { defineConfig } from "astro/config";
import fs from "node:fs";
import path from "node:path";

// Vite plugin to serve pre-built mdbook docs during dev.
// Looks in book/ (default build_docs.py output) which survives Astro's dist/ rebuilds.
function serveBuiltDocs() {
  const MIME = { ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
    ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml",
    ".jpg": "image/jpeg", ".woff": "font/woff", ".woff2": "font/woff2" };
  return {
    name: "serve-built-docs",
    configureServer(server) {
      const docsDir = path.resolve("book");
      server.middlewares.use((req, res, next) => {
        const prefix = "/docs/";
        // Handle without trailing slash
        if (req.url === "/docs") {
          res.writeHead(302, { Location: "/caliptra-web/docs/" });
          return res.end();
        }
        if (!req.url?.startsWith(prefix)) return next();
        let rel = req.url.slice(prefix.length).split("?")[0].split("#")[0];
        if (!rel || rel.endsWith("/")) rel += "index.html";
        const fp = path.resolve(docsDir, rel);
        if (!fp.startsWith(docsDir) || !fs.existsSync(fp)) return next();
        res.setHeader("Content-Type", MIME[path.extname(fp)] || "application/octet-stream");
        fs.createReadStream(fp).pipe(res);
      });
    },
  };
}

// https://astro.build/config
export default defineConfig({
  base: "/caliptra-web",

  build: {
    inlineStylesheets: "always",
    assets: "assets",
    assetsPrefix: undefined,
    format: "file",
  },

  vite: {
    plugins: [serveBuiltDocs()],
    build: {
      // Ensure proper MIME types
      rollupOptions: {
        output: {
          entryFileNames: "assets/[name].[hash].js",
          chunkFileNames: "assets/[name].[hash].js",
          assetFileNames: "assets/[name].[hash][extname]",
        },
      },
    },
    server: {
      fs: {
        strict: true,
      },
    },
  },
});
