import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

export default defineConfig({
  plugins: [svelte()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/v1': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
    fs: {
      strict: true,
      deny: ['.env', '.env.*', 'package.json', 'package-lock.json'],
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
})
