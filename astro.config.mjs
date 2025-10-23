// @ts-check
import { defineConfig } from "astro/config";

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
