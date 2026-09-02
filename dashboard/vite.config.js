import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    // Vite 6 rejects unknown Host headers. Local dev is unaffected; this only
    // lets the app be reached through a hosted preview/tunnel domain.
    allowedHosts: true,
    // Browser calls /api -> vite proxies to FastAPI. Never hardcode localhost in the browser.
    // /auth is proxied too, so the HttpOnly refresh cookie stays same-origin.
    proxy: {
      '/api': 'http://localhost:8000',
      '/auth': 'http://localhost:8000',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.js'],
    include: ['tests/**/*.test.{js,jsx}'],
    // The XSS-boundary test greps the real source tree, so it must not be
    // confused by a build directory.
    exclude: ['node_modules', 'dist'],
    restoreMocks: true,
  },
})
