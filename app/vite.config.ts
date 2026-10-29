import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// Tauri expects a fixed dev port and never opens a browser by itself.
// The frontend is a plain SPA: the sidecar is reached over localhost HTTP/WS.
export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  server: {
    // tauri.conf.json devUrl is http://localhost:1420
    host: 'localhost',
    port: 1420,
    strictPort: true,
    watch: {
      // src-tauri is owned by another agent; locked files there crash the watcher.
      ignored: ['**/src-tauri/**'],
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
  },
  envPrefix: ['VITE_', 'TAURI_'],
  build: {
    // WebView2 on Windows 10/11 is Chromium-based; Tauri 2 baseline.
    target: 'chrome105',
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
});
