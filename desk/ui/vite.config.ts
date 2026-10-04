import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { defineConfig } from 'vite';

// Built into desk/static/ui and served by desk/bridge.py, whose Flask app
// serves desk/static at /static -- so every asset URL must start there.
export default defineConfig({
  base: '/static/ui/',
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  build: {
    outDir: path.resolve(__dirname, '../static/ui'),
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    // No source maps in the shipped app: they double the bundle and the
    // installer carries them to every user for no benefit.
    sourcemap: false,
  },
  server: {
    // `npm run dev` against a running NOVA backend on its default port.
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/ws': { target: 'ws://127.0.0.1:8765', ws: true },
    },
  },
});
