import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'

// In development the app's /api calls go to a running UI server (ui_server/app.py), normally the server's,
// through an SSH tunnel: TRACEHOLD_API=http://localhost:8502 npm run dev
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    proxy: { '/api': { target: process.env.TRACEHOLD_API ?? 'http://localhost:8502', changeOrigin: true } },
  },
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 1600 },
})
