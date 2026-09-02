import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    // Browser calls /api -> vite proxies to FastAPI. Never hardcode localhost in the browser.
    // /auth is proxied too, so the HttpOnly refresh cookie stays same-origin.
    proxy: {
      '/api': 'http://localhost:8000',
      '/auth': 'http://localhost:8000',
    },
  },
})
