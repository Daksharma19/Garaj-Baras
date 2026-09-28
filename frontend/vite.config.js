import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // MAPBOX_ACCESS_TOKEN lives in the repo-root .env (or the host's env vars);
  // expose it to the client as import.meta.env.VITE_MAPBOX_ACCESS_TOKEN.
  const rootEnv = loadEnv(mode, '..', 'MAPBOX_')
  const localEnv = loadEnv(mode, process.cwd(), ['MAPBOX_', 'VITE_MAPBOX_'])
  const mapboxToken =
    localEnv.VITE_MAPBOX_ACCESS_TOKEN || localEnv.MAPBOX_ACCESS_TOKEN || rootEnv.MAPBOX_ACCESS_TOKEN || ''

  return {
  define: {
    'import.meta.env.VITE_MAPBOX_ACCESS_TOKEN': JSON.stringify(mapboxToken),
  },
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Vite 8 (rolldown) expects a function here.
        manualChunks(id) {
          if (!id) return
          if (id.includes('node_modules')) {
            if (id.includes('/leaflet/') || id.includes('\\leaflet\\')) return 'leaflet'
            if (id.includes('/react-leaflet/') || id.includes('\\react-leaflet\\'))
              return 'leaflet'
            if (id.includes('/axios/') || id.includes('\\axios\\')) return 'axios'
            return 'vendor'
          }
        },
      },
    },
  },
}
})
