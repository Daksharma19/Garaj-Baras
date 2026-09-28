import { useEffect, useMemo, useState } from 'react'
import { ImageOverlay, MapContainer, TileLayer } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'
import { baseTiles } from './mapTiles'

const API_BASE = import.meta.env.DEV
  ? 'http://127.0.0.1:8000'
  : (import.meta.env.VITE_API_BASE || 'https://garaj-baras-api.onrender.com')
const FALLBACK_BOUNDS = [[6, 68], [38, 98]]
const MAX_MERCATOR_LAT = 85.05112878

function mercatorY(lat) {
  const safeLat = Math.max(-MAX_MERCATOR_LAT, Math.min(MAX_MERCATOR_LAT, lat))
  return Math.log(Math.tan(Math.PI / 4 + (safeLat * Math.PI / 180) / 2))
}

function buildNoDataMask(meta) {
  if (!meta?.stations?.length) return null

  const [[south, west], [north, east]] = meta.bounds || FALLBACK_BOUNDS
  const width = 1500
  const height = 1600
  const mercSouth = mercatorY(south)
  const mercNorth = mercatorY(north)
  const latScale = 111.32

  const ellipses = meta.stations.map((station) => {
    const cosLat = Math.max(0.2, Math.abs(Math.cos(station.lat * Math.PI / 180)))
    const cx = ((station.lon - west) / (east - west)) * width
    const cy = ((mercNorth - mercatorY(station.lat)) / (mercNorth - mercSouth)) * height
    const rx = (station.range_km / ((east - west) * latScale * cosLat)) * width
    const ry = (station.range_km / ((mercNorth - mercSouth) * latScale)) * height
    return `<ellipse cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" rx="${Math.abs(rx).toFixed(1)}" ry="${Math.abs(ry).toFixed(1)}" fill="white" />`
  }).join('')

  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <defs>
        <filter id="soften" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="18" />
        </filter>
        <mask id="coverage-mask">
          <rect width="100%" height="100%" fill="black" />
          <g filter="url(#soften)">${ellipses}</g>
        </mask>
      </defs>
      <rect width="100%" height="100%" fill="#0f172a" fill-opacity="0.28" mask="url(#coverage-mask)" />
    </svg>
  `.trim()

  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`
}

export default function IndiaRadarMap() {
  const [meta, setMeta] = useState({ bounds: FALLBACK_BOUNDS, stations: [] })
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)
  const [refreshWindow, setRefreshWindow] = useState(() => Math.floor(Date.now() / 300000))
  const mosaicUrl = useMemo(() => `${API_BASE}/india-radar/mosaic.png?window=${refreshWindow}`, [refreshWindow])
  const noDataMaskUrl = useMemo(() => buildNoDataMask(meta), [meta])

  useEffect(() => {
    fetch(`${API_BASE}/india-radar/metadata`).then((r) => r.ok ? r.json() : Promise.reject())
      .then((data) => setMeta(data)).catch(() => setFailed(true))
  }, [])

  useEffect(() => {
    const id = window.setInterval(() => {
      setLoaded(false)
      setRefreshWindow(Math.floor(Date.now() / 300000))
    }, 300000)
    return () => window.clearInterval(id)
  }, [])

  return (
    <div className="india-radar-map">
      <MapContainer center={[22.5, 80.5]} zoom={5} minZoom={4} maxZoom={9} scrollWheelZoom className="india-radar-map__canvas">
        <TileLayer {...baseTiles('light')} />
        {noDataMaskUrl && <ImageOverlay url={noDataMaskUrl} bounds={meta.bounds || FALLBACK_BOUNDS} opacity={1} zIndex={390} />}
        <ImageOverlay url={mosaicUrl} bounds={meta.bounds || FALLBACK_BOUNDS} opacity={0.9} zIndex={420} eventHandlers={{ load: () => setLoaded(true), error: () => setFailed(true) }} />
      </MapContainer>
      <div className="india-radar-map__status">
        <span className="india-radar-map__echo" /> Live composite
        <span className="india-radar-map__nodata" /> Limited coverage
        {!loaded && !failed && <span>Loading radar frames...</span>}
        {failed && <span>Radar mosaic is temporarily unavailable.</span>}
      </div>
    </div>
  )
}
