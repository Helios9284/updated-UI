import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Backend port follows BACKEND_PORT (set by run_frontend.sh); defaults to 8772
// so this app runs on its own ports, separate from the legacy dash backend.
const BACKEND_PORT = process.env.BACKEND_PORT || '8772'
const httpTarget = `http://127.0.0.1:${BACKEND_PORT}`
const wsTarget = `ws://127.0.0.1:${BACKEND_PORT}`

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/ws': {
        target: wsTarget,
        ws: true,
        changeOrigin: true,
      },
      // Stake API is mounted on the same backend process at /stake-api.
      // Keep the prefix (no rewrite) so it matches the mounted sub-app.
      '/stake-api': {
        target: httpTarget,
        changeOrigin: true,
        ws: true,
      },
      '/api': {
        target: httpTarget,
        changeOrigin: true,
      },
      '/health': {
        target: httpTarget,
        changeOrigin: true,
      },
    },
  },
})
