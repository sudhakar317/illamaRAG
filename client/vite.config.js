import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,

    // Listen on every network interface, not just localhost, so the app can be
    // opened from a phone on the same Wi-Fi. Vite prints the LAN URL on start.
    //
    // Only this port needs to be reachable: the proxy below runs on the dev
    // server, so the phone talks to :5173 and Vite forwards to the backend
    // over loopback. The FastAPI server stays bound to 127.0.0.1 and never
    // needs to be exposed.
    host: true,

    // Accept the LAN IP and any .local hostname in the Host header. Without
    // this, Vite rejects requests whose host it does not recognise.
    allowedHosts: true,

    proxy: {
      // Proxying in dev means the browser only ever talks to one origin, so
      // no CORS preflight and no hardcoded backend URL in the client code.
      '/api': {
        // run.sh sets API_TARGET when SERVER_PORT moves the backend.
        target: process.env.API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        // Replies stream token by token; buffering here would defeat that.
        ws: false,
      },
    },
  },
})
