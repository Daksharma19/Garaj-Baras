// Base-map tiles. Mapbox when a token is configured (MAPBOX_ACCESS_TOKEN in the
// repo-root .env, injected by vite.config.js); otherwise the previous CARTO /
// OSM tiles so the map never goes blank.

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_ACCESS_TOKEN
const CARTO_KEY = import.meta.env.VITE_CARTO_API_KEY

const MAPBOX_ATTRIBUTION =
  '&copy; <a href="https://www.mapbox.com/about/maps/">Mapbox</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'

function mapboxLayer(styleId) {
  return {
    url: `https://api.mapbox.com/styles/v1/mapbox/${styleId}/tiles/512/{z}/{x}/{y}@2x?access_token=${MAPBOX_TOKEN}`,
    attribution: MAPBOX_ATTRIBUTION,
    tileSize: 512,
    zoomOffset: -1,
    maxZoom: 20,
  }
}

/** kind: 'route' (dark results map) | 'nav' (turn-by-turn) | 'light' (India radar) */
export function baseTiles(kind) {
  if (MAPBOX_TOKEN) {
    if (kind === 'nav') return mapboxLayer('dark-v11') // muted; route is the only bright thing
    if (kind === 'light') return mapboxLayer('light-v11')
    return mapboxLayer('dark-v11')
  }
  if (kind === 'light') {
    return { url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', attribution: '&copy; OpenStreetMap contributors' }
  }
  return {
    url: `https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}{r}.png?key=${CARTO_KEY}`,
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
  }
}
