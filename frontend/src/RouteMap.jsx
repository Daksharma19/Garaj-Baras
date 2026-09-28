import { useEffect, useMemo, useRef, useState } from 'react'
import L from './leafletSetup'
import 'leaflet-rotate'
import { MapContainer, Marker, Popup, Polyline, TileLayer } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'
import { useT, tr } from './i18n'
import { FOG_COLORS, fogZones } from './fog'
import { baseTiles } from './mapTiles'

// Localize a backend rain-intensity label for display.
function rainLabelTr(label) {
  const map = {
    'No Rain': 'बारिश नहीं', 'Rain': 'बारिश',
    'Very Light Rain': 'बहुत हल्की बारिश', 'Light Rain': 'हल्की बारिश',
    'Moderate Rain': 'मध्यम बारिश', 'Heavy Rain': 'तेज़ बारिश',
    'Very Heavy Rain': 'बहुत तेज़ बारिश',
  }
  const hi = map[String(label || '').trim()]
  return hi ? tr(label, hi) : label
}

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

const liveIcon = L.divIcon({
  className: 'live-marker-wrap',
  html: '<div class="live-marker"><div class="live-marker__pulse"></div><div class="live-marker__dot"></div></div>',
  iconSize: [36, 36],
  iconAnchor: [18, 18],
})

// Navigation puck: an arrow pointing along the direction of travel. Markers
// stay upright on screen under leaflet-rotate, so `rot` = heading + map bearing.
function navArrowIcon(rot) {
  return L.divIcon({
    className: 'nav-arrow-wrap',
    html: `<div class="nav-arrow" style="transform:rotate(${rot}deg)"><svg viewBox="0 0 40 40" aria-hidden="true"><path d="M20 3 L34 35 L20 27 L6 35 Z"/></svg></div>`,
    iconSize: [44, 44],
    iconAnchor: [22, 22],
  })
}

/** Slice of the route polyline between two along-route distances (km). */
function sliceRoute(routeCoords, cumKm, fromKm, toKm) {
  const out = []
  const lerp = (i, km) => {
    const span = cumKm[i] - cumKm[i - 1] || 1e-9
    const u = Math.max(0, Math.min(1, (km - cumKm[i - 1]) / span))
    return [
      routeCoords[i - 1][0] + u * (routeCoords[i][0] - routeCoords[i - 1][0]),
      routeCoords[i - 1][1] + u * (routeCoords[i][1] - routeCoords[i - 1][1]),
    ]
  }
  for (let i = 1; i < routeCoords.length; i++) {
    if (cumKm[i] < fromKm) continue
    if (!out.length) out.push(lerp(i, fromKm))
    if (cumKm[i] >= toKm) { out.push(lerp(i, toKm)); break }
    out.push(routeCoords[i])
  }
  return out
}

/** Point `km` ahead of (lat, lon) along compass heading `deg`. */
function offsetPoint(lat, lon, deg, km) {
  const r = (deg * Math.PI) / 180
  return [
    lat + (km * Math.cos(r)) / 111.32,
    lon + (km * Math.sin(r)) / (111.32 * Math.cos((lat * Math.PI) / 180)),
  ]
}

function endpointIcon(kind) {
  // kind: 'start' | 'end'
  const label = kind === 'start' ? 'A' : 'B'
  return L.divIcon({
    className: 'endpoint-marker-wrap',
    html: `<div class="endpoint-marker endpoint-marker--${kind}"><span>${label}</span></div>`,
    iconSize: [28, 36],
    iconAnchor: [14, 34],
    popupAnchor: [0, -32],
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
  livePos,          // {lat, lon, heading} while a live journey is running, else null
  fog,              // [{cumKm, visM, level}] from fog.js, or null
  navMode = false,  // full-screen turn-by-turn map (heading-up, follow-me)
  height = '320px', // non-nav map height
  hideStartMarker = false, // approach leg: the start is the user's own position
}) {
  const t = useT()
  const [mapRef, setMapRef] = useState(null)
  const isLive = !!livePos
  const [showRain, setShowRain] = useState(true)
  const [showFog, setShowFog] = useState(true)
  const [headingUp, setHeadingUp] = useState(true)
  const [bearingDeg, setBearingDeg] = useState(0)

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
    if (!journey || isLive) return
    const t = setTimeout(startJourney, 700)
    return () => { clearTimeout(t); cancelAnim() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [journey, isLive])

  // ── Live journey: follow the real GPS marker ─────────────────────────────
  const followRef = useRef(true)
  const [follow, setFollow] = useState(true)

  useEffect(() => {
    if (!mapRef) return
    const onDrag = () => { followRef.current = false; setFollow(false) }
    mapRef.on('dragstart', onDrag)
    return () => { mapRef.off('dragstart', onDrag) }
  }, [mapRef])

  const firstFixRef = useRef(true)

  function followLive(animate = true) {
    if (!mapRef || !livePos) return
    const h = Number(livePos.heading)
    if (!navMode) {
      mapRef.panTo([livePos.lat, livePos.lon], { animate, duration: 0.6 })
      return
    }
    // heading-up: rotate so the travel direction points up the screen
    const target = headingUp && Number.isFinite(h) ? (360 - h) % 360 : 0
    if (typeof mapRef.setBearing === 'function') {
      const cur = mapRef.getBearing()
      const diff = Math.abs(((target - cur + 540) % 360) - 180)
      if (diff > 2) mapRef.setBearing(target)
      setBearingDeg(mapRef.getBearing())
    }
    const z = firstFixRef.current ? 17 : mapRef.getZoom()
    firstFixRef.current = false
    // Google-style framing: the puck sits in the lower third so more road
    // ahead is visible — shift the map centre forward along the heading.
    let center = [livePos.lat, livePos.lon]
    if (headingUp && Number.isFinite(h)) {
      const mPerPx = (156543.03 * Math.cos((livePos.lat * Math.PI) / 180)) / 2 ** z
      const aheadKm = (mPerPx * mapRef.getSize().y * 0.22) / 1000
      center = offsetPoint(livePos.lat, livePos.lon, h, aheadKm)
    }
    mapRef.setView(center, z, { animate, duration: 0.8 })
  }

  useEffect(() => {
    if (!isLive) { followRef.current = true; setFollow(true); firstFixRef.current = true; return }
    if (!mapRef || !followRef.current) return
    followLive(!firstFixRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLive, livePos, mapRef, navMode, headingUp])

  const carPos = useMemo(() => {
    if (!journey || !routeCoords?.length) return null
    return carPositionAt(journey, routeCoords, carT)
  }, [journey, routeCoords, carT])

  // ── Fog overlay: contiguous low-visibility stretches along the route ─────
  const routeCumKm = useMemo(() => {
    if (!Array.isArray(routeCoords) || routeCoords.length < 2) return null
    const cum = [0]
    for (let i = 1; i < routeCoords.length; i++) {
      cum.push(cum[i - 1] + haversineKm(routeCoords[i - 1][0], routeCoords[i - 1][1], routeCoords[i][0], routeCoords[i][1]))
    }
    return cum
  }, [routeCoords])

  const fogLines = useMemo(() => {
    if (!routeCumKm || !Array.isArray(fog) || !fog.length) return []
    return fogZones(fog)
      .map((z) => {
        // a single-point zone still deserves a visible stretch (±0.5 km)
        const short = z.endKm - z.startKm < 0.2
        const a = short ? z.startKm - 0.5 : z.startKm
        const b = short ? z.endKm + 0.5 : z.endKm
        return { ...z, positions: sliceRoute(routeCoords, routeCumKm, Math.max(0, a), b) }
      })
      .filter((z) => z.positions.length >= 2)
  }, [fog, routeCoords, routeCumKm])

  const heading = Number(livePos?.heading)
  const arrowIcon = useMemo(
    () => navArrowIcon(Number.isFinite(heading) ? Math.round((heading + bearingDeg) % 360) : 0),
    [heading, bearingDeg],
  )

  // ── Map fit ──────────────────────────────────────────────────────────────
  const midpoint = routeCoords?.length
    ? routeCoords[Math.floor(routeCoords.length / 2)]
    : [26.7606, 80.8893]

  const routeBounds = useMemo(() => {
    if (!Array.isArray(routeCoords) || routeCoords.length < 2) return null
    return L.latLngBounds(routeCoords)
  }, [routeCoords])

  // The route tab is hidden with display:none while other tabs are active;
  // Leaflet mis-sizes if anything changed while hidden. Re-measure whenever
  // the container becomes visible / resizes.
  useEffect(() => {
    if (!mapRef || typeof ResizeObserver === 'undefined') return
    const el = mapRef.getContainer()
    const ro = new ResizeObserver(() => {
      if (el.offsetWidth > 0) mapRef.invalidateSize()
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [mapRef])

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
    <div className={`map-container${navMode ? ' map-container--nav' : ''}`}>
      <MapContainer
        ref={setMapRef}
        center={midpoint}
        zoom={9}
        style={{ height: navMode ? '100%' : height, width: '100%' }}
        zoomControl={!navMode}
        scrollWheelZoom
        rotate={navMode}
        bearing={0}
        touchRotate={false}
        rotateControl={false}
        shiftKeyRotate={false}
      >
        <TileLayer {...baseTiles(navMode ? 'nav' : 'route')} />

        {Array.isArray(routeCoords) && routeCoords.length > 0 && (
          <>
            {/* Focus: soft glow + bright casing so the route pops off the muted base map */}
            <Polyline positions={routeCoords} color="#60A5FA" weight={navMode ? 26 : 18} opacity={0.16} interactive={false} />
            <Polyline positions={routeCoords} color="#F8FAFC" weight={navMode ? 14 : 10} opacity={0.95} interactive={false} />
            <Polyline positions={routeCoords} color="#2563EB" weight={navMode ? 9 : 6} opacity={1} interactive={false} />
          </>
        )}

        {showRain && Array.isArray(routeSegments) &&
          routeSegments.map((seg, idx) => (
            <Polyline
              key={`seg-${idx}`}
              positions={seg.positions}
              color={seg.color}
              weight={navMode ? 9 : 6}
              opacity={seg.color === '#FFFFFF' ? 0 : 0.92} // clear stretches show the blue route underneath (still tappable)
              eventHandlers={{ click: () => openSegmentPopup(seg) }}
            />
          ))}

        {/* Fog layer: dashed haze over low-visibility stretches */}
        {showFog && fogLines.map((z, i) => (
          <Polyline
            key={`fog-${i}`}
            positions={z.positions}
            color={FOG_COLORS[z.level] || '#CBD5E1'}
            weight={navMode ? 18 : 14}
            opacity={0.45}
            dashArray="2 10"
            lineCap="round"
            interactive={false}
          />
        ))}

        {/* Start / destination markers */}
        {Array.isArray(routeCoords) && routeCoords.length >= 2 && (
          <>
            {!hideStartMarker && (
              <Marker position={routeCoords[0]} icon={endpointIcon('start')} zIndexOffset={400}>
                <Popup>Start</Popup>
              </Marker>
            )}
            <Marker position={routeCoords[routeCoords.length - 1]} icon={endpointIcon('end')} zIndexOffset={400}>
              <Popup>Destination</Popup>
            </Marker>
          </>
        )}

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

        {/* The car (preview animation — hidden during a live journey) */}
        {carPos && journeyMode !== 'idle' && !isLive && (
          <Marker position={carPos} icon={carIcon} zIndexOffset={1000} interactive={false} />
        )}

        {/* Live GPS marker */}
        {isLive && (
          <Marker
            position={[livePos.lat, livePos.lon]}
            icon={navMode ? arrowIcon : liveIcon}
            zIndexOffset={1200}
            interactive={false}
          />
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
                {activeSeg.locationName || t('Selected location', 'चयनित स्थान')}
              </div>
              <div style={{ fontSize: 12, opacity: 0.9, marginBottom: 8 }}>
                {activeSeg.mid.lat.toFixed(4)}, {activeSeg.mid.lon.toFixed(4)}
              </div>
              <div style={{ fontWeight: 800 }}>
                {activeSeg.inBounds ? rainLabelTr(activeSeg.label) : t('Unknown (out of radar)', 'अज्ञात (रडार के बाहर)')}
              </div>
              <div style={{ fontSize: 12, marginTop: 6 }}>
                {t('Rain', 'बारिश')}: {activeSeg.rain_expected ? t('Yes', 'हाँ') : t('No', 'नहीं')}
                {activeSeg.eta_mins != null ? ` • ${t('ETA', 'पहुँच')} ~${Math.round(Number(activeSeg.eta_mins))} ${t('min', 'मिनट')}` : ''}
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
              {rainLabelTr(activeStop.label)} · {toISTClock(activeStop.eta_mins)} {t('IST', 'IST')}
            </span>
            <span className="journey-chip__sub">
              {t(`You reach this rain ~${Math.round(activeStop.eta_mins)} min into the trip`, `आप सफ़र में ~${Math.round(activeStop.eta_mins)} मिनट पर इस बारिश तक पहुँचेंगे`)}
            </span>
          </div>
          {typeof onStopDetails === 'function' && (
            <button
              type="button"
              className="journey-chip__view"
              onClick={() => onStopDetails(activeStop)}
            >
              {t('View radar', 'रडार देखें')}
            </button>
          )}
          <button
            type="button"
            className="journey-chip__close"
            onClick={() => setActiveStop(null)}
            aria-label={t('Dismiss', 'हटाएँ')}
          >
            ×
          </button>
        </div>
      )}

      {/* Layer toggles: forecast rain coloring + fog (+ compass in nav) */}
      <div className={`map-layers${navMode ? ' map-layers--nav' : ''}`}>
        <button
          type="button"
          className={`map-layer-btn${showRain ? ' is-on' : ''}`}
          onClick={() => setShowRain((v) => !v)}
          aria-pressed={showRain}
          title={t('Rain forecast coloring', 'बारिश पूर्वानुमान रंग')}
        >
          🌧 <span>{t('Rain', 'बारिश')}</span>
        </button>
        <button
          type="button"
          className={`map-layer-btn${showFog ? ' is-on' : ''}`}
          onClick={() => setShowFog((v) => !v)}
          aria-pressed={showFog}
          title={t('Fog / low visibility', 'कोहरा / कम दृश्यता')}
        >
          🌫 <span>{t('Fog', 'कोहरा')}</span>
          {fogLines.length > 0 && <i className="map-layer-btn__dot" aria-hidden />}
        </button>
        {navMode && (
          <button
            type="button"
            className="map-layer-btn"
            onClick={() => setHeadingUp((v) => !v)}
            title={headingUp ? t('Switch to north-up', 'उत्तर-ऊपर करें') : t('Switch to heading-up', 'दिशा-ऊपर करें')}
          >
            <span className="compass-needle" style={{ transform: `rotate(${bearingDeg}deg)` }} aria-hidden>▲</span>
            <span>{headingUp ? t('Heading', 'दिशा') : t('North', 'उत्तर')}</span>
          </button>
        )}
      </div>

      {/* Re-center on the live marker after the user pans away */}
      {isLive && !follow && (
        <button
          type="button"
          className={`journeyBtn recenterBtn${navMode ? ' recenterBtn--nav' : ''}`}
          onClick={() => {
            followRef.current = true
            setFollow(true)
            followLive(true)
          }}
        >
          ◎ {t('Re-center', 'फिर केंद्र करें')}
        </button>
      )}

      {/* Journey control */}
      {journey && !isLive && !navMode && (journeyMode === 'done' || journeyMode === 'idle') && (
        <button type="button" className="journeyBtn" onClick={startJourney}>
          {journeyMode === 'done' ? t('↻ Replay journey', '↻ सफ़र दोबारा चलाएँ') : t('▶ Preview journey', '▶ सफ़र का पूर्वावलोकन')}
        </button>
      )}

      {!navMode && (
        <button
          type="button"
          className="zoomRouteBtn"
          onClick={() => {
            if (mapRef && routeBounds && routeBounds.isValid()) {
              mapRef.fitBounds(routeBounds, { padding: [10, 10], maxZoom: 17, animate: true })
            }
          }}
        >
          {t('Zoom to route', 'रास्ते पर ज़ूम करें')}
        </button>
      )}
    </div>
  )
}
