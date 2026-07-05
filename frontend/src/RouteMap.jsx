import { useEffect, useMemo, useRef, useState } from 'react'
import L from 'leaflet'
import { MapContainer, Marker, Popup, Polyline, TileLayer } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'

// ── Journey animation timing ──────────────────────────────────────────────────
const DRIVE_MS = 3200      // whole trip drives past in ~3.2s (excluding stops)
const STOP_PAUSE_MS = 1500 // how long the car waits at each rain stop

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371
  const dLat = ((lat2 - lat1) * Math.PI) / 180
  const dLon = ((lon2 - lon1) * Math.PI) / 180
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLon / 2) ** 2
  return R * 2 * Math.asin(Math.sqrt(a))
}

function toISTClock(etaMins) {
  const ist = new Date(Date.now() + etaMins * 60 * 1000 + 5.5 * 60 * 60 * 1000)
  return `${String(ist.getUTCHours()).padStart(2, '0')}:${String(ist.getUTCMinutes()).padStart(2, '0')}`
}

/**
 * Precompute the journey model:
 *  - cumKm[i]: distance along routeCoords ([lat,lon] pairs)
 *  - maxEta:  trip duration (last waypoint ETA)
 *  - stops:   contiguous rainy waypoint groups -> one stop each (entry point)
 */
function buildJourney(routeCoords, waypoints) {
  if (!Array.isArray(routeCoords) || routeCoords.length < 2) return null
  const wps = (waypoints || []).filter((w) => Number.isFinite(Number(w?.eta_mins)))
  if (wps.length < 2) return null

  const cumKm = [0]
  let acc = 0
  for (let i = 1; i < routeCoords.length; i++) {
    acc += haversineKm(routeCoords[i - 1][0], routeCoords[i - 1][1], routeCoords[i][0], routeCoords[i][1])
    cumKm.push(acc)
  }
  const totalKm = acc || 1e-6
  const sorted = [...wps].sort((a, b) => Number(a.eta_mins) - Number(b.eta_mins))
  const maxEta = Number(sorted[sorted.length - 1].eta_mins) || 0
  if (maxEta <= 0) return null

  // Contiguous rain groups -> stop at group entry
  const stops = []
  let group = null
  for (const w of sorted) {
    if (w.rain_expected) {
      if (!group) {
        group = { entry: w, labels: [w.label], endEta: Number(w.eta_mins) }
      } else {
        group.labels.push(w.label)
        group.endEta = Number(w.eta_mins)
      }
    } else if (group) {
      stops.push(group); group = null
    }
  }
  if (group) stops.push(group)

  const stopPoints = stops.map((g) => ({
    lat: Number(g.entry.lat),
    lon: Number(g.entry.lon),
    eta_mins: Number(g.entry.eta_mins),
    endEta: g.endEta,
    label: g.entry.label || 'Rain',
    color: g.entry.rainColor || g.entry.color || '#38BDF8',
  }))

  return { cumKm, totalKm, maxEta, stops: stopPoints }
}

/** Position along the polyline at trip-time t (minutes), constant-speed model. */
function carPositionAt(journey, routeCoords, tMins) {
  const { cumKm, totalKm, maxEta } = journey
  const km = Math.max(0, Math.min(1, tMins / maxEta)) * totalKm
  // binary search cumKm
  let lo = 0, hi = cumKm.length - 1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (cumKm[mid] < km) lo = mid + 1
    else hi = mid
  }
  const i = Math.max(1, lo)
  const span = cumKm[i] - cumKm[i - 1] || 1e-9
  const u = Math.max(0, Math.min(1, (km - cumKm[i - 1]) / span))
  const [lat1, lon1] = routeCoords[i - 1]
  const [lat2, lon2] = routeCoords[i]
  return [lat1 + u * (lat2 - lat1), lon1 + u * (lon2 - lon1)]
}

const carIcon = L.divIcon({
  className: 'car-marker-wrap',
  html: '<div class="car-marker">🚗</div>',
  iconSize: [30, 30],
  iconAnchor: [15, 15],
})

function stopIcon(color) {
  return L.divIcon({
    className: 'stop-marker-wrap',
    html: `<div class="stop-marker" style="--stop-color:${color}">⛈</div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  })
}

export default function RouteMap({
  routeCoords,
  routeSegments,
  waypoints,
  activeSeg,
  setActiveSeg,
  openSegmentPopup,
  onStopDetails,
}) {
  const [mapRef, setMapRef] = useState(null)

  // ── Journey state ────────────────────────────────────────────────────────
  const journey = useMemo(
    () => buildJourney(routeCoords, waypoints),
    [routeCoords, waypoints],
  )
  const [journeyMode, setJourneyMode] = useState('idle') // idle | playing | done
  const [carT, setCarT] = useState(0)                    // trip minutes elapsed
  const [activeStop, setActiveStop] = useState(null)
  const rafRef = useRef(null)
  const animRef = useRef(null) // { mode, t, lastTs, stopIdx, resumeAt }
  const journeyRef = useRef(null)
  journeyRef.current = journey

  function cancelAnim() {
    if (rafRef.current) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
  }

  function startJourney() {
    const j = journeyRef.current
    if (!j) return
    cancelAnim()
    animRef.current = { mode: 'playing', t: 0, lastTs: performance.now(), stopIdx: 0, resumeAt: 0 }
    setJourneyMode('playing')
    setActiveStop(null)
    setCarT(0)
    rafRef.current = requestAnimationFrame(tick)
  }

  function tick(ts) {
    const s = animRef.current
    const j = journeyRef.current
    if (!s || !j) return

    if (s.mode === 'stopped') {
      if (ts >= s.resumeAt) {
        s.mode = 'playing'
        s.lastTs = ts
        setActiveStop(null)
      }
      rafRef.current = requestAnimationFrame(tick)
      return
    }
    if (s.mode !== 'playing') return

    const dt = ts - s.lastTs
    s.lastTs = ts
    let t2 = s.t + dt * (j.maxEta / DRIVE_MS)

    const nextStop = j.stops[s.stopIdx]
    if (nextStop && t2 >= nextStop.eta_mins) {
      t2 = nextStop.eta_mins
      s.stopIdx += 1
      s.mode = 'stopped'
      s.resumeAt = ts + STOP_PAUSE_MS
      setActiveStop(nextStop)
    }

    s.t = Math.min(t2, j.maxEta)
    setCarT(s.t)

    if (s.t >= j.maxEta - 1e-9 && s.mode === 'playing') {
      s.mode = 'done'
      setJourneyMode('done')
      cancelAnim()
      return
    }
    rafRef.current = requestAnimationFrame(tick)
  }

  // Auto-play once when a scanned route arrives (after fitBounds settles)
  useEffect(() => {
    setJourneyMode('idle')
    setActiveStop(null)
    setCarT(0)
    cancelAnim()
    if (!journey) return
    const t = setTimeout(startJourney, 700)
    return () => { clearTimeout(t); cancelAnim() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [journey])

  const carPos = useMemo(() => {
    if (!journey || !routeCoords?.length) return null
    return carPositionAt(journey, routeCoords, carT)
  }, [journey, routeCoords, carT])

  // ── Map fit ──────────────────────────────────────────────────────────────
  const midpoint = routeCoords?.length
    ? routeCoords[Math.floor(routeCoords.length / 2)]
    : [26.7606, 80.8893]

  const routeBounds = useMemo(() => {
    if (!Array.isArray(routeCoords) || routeCoords.length < 2) return null
    return L.latLngBounds(routeCoords)
  }, [routeCoords])

  useEffect(() => {
    if (!mapRef || !routeBounds || !routeBounds.isValid()) return
    const doFit = () => {
      mapRef.invalidateSize()
      mapRef.fitBounds(routeBounds, { padding: [10, 10], maxZoom: 17, animate: true })
    }
    doFit()
    const t = setTimeout(doFit, 120)
    return () => clearTimeout(t)
  }, [mapRef, routeBounds])

  return (
    <div className="map-container">
      <MapContainer
        center={midpoint}
        zoom={9}
        style={{ height: '320px', width: '100%' }}
        zoomControl
        scrollWheelZoom
        whenCreated={(map) => {
          setMapRef(map)
          if (routeBounds && routeBounds.isValid()) {
            map.fitBounds(routeBounds, { padding: [10, 10], maxZoom: 17, animate: true })
          }
        }}
      >
        <TileLayer
          url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          attribution="CartoDB"
        />

        {Array.isArray(routeCoords) && routeCoords.length > 0 && (
          <Polyline positions={routeCoords} color="#0EA5E9" weight={5} opacity={0.35} />
        )}

        {Array.isArray(routeSegments) &&
          routeSegments.map((seg, idx) => (
            <Polyline
              key={`seg-${idx}`}
              positions={seg.positions}
              color={seg.color}
              weight={7}
              opacity={0.92}
              eventHandlers={{ click: () => openSegmentPopup(seg) }}
            />
          ))}

        {/* Rain stop markers (persist after the animation) */}
        {journey?.stops.map((stop, i) => (
          <Marker
            key={`stop-${i}`}
            position={[stop.lat, stop.lon]}
            icon={stopIcon(stop.color)}
            eventHandlers={{
              click: () => {
                setActiveStop(stop)
                if (typeof onStopDetails === 'function') onStopDetails(stop)
              },
            }}
            zIndexOffset={500}
          />
        ))}

        {/* The car */}
        {carPos && journeyMode !== 'idle' && (
          <Marker position={carPos} icon={carIcon} zIndexOffset={1000} interactive={false} />
        )}

        {activeSeg?.mid && (
          <Popup
            position={[activeSeg.mid.lat, activeSeg.mid.lon]}
            closeButton
            autoClose
            closeOnEscapeKey
            eventHandlers={{ remove: () => setActiveSeg(null) }}
          >
            <div style={{ minWidth: 220 }}>
              <div style={{ fontWeight: 900, marginBottom: 6 }}>
                {activeSeg.locationName || 'Selected location'}
              </div>
              <div style={{ fontSize: 12, opacity: 0.9, marginBottom: 8 }}>
                {activeSeg.mid.lat.toFixed(4)}, {activeSeg.mid.lon.toFixed(4)}
              </div>
              <div style={{ fontWeight: 800 }}>
                {activeSeg.inBounds ? activeSeg.label : 'Unknown (out of radar)'}
              </div>
              <div style={{ fontSize: 12, marginTop: 6 }}>
                Rain: {activeSeg.rain_expected ? 'Yes' : 'No'}
                {activeSeg.eta_mins != null ? ` • ETA ~${Math.round(Number(activeSeg.eta_mins))} min` : ''}
                {activeSeg.dbz != null ? ` • dBZ ${Math.round(Number(activeSeg.dbz))}` : ''}
              </div>
            </div>
          </Popup>
        )}
      </MapContainer>

      {/* Journey chip: what stopped the car */}
      {activeStop && (
        <div className="journey-chip" style={{ '--stop-color': activeStop.color }}>
          <span className="journey-chip__icon" aria-hidden>⛈</span>
          <div className="journey-chip__text">
            <span className="journey-chip__title">
              {activeStop.label} · {toISTClock(activeStop.eta_mins)} IST
            </span>
            <span className="journey-chip__sub">
              You reach this rain ~{Math.round(activeStop.eta_mins)} min into the trip
            </span>
          </div>
          {typeof onStopDetails === 'function' && (
            <button
              type="button"
              className="journey-chip__view"
              onClick={() => onStopDetails(activeStop)}
            >
              View radar
            </button>
          )}
          <button
            type="button"
            className="journey-chip__close"
            onClick={() => setActiveStop(null)}
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      )}

      {/* Journey control */}
      {journey && (journeyMode === 'done' || journeyMode === 'idle') && (
        <button type="button" className="journeyBtn" onClick={startJourney}>
          {journeyMode === 'done' ? '↻ Replay journey' : '▶ Preview journey'}
        </button>
      )}

      <button
        type="button"
        className="zoomRouteBtn"
        onClick={() => {
          if (mapRef && routeBounds && routeBounds.isValid()) {
            mapRef.fitBounds(routeBounds, { padding: [10, 10], maxZoom: 17, animate: true })
          }
        }}
      >
        Zoom to route
      </button>
    </div>
  )
}
