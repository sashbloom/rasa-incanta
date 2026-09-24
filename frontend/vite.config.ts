import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Served under /reports/rasa-incanta/ so the Practus Portal can proxy the full path
// (CGO reports standard). FastAPI serves the same build at / too.
export default defineConfig({
  base: '/reports/rasa-incanta/',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../api/static',
    emptyOutDir: true,
  },
  server: { port: 5173 },
})
