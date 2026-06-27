import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import axios from 'axios'
import './App.css'

const API_BASE =
  import.meta.env.VITE_API_BASE || 'https://garaj-baras-api.onrender.com'
const PREDICT_WAYPOINTS_URL = `${API_BASE}/predict_waypoints`

const NOMINATIM_SEARCH_URL = 'https://nominatim.openstreetmap.org/search'
const NOMINATIM_REVERSE_URL = 'https://nominatim.openstreetmap.org/reverse'

const ORS_KEY = import.meta.env.VITE_ORS_API_KEY

const RouteMap = lazy(() => import('./RouteMap.jsx'))

// Wake the backend (Render free tier sleeps after ~15 min). Fire-and-forget.
function warmBackend() {
  try {
    axios.get(`${API_BASE}/health`, { timeout: 90000 }).catch(() => {})
  } catch {
    // ignore
  }
}

// Retry with exponential backoff on network/timeout/5xx. Render free-tier
// cold-starts can take 30–60s, so we quietly retry up to 4 times before
// surfacing an error to the user.
async function postWithRetry(url, body, config = {}, onAttempt = null) {
  const backoffsMs = [0, 3000, 7000, 15000]
  let lastErr = null
  for (let attempt = 0; attempt < backoffsMs.length; attempt++) {
    if (backoffsMs[attempt] > 0) {
      await new Promise((r) => setTimeout(r, backoffsMs[attempt]))
    }
    if (typeof onAttempt === 'function') {
      try { onAttempt(attempt + 1, backoffsMs.length) } catch {}
    }
    try {
      return await axios.post(url, body, config)
    } catch (err) {
      lastErr = err
      const status = err?.response?.status
      const isTimeout = err?.code === 'ECONNABORTED' || /timeout/i.test(err?.message || '')
      const isServerErr = status && status >= 500
      const isNetwork = !status && !err?.response
      // Only retry on transient errors; surface anything else immediately.
      if (!(isTimeout || isServerErr || isNetwork)) throw err
    }
  }
  throw lastErr
}

function computeViewboxAround(lat, lon, radiusKm = 180) {
  const la = Number(lat)
  const lo = Number(lon)
  if (!Number.isFinite(la) || !Number.isFinite(lo)) return null

  // Very rough: 1° lat ≈ 111 km; lon shrinks by cos(lat).
  const dLat = radiusKm / 111
  const dLon = radiusKm / (111 * Math.max(0.2, Math.cos((la * Math.PI) / 180)))

  const left = lo - dLon
  const right = lo + dLon
  const top = la + dLat
  const bottom = la - dLat
  // Nominatim expects "left,top,right,bottom" (lon,lat,lon,lat)
  return `${left},${top},${right},${bottom}`
}

function getRainColor(label) {
  const l = String(label || '')
  if (l === 'No Rain') return '#FFFFFF'
  if (l.includes('Very Light')) return '#7DD3FC'
  if (l.includes('Light')) return '#38BDF8'
  if (l.includes('Moderate')) return '#0EA5E9'
  if (l.includes('Very Heavy')) return '#EF4444'
  if (l.includes('Heavy')) return '#F59E0B'
  return '#FFFFFF'
}

function getRainGroupLabel(label) {
  const l = String(label || '')
  if (l === 'No Rain') return 'No Rain'
  if (l.includes('Very Light') || l.includes('Light')) return 'Light'
  if (l.includes('Moderate')) return 'Medium'
  if (l.includes('Heavy')) return 'Heavy'
  return 'No Rain'
}

// Collapse a sorted list of waypoints into contiguous rain patches and
// produce a human-friendly summary focused on the patch CLOSEST to the user.
//
// Returns: { tone, headline, secondary, patches, closest, lastEta } or null.
function computeRainTimeline(waypoints) {
  if (!Array.isArray(waypoints) || !waypoints.length) return null

  const sorted = waypoints
    .filter((w) => w && Number.isFinite(Number(w.eta_mins)))
    .slice()
    .sort((a, b) => Number(a.eta_mins) - Number(b.eta_mins))
  if (!sorted.length) return null

  const patches = []
  let start = null
  let last = null
  let patchWps = []
  for (const wp of sorted) {
    const eta = Number(wp.eta_mins)
    if (wp.rain_expected) {
      if (start === null) { start = eta; patchWps = [] }
      last = eta
      patchWps.push(wp)
    } else if (start !== null) {
      const labels = patchWps.map((w) => getRainGroupLabel(w.label))
      const dominant = labels.includes('Heavy') ? 'Heavy' : labels.includes('Medium') ? 'Medium' : 'Light'
      const decayStatuses = patchWps.map((w) => w.decay_status).filter(Boolean)
      const patchDecay = decayStatuses.includes('dead') ? 'dead'
        : decayStatuses.includes('dying') ? 'dying'
        : decayStatuses.includes('weakening') ? 'weakening'
        : 'stable'
      patches.push({ startMin: start, endMin: last, intensity: dominant, decayStatus: patchDecay })
      start = null; last = null; patchWps = []
    }
  }
  if (start !== null) {
    const labels = patchWps.map((w) => getRainGroupLabel(w.label))
    const dominant = labels.includes('Heavy') ? 'Heavy' : labels.includes('Medium') ? 'Medium' : 'Light'
    const decayStatuses = patchWps.map((w) => w.decay_status).filter(Boolean)
    const patchDecay = decayStatuses.includes('dead') ? 'dead'
      : decayStatuses.includes('dying') ? 'dying'
      : decayStatuses.includes('weakening') ? 'weakening'
      : 'stable'
    patches.push({ startMin: start, endMin: last, intensity: dominant, decayStatus: patchDecay })
  }

  const lastEta = Number(sorted[sorted.length - 1].eta_mins) || 0
  const firstEta = Number(sorted[0].eta_mins) || 0

  if (!patches.length) {
    return {
      tone: 'clear',
      headline: 'No rain on route',
      secondary: 'Clear skies expected all the way.',
      patches: [],
      closest: null,
      lastEta,
    }
  }

  const closest = patches[0]
  const NOW_THRESHOLD_MIN = 2
  const END_THRESHOLD_MIN = 2.5

  const isNow = closest.startMin <= firstEta + NOW_THRESHOLD_MIN
  const continuesToEnd = closest.endMin >= lastEta - END_THRESHOLD_MIN

  const fmt = (m) => `${Math.max(0, Math.round(Number(m) || 0))} min`

  const closestDecay = closest.decayStatus || 'stable'
  const isDying = closestDecay === 'dying' || closestDecay === 'dead'
  const isWeakening = closestDecay === 'weakening'

  let headline
  let secondary
  let decayNote = null  // shown as a sub-note when patch is fading

  if (isDying) {
    headline = isNow
      ? 'Rain nearby — but it\'s fading fast'
      : `Rain detected in ${fmt(closest.startMin)} — likely to clear`
    secondary = 'This patch is losing intensity. By the time you reach it, skies may already be clearing.'
    decayNote = 'dying'
  } else if (isWeakening) {
    if (isNow) {
      headline = 'Light rain right now — weakening as you travel'
      secondary = 'Rain is losing strength. Expect it to get lighter along your route.'
    } else {
      headline = `Rain in ${fmt(closest.startMin)} — and it\'s weakening`
      secondary = 'This patch is losing intensity. Rain will likely be lighter than current radar shows.'
    }
    decayNote = 'weakening'
  } else {
    // stable / growing — normal messaging
    if (isNow) {
      if (continuesToEnd) {
        headline = 'Rain right now — continues to destination'
        secondary = `Expect rain for the full ${fmt(lastEta)} trip.`
      } else {
        headline = `Rain right now — clearing in ${fmt(closest.endMin)}`
        secondary = 'After that, skies clear for the rest of the route.'
      }
    } else {
      if (continuesToEnd) {
        headline = `Rain starts in ${fmt(closest.startMin)}`
        secondary = 'Once it starts, rain continues to your destination.'
      } else {
        const duration = Math.max(1, Math.round(closest.endMin - closest.startMin))
        headline = `Rain starts in ${fmt(closest.startMin)}, clearing in ${fmt(closest.endMin)}`
        secondary = `Rainy stretch ~${duration} min.`
      }
    }
  }

  return { tone: 'rain', headline, secondary, decayNote, patches, closest, lastEta }
}

function haversine(lat1, lon1, lat2, lon2) {
  const R = 6371
  const dLat = ((lat2 - lat1) * Math.PI) / 180
  const dLon = ((lon2 - lon1) * Math.PI) / 180
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) ** 2
  return R * 2 * Math.asin(Math.sqrt(a))
}

function toShortCityName(name) {
  const s = (name ?? '').trim()
  if (!s) return ''
  return s.split(',')[0].trim()
}

function toCityRouteName(a, b) {
  const left = toShortCityName(a) || 'Source'
  const right = toShortCityName(b) || 'Destination'
  return `${left} → ${right}`
}

async function geocode(place) {
  const q = String(place ?? '').trim()
  if (!q) throw new Error('Please enter both Source and Destination.')

  const url = `${NOMINATIM_SEARCH_URL}?q=${encodeURIComponent(
    q
  )}&format=json&limit=1&countrycodes=in`

  const res = await axios.get(url, {
    timeout: 15000,
    headers: { Accept: 'application/json' },
  })
  const data = Array.isArray(res.data) ? res.data[0] : null
  if (!data?.lat || !data?.lon) throw new Error('No geocoding results.')
  return {
    lat: parseFloat(data.lat),
    lon: parseFloat(data.lon),
    display_name: data.display_name,
  }
}

async function searchPlaces(query, signal, opts = {}) {
  const q = String(query ?? '').trim()
  if (!q) return []

  const viewbox = opts?.viewbox || null

  const res = await axios.get(NOMINATIM_SEARCH_URL, {
    timeout: 15000,
    signal,
    params: {
      q,
      format: 'jsonv2',
      limit: 6,
      addressdetails: 1,
      // Do NOT hard-restrict to a region. If we have a viewbox (e.g. user's
      // current location), use it as a soft ranking hint.
      ...(viewbox ? { viewbox, bounded: 0 } : {}),
      countrycodes: 'in',
    },
    headers: { Accept: 'application/json' },
  })

  const arr = Array.isArray(res.data) ? res.data : []
  return arr
    .filter((x) => x?.lat && x?.lon && x?.display_name)
    .map((x) => ({
      id: String(x.place_id ?? x.osm_id ?? x.display_name),
      display_name: String(x.display_name),
      lat: Number(x.lat),
      lon: Number(x.lon),
      type: x.type ? String(x.type) : '',
    }))
    .filter((x) => Number.isFinite(x.lat) && Number.isFinite(x.lon))
}

async function reversePlaceName(lat, lon, signal) {
  const res = await axios.get(NOMINATIM_REVERSE_URL, {
    timeout: 15000,
    signal,
    params: {
      lat,
      lon,
      format: 'jsonv2',
      zoom: 16,
      addressdetails: 1,
    },
    headers: { Accept: 'application/json' },
  })
  const name = res.data?.display_name
  return typeof name === 'string' && name.trim() ? name.trim() : null
}

function sampleRouteEvery2km(routeCoords, stepKm = 2.0) {
  // routeCoords: Array of [lon, lat]
  if (!Array.isArray(routeCoords) || routeCoords.length < 2) return []

  const sampled = []
  sampled.push(routeCoords[0])

  let distAcc = 0
  for (let i = 1; i < routeCoords.length; i++) {
    const [lon1, lat1] = routeCoords[i - 1]
    const [lon2, lat2] = routeCoords[i]
    distAcc += haversine(lat1, lon1, lat2, lon2)
    if (distAcc >= stepKm) {
      sampled.push(routeCoords[i])
      distAcc = 0
    }
  }

  const last = routeCoords[routeCoords.length - 1]
  const lastKey = `${last[0]}|${last[1]}`
  const lastSampled = sampled[sampled.length - 1]
  const lastSampledKey = `${lastSampled[0]}|${lastSampled[1]}`
  if (lastKey !== lastSampledKey) sampled.push(last)

  // Convert to {lat,lon} objects
  return sampled.map(([lon, lat]) => ({ lat, lon }))
}

function sampleRouteEvery5Min(routeCoords, speedKmh, intervalMin = 5) {
  // routeCoords: Array of [lon, lat]
  if (!Array.isArray(routeCoords) || routeCoords.length < 2) return []
  const v = Number(speedKmh)
  if (!Number.isFinite(v) || v <= 0) return []

  const stepKm = v * (intervalMin / 60)
  const sampled = []

  let cumKm = 0
  let distAcc = 0

  // always start
  const [lon0, lat0] = routeCoords[0]
  sampled.push({ lat: lat0, lon: lon0, eta_mins: 0, cumKm: 0 })

  for (let i = 1; i < routeCoords.length; i++) {
    const [lon1, lat1] = routeCoords[i - 1]
    const [lon2, lat2] = routeCoords[i]
    const dKm = haversine(lat1, lon1, lat2, lon2)
    cumKm += dKm
    distAcc += dKm

    if (distAcc >= stepKm) {
      const eta = (cumKm / v) * 60
      sampled.push({ lat: lat2, lon: lon2, eta_mins: eta, cumKm })
      distAcc = 0
    }
  }

  const [lonLast, latLast] = routeCoords[routeCoords.length - 1]
  const last = sampled[sampled.length - 1]
  if (!last || Math.abs(last.lat - latLast) > 1e-9 || Math.abs(last.lon - lonLast) > 1e-9) {
    const eta = (cumKm / v) * 60
    sampled.push({ lat: latLast, lon: lonLast, eta_mins: eta, cumKm })
  }

  return sampled
}

function binarySearchNearestIndex(sortedNums, target) {
  if (!Array.isArray(sortedNums) || !sortedNums.length) return -1
  let lo = 0
  let hi = sortedNums.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const v = sortedNums[mid]
    if (v === target) return mid
    if (v < target) lo = mid + 1
    else hi = mid - 1
  }
  if (lo <= 0) return 0
  if (lo >= sortedNums.length) return sortedNums.length - 1
  const a = sortedNums[lo - 1]
  const b = sortedNums[lo]
  return Math.abs(a - target) <= Math.abs(b - target) ? lo - 1 : lo
}

function buildColoredSegments(routeLonLat, predictedWaypoints) {
  // routeLonLat: Array<[lon,lat]>
  if (!Array.isArray(routeLonLat) || routeLonLat.length < 2) return []
  if (!Array.isArray(predictedWaypoints) || !predictedWaypoints.length) return []

  const cumKm = [0]
  let acc = 0
  for (let i = 1; i < routeLonLat.length; i++) {
    const [lon1, lat1] = routeLonLat[i - 1]
    const [lon2, lat2] = routeLonLat[i]
    acc += haversine(lat1, lon1, lat2, lon2)
    cumKm.push(acc)
  }

  const wpEta = predictedWaypoints.map((w) => Number(w?.eta_mins || 0))
  const maxEta = Math.max(...wpEta, 0)
  const totalKm = cumKm[cumKm.length - 1] || 1e-6
  const wpKm = predictedWaypoints.map((w) => {
    const eta = Number(w?.eta_mins || 0)
    const frac = maxEta > 0 ? Math.max(0, Math.min(1, eta / maxEta)) : 0
    return frac * totalKm
  })

  const segments = []
  for (let i = 1; i < routeLonLat.length; i++) {
    const midKm = (cumKm[i - 1] + cumKm[i]) / 2
    const idx = binarySearchNearestIndex(wpKm, midKm)
    const wp = idx >= 0 ? predictedWaypoints[idx] : null
    const inBounds = !!wp?.in_radar_bounds
    const label = wp?.label || 'Unknown'
    const color = inBounds ? getRainColor(label) : '#64748B'
    const midLat = (routeLonLat[i - 1][1] + routeLonLat[i][1]) / 2
    const midLon = (routeLonLat[i - 1][0] + routeLonLat[i][0]) / 2
    segments.push({
      positions: [
        [routeLonLat[i - 1][1], routeLonLat[i - 1][0]],
        [routeLonLat[i][1], routeLonLat[i][0]],
      ],
      color,
      inBounds,
      label,
      eta_mins: wp?.eta_mins ?? null,
      dbz: wp?.dbz ?? null,
      rain_expected: !!wp?.rain_expected,
      mid: { lat: midLat, lon: midLon },
    })
  }
  return segments
}

function joinApiUrl(maybePath) {
  const p = String(maybePath || '')
  if (!p) return ''
  if (p.startsWith('http://') || p.startsWith('https://')) return p
  if (p.startsWith('/')) return `${API_BASE}${p}`
  return `${API_BASE}/${p}`
}

function toIST(etaMins) {
  const ms = Date.now() + etaMins * 60 * 1000
  const ist = new Date(ms + 5.5 * 60 * 60 * 1000)
  const h = String(ist.getUTCHours()).padStart(2, '0')
  const m = String(ist.getUTCMinutes()).padStart(2, '0')
  return `${h}:${m}`
}

function RainTimelineBar({ patches, lastEta, showBreakdown, onToggleBreakdown }) {
  if (!patches?.length || !lastEta || lastEta <= 0) return null
  const first = patches[0]
  const startIST = toIST(first.startMin)
  const endIST = toIST(first.endMin)
  const duration = Math.round(first.endMin - first.startMin)
  const firstLabel = duration <= 1 ? `Rain at ${startIST}` : `${startIST} – ${endIST}`

  return (
    <div className="routeTimeline">
      <div className="routeTimelineTrack">
        <div className="routeStartDot" />
        {patches.map((p, i) => {
          const left = Math.max(0, (p.startMin / lastEta) * 100)
          const width = Math.max(3, Math.min(100 - left, ((p.endMin - p.startMin) / lastEta) * 100))
          return (
            <div
              key={i}
              className="routeRainPatch"
              style={{ left: `${left}%`, width: `${width}%` }}
            />
          )
        })}
        <div className="routeEndDot" />
      </div>
      <div className="routeTimelineMeta">
        <span>{toIST(0)}</span>
        <span className="routeTimelineNote">{firstLabel}</span>
        <span>{toIST(lastEta)}</span>
      </div>

      <button className="breakdownToggle" type="button" onClick={onToggleBreakdown}>
        {showBreakdown ? 'Hide breakdown' : `See full breakdown (${patches.length} rain ${patches.length === 1 ? 'zone' : 'zones'})`}
      </button>

      {showBreakdown && (
        <div className="rainBreakdown">
          {patches.map((p, i) => {
            const decay = p.decayStatus || 'stable'
            const decayLabel = decay === 'dead' ? 'Likely clear'
              : decay === 'dying' ? 'Fading fast'
              : decay === 'weakening' ? 'Weakening'
              : null
            return (
              <div key={i} className={`rainBreakdownRow ${decay !== 'stable' ? 'rainBreakdownRow--fading' : ''}`}>
                <span className={`breakdownDot breakdownDot--${(p.intensity || 'Light').toLowerCase()}`} />
                <div className="breakdownMain">
                  <span className="breakdownTime">{toIST(p.startMin)} – {toIST(p.endMin)}</span>
                  <span className="breakdownIntensity">{p.intensity || 'Light'} Rain</span>
                </div>
                {decayLabel && (
                  <span className={`decayChip decayChip--${decay}`}>{decayLabel}</span>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}


export default function App() {
  const [source, setSource] = useState('')
  const [destination, setDestination] = useState('')
  const [avgSpeedKmh, setAvgSpeedKmh] = useState('')
  const [sourcePlace, setSourcePlace] = useState(null) // {lat,lon,display_name}
  const [destPlace, setDestPlace] = useState(null) // {lat,lon,display_name}
  const [userLoc, setUserLoc] = useState(null) // {lat,lon} for soft place-search bias

  const [sourceSug, setSourceSug] = useState([])
  const [destSug, setDestSug] = useState([])
  const [sourceOpen, setSourceOpen] = useState(false)
  const [destOpen, setDestOpen] = useState(false)
  const sourceDebounceRef = useRef(null)
  const destDebounceRef = useRef(null)
  const sourceAbortRef = useRef(null)
  const destAbortRef = useRef(null)

  const [loading, setLoading] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [scanStatus, setScanStatus] = useState('')
  const [result, setResult] = useState(null)
  const [routeCoords, setRouteCoords] = useState([]) // [lat, lon] full road polyline
  const [routeSegments, setRouteSegments] = useState([]) // colored line segments
  const [activeSeg, setActiveSeg] = useState(null) // {lat,lon,label,dbz,eta_mins,rain_expected,inBounds,locationName}
  const [routeDistanceKm, setRouteDistanceKm] = useState(null)
  const [showBreakdown, setShowBreakdown] = useState(false)
  const [error, setError] = useState(null)

  const reverseAbortRef = useRef(null)
  const reverseCacheRef = useRef(new Map())

  const routeName = useMemo(
    () => toCityRouteName(source, destination),
    [source, destination]
  )

  // Warm the backend once on mount so the first click doesn't pay the
  // Render free-tier cold-start cost (which was causing the "open-after-a-while"
  // timeouts that the welcome popup alludes to).
  useEffect(() => {
    warmBackend()
  }, [])

  // Soft-bias search suggestions near the user's current location.
  useEffect(() => {
    let alive = true
    try {
      if (!('geolocation' in navigator)) return
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          if (!alive) return
          const lat = Number(pos?.coords?.latitude)
          const lon = Number(pos?.coords?.longitude)
          if (!Number.isFinite(lat) || !Number.isFinite(lon)) return
          setUserLoc({ lat, lon })
        },
        () => {
          // ignore (permission denied / unavailable)
        },
        { enableHighAccuracy: false, timeout: 6000, maximumAge: 5 * 60 * 1000 }
      )
    } catch {
      // ignore
    }
    return () => {
      alive = false
    }
  }, [])

  // Autocomplete: Source
  useEffect(() => {
    const q = String(source || '').trim()
    if (sourceAbortRef.current) sourceAbortRef.current.abort()
    if (sourceDebounceRef.current) clearTimeout(sourceDebounceRef.current)

    if (q.length < 3) {
      setSourceSug([])
      return
    }

    sourceDebounceRef.current = setTimeout(async () => {
      const ac = new AbortController()
      sourceAbortRef.current = ac
      try {
        const viewbox = userLoc ? computeViewboxAround(userLoc.lat, userLoc.lon, 220) : null
        const items = await searchPlaces(q, ac.signal, { viewbox })
        setSourceSug(items)
      } catch (e) {
        if (e?.name === 'CanceledError' || e?.name === 'AbortError') return
        setSourceSug([])
      }
    }, 350)

    return () => {
      if (sourceDebounceRef.current) clearTimeout(sourceDebounceRef.current)
    }
  }, [source, userLoc])

  // Autocomplete: Destination
  useEffect(() => {
    const q = String(destination || '').trim()
    if (destAbortRef.current) destAbortRef.current.abort()
    if (destDebounceRef.current) clearTimeout(destDebounceRef.current)

    if (q.length < 3) {
      setDestSug([])
      return
    }

    destDebounceRef.current = setTimeout(async () => {
      const ac = new AbortController()
      destAbortRef.current = ac
      try {
        const viewbox = userLoc ? computeViewboxAround(userLoc.lat, userLoc.lon, 220) : null
        const items = await searchPlaces(q, ac.signal, { viewbox })
        setDestSug(items)
      } catch (e) {
        if (e?.name === 'CanceledError' || e?.name === 'AbortError') return
        setDestSug([])
      }
    }, 350)

    return () => {
      if (destDebounceRef.current) clearTimeout(destDebounceRef.current)
    }
  }, [destination, userLoc])

  async function handlePredict() {
    const startCity = source.trim()
    const endCity = destination.trim()
    const speedNum = Number(avgSpeedKmh)

    if (!startCity || !endCity) {
      setError('Please enter both Source and Destination city names.')
      return
    }
    if (!Number.isFinite(speedNum) || speedNum <= 0) {
      setError('Please enter a valid average speed (km/h).')
      return
    }
    if (!ORS_KEY) {
      setError('Missing ORS API key. Set `VITE_ORS_API_KEY` in frontend/.env.')
      return
    }

    setError(null)
    setLoading(true)
    setResult(null)
    setRouteCoords([])
    setRouteSegments([])
    setRouteDistanceKm(null)
    setScanning(false)
    setScanStatus('')

    // Kick a health ping in parallel to wake Render (harmless if already warm).
    warmBackend()

    try {
      // 1) Geocode source + destination in parallel (saves ~200-1000ms).
      const [start, end] = await Promise.all([
        sourcePlace ? Promise.resolve(sourcePlace) : geocode(startCity),
        destPlace ? Promise.resolve(destPlace) : geocode(endCity),
      ])

      // 2) Get road route from ORS
      let routeLonLat = null
      try {
        const orsRes = await axios.post(
          'https://api.openrouteservice.org/v2/directions/driving-car/geojson',
          {
            coordinates: [
              [start.lon, start.lat],
              [end.lon, end.lat],
            ],
            // Helps when geocoded points are slightly away from roads.
            radiuses: [5000, 5000],
          },
          {
            timeout: 90000,
            headers: {
              Authorization: ORS_KEY,
              'Content-Type': 'application/json',
            },
          }
        )
        routeLonLat = orsRes.data?.features?.[0]?.geometry?.coordinates || null
      } catch (orsError) {
        // ORS can return 404 "Could not find routable point..." for city centers.
        // Fallback to a straight segment so prediction can still proceed.
        routeLonLat = [
          [start.lon, start.lat],
          [end.lon, end.lat],
        ]
      }

      if (!Array.isArray(routeLonLat) || routeLonLat.length < 2) {
        throw new Error('Route planning failed (ORS returned empty geometry).')
      }

      // Total distance from the actual road geometry (used for UI stats)
      let totalKm = 0
      for (let i = 1; i < routeLonLat.length; i++) {
        const [lon1, lat1] = routeLonLat[i - 1]
        const [lon2, lat2] = routeLonLat[i]
        totalKm += haversine(lat1, lon1, lat2, lon2)
      }
      setRouteDistanceKm(totalKm)

      // 3) Sample waypoints every 5 minutes by constant avg speed
      const sampled = sampleRouteEvery5Min(routeLonLat, speedNum, 5)
      if (!sampled.length) {
        throw new Error('Could not sample route into waypoints. Try a different route or speed.')
      }

      // 4) PHASE 2 — show the map + route polyline immediately, then scan radar
      // in the background so the user doesn't stare at a blank spinner.
      const polylineLatLon = routeLonLat.map(([lon, lat]) => [lat, lon])
      setRouteCoords(polylineLatLon)
      setResult({
        total_waypoints: sampled.length,
        rain_waypoints: 0,
        clear_waypoints: sampled.length,
        first_rain_eta: null,
        first_rain_label: null,
        rain_direction_from: '—',
        rain_direction_to: '—',
        rain_speed_kmh: 0,
        radar_lag_mins: null,
        radar_freshness: 'pending',
        radar_message: 'Scanning radar…',
        route_distance_km: totalKm,
        waypoints: [],
        _pending: true,
      })
      setLoading(false)
      setScanning(true)

      // 5) PHASE 3 — call predict-waypoints API (retry once on cold-start).
      const cumWaypoints = sampled.map(({ lat, lon, eta_mins }) => ({
        lat,
        lon,
        eta_mins,
      }))

      const predictRes = await postWithRetry(
        PREDICT_WAYPOINTS_URL,
        { waypoints: cumWaypoints },
        { timeout: 90000 },
        (attempt, total) => {
          if (attempt === 1) {
            setScanStatus('Scanning radar…')
          } else {
            setScanStatus(
              `Waking up server — attempt ${attempt} of ${total} (first load can take up to a minute)`
            )
          }
        }
      )

      const predictWaypoints = Array.isArray(predictRes.data?.waypoints)
        ? predictRes.data.waypoints
        : []

      const mergedWaypoints = predictWaypoints.map((wp) => ({
        ...wp,
        rainGroup: getRainGroupLabel(wp.label),
        rainColor: getRainColor(wp.label),
      }))

      const segments = buildColoredSegments(routeLonLat, mergedWaypoints)
      setRouteSegments(segments)

      setResult({
        ...predictRes.data,
        route_distance_km: totalKm,
        waypoints: mergedWaypoints,
      })
    } catch (e) {
      const status = e?.response?.status
      const detail =
        e?.response?.data?.detail ||
        e?.response?.data?.error?.message ||
        e?.response?.data?.message
      const isTimeout = e?.code === 'ECONNABORTED' || /timeout/i.test(e?.message || '')
      const isNetwork = !status && !e?.response
      let msg
      if (isTimeout || isNetwork) {
        msg = "Couldn't reach the radar server. It may still be waking up — please try again in a minute."
      } else {
        msg = status
          ? `Request failed (${status}): ${detail || e?.message || 'Unknown error'}`
          : e?.message || 'Something went wrong while scanning the radar.'
      }
      setError(typeof msg === 'string' ? msg : 'Something went wrong.')
      // Roll back the optimistic Phase-2 result so we return to the planner.
      setResult(null)
      setRouteCoords([])
      setRouteSegments([])
      setRouteDistanceKm(null)
    } finally {
      setLoading(false)
      setScanning(false)
      setScanStatus('')
    }
  }

  async function openSegmentPopup(seg) {
    if (!seg?.mid) return
    const lat = Number(seg.mid.lat)
    const lon = Number(seg.mid.lon)
    const key = `${lat.toFixed(4)},${lon.toFixed(4)}`

    setActiveSeg({
      ...seg,
      locationName: reverseCacheRef.current.get(key) || null,
    })

    if (reverseCacheRef.current.has(key)) return

    if (reverseAbortRef.current) reverseAbortRef.current.abort()
    const ac = new AbortController()
    reverseAbortRef.current = ac
    try {
      const name = await reversePlaceName(lat, lon, ac.signal)
      if (name) reverseCacheRef.current.set(key, name)
      setActiveSeg((prev) => {
        if (!prev) return prev
        return { ...prev, locationName: name }
      })
    } catch (e) {
      // ignore abort/network errors; popup will still show lat/lon + intensity
    }
  }

  const hasRain = !!result && (result.rain_waypoints ?? 0) > 0
  const shownDistanceKm = Number.isFinite(Number(routeDistanceKm))
    ? Number(routeDistanceKm)
    : Number.isFinite(Number(result?.route_distance_km))
      ? Number(result.route_distance_km)
      : null

  // Compute the rain timeline (patches + headline) only for the finalized
  // predict response. During the optimistic Phase-2 "_pending" window we
  // don't have real waypoints yet, so skip.
  const rainTimeline = useMemo(() => {
    if (!result || result._pending) return null
    return computeRainTimeline(result.waypoints)
  }, [result])

  function handleBackToPlanner() {
    setResult(null)
    setError(null)
    setActiveSeg(null)
    setRouteCoords([])
    setRouteSegments([])
    setRouteDistanceKm(null)
    setShowBreakdown(false)
  }

  return (
    <div className="app">
      <header className="topbar">
        <div style={{ width: 36 }} />
        <div className="brandTitle">GARAJ BARAS</div>
        <div style={{ width: 36 }} />
      </header>

      <div className="screen">
        <>
        {/* Hero + Inputs */}
        {!loading && !result && (
          <>
            <section className="heroBlock">
              <h1 className="heroTitle">Where to next?</h1>
              <div className="heroSubtitle">Real-time rain prediction on your route</div>
            </section>

            <section className="card plannerCard">
              <div className="fieldGrid">
                <div className="field">
                  <div className="fieldLabel">Source</div>
                  <div className="typeaheadWrap">
                    <div className="inputShell">
                      <span className="glowDot glowDot--source" aria-hidden="true" />
                      <input
                        className="routeInput"
                        placeholder="Starting point"
                        value={source}
                        onChange={(e) => {
                          setSource(e.target.value)
                          setSourcePlace(null)
                          setSourceOpen(true)
                        }}
                        onFocus={() => setSourceOpen(true)}
                        onBlur={() => {
                          // allow click selection to register
                          setTimeout(() => setSourceOpen(false), 140)
                        }}
                      />
                    </div>

                    {sourceOpen && sourceSug.length > 0 && (
                      <div className="typeaheadMenu" role="listbox">
                        {sourceSug.map((it) => (
                          <button
                            key={it.id}
                            type="button"
                            className="typeaheadItem"
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => {
                              setSource(it.display_name)
                              setSourcePlace(it)
                              setSourceSug([])
                              setSourceOpen(false)
                            }}
                          >
                            <div className="typeaheadPrimary">{it.display_name}</div>
                            {it.type ? <div className="typeaheadSecondary">{it.type}</div> : null}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>

                <div className="arrowBetween" aria-hidden="true">
                  →
                </div>

                <div className="field">
                  <div className="fieldLabel">Destination</div>
                  <div className="typeaheadWrap">
                    <div className="inputShell">
                      <span className="glowDot glowDot--destination" aria-hidden="true" />
                      <input
                        className="routeInput"
                        placeholder="Destination"
                        value={destination}
                        onChange={(e) => {
                          setDestination(e.target.value)
                          setDestPlace(null)
                          setDestOpen(true)
                        }}
                        onFocus={() => setDestOpen(true)}
                        onBlur={() => {
                          setTimeout(() => setDestOpen(false), 140)
                        }}
                      />
                    </div>

                    {destOpen && destSug.length > 0 && (
                      <div className="typeaheadMenu" role="listbox">
                        {destSug.map((it) => (
                          <button
                            key={it.id}
                            type="button"
                            className="typeaheadItem"
                            onMouseDown={(e) => e.preventDefault()}
                            onClick={() => {
                              setDestination(it.display_name)
                              setDestPlace(it)
                              setDestSug([])
                              setDestOpen(false)
                            }}
                          >
                            <div className="typeaheadPrimary">{it.display_name}</div>
                            {it.type ? <div className="typeaheadSecondary">{it.type}</div> : null}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              <div className="speedRow">
                <div className="field">
                  <div className="fieldLabel">Avg speed (km/h)</div>
                  <div className="inputShell">
                    <input
                      className="routeInput"
                      inputMode="decimal"
                      placeholder="e.g., 55"
                      value={avgSpeedKmh}
                      onChange={(e) => setAvgSpeedKmh(e.target.value)}
                    />
                  </div>
                </div>
              </div>

              <button
                className="planButton"
                type="button"
                onClick={handlePredict}
                disabled={!source.trim() || !destination.trim() || !String(avgSpeedKmh).trim() || loading}
                aria-label="Plan My Route"
              >
                Plan My Route →
              </button>

              <div className="dividerMini" />
            </section>

            {error && (
              <section className="errorCard" role="alert" aria-live="polite">
                <div className="errorTitle">Scan failed</div>
                <div className="errorBody">{error}</div>
              </section>
            )}
          </>
        )}

        {/* Loading */}
        {loading && (
          <section className="card loadingCard" aria-live="polite">
            <div className="spinner" aria-hidden="true" />
            <div className="loadingText">SCANNING RADAR...</div>
          </section>
        )}

        {/* Results */}
        {result && (
          <section className="resultsWrap">
            <section className="card resultsCard">
              <div className="resultsHeader">
                <button
                  type="button"
                  className="backBtn"
                  onClick={handleBackToPlanner}
                >
                  ← Back
                </button>
                <div
                  className={`statusPill ${
                    result._pending
                      ? 'statusPill--pending'
                      : hasRain
                        ? 'statusPill--rain'
                        : 'statusPill--clear'
                  }`}
                >
                  {result._pending ? 'Scanning…' : hasRain ? 'Rain ahead' : 'Clear skies'}
                </div>
              </div>
              <div className="resultsRouteTitle">{routeName}</div>

              {/* Rain timeline banner: primary narrative focused on the
                  rain patch closest to the user. Appears as soon as the
                  predict response lands (skipped during _pending). */}
              {rainTimeline && (
                <div
                  className={`rainBanner ${
                    rainTimeline.tone === 'rain'
                      ? rainTimeline.decayNote === 'dying' ? 'rainBanner--dying'
                        : rainTimeline.decayNote === 'weakening' ? 'rainBanner--weakening'
                        : 'rainBanner--rain'
                      : 'rainBanner--clear'
                  }`}
                  role="status"
                >
                  {rainTimeline.decayNote && (
                    <div className={`decayNoteBadge decayNoteBadge--${rainTimeline.decayNote}`}>
                      {rainTimeline.decayNote === 'dying' ? 'Patch fading' : 'Weakening'}
                    </div>
                  )}
                  <div className="rainBannerHead">{rainTimeline.headline}</div>
                  {rainTimeline.secondary && (
                    <div className="rainBannerSub">{rainTimeline.secondary}</div>
                  )}
                </div>
              )}

              {rainTimeline?.tone === 'rain' && !result._pending && (
                <RainTimelineBar
                  patches={rainTimeline.patches}
                  lastEta={rainTimeline.lastEta}
                  showBreakdown={showBreakdown}
                  onToggleBreakdown={() => setShowBreakdown((s) => !s)}
                />
              )}

              <div className="statsRow">
                <div className="statBox">
                  <div className="statLabel">Distance</div>
                  <div className="statValue">
                    {shownDistanceKm == null ? '—' : `${shownDistanceKm.toFixed(1)} km`}
                  </div>
                </div>
                <div className="statBox">
                  {(() => {
                    // Adaptive middle stat:
                    //  - rain right now + ends on route => "Rain ends" Xm
                    //  - rain right now + continues to end => "Rain duration" route length
                    //  - rain upcoming => "Rain starts" Xm
                    //  - no rain => "Rain" —
                    if (result._pending) {
                      return (
                        <>
                          <div className="statLabel">Rain status</div>
                          <div className="statValue">—</div>
                        </>
                      )
                    }
                    const tl = rainTimeline
                    if (!tl || tl.tone === 'clear' || !tl.closest) {
                      return (
                        <>
                          <div className="statLabel">Rain</div>
                          <div className="statValue">None</div>
                        </>
                      )
                    }
                    const firstEta = Number(tl.closest.startMin) || 0
                    const lastEta = Number(tl.lastEta) || 0
                    const isNow = firstEta <= 2
                    const continuesToEnd = tl.closest.endMin >= lastEta - 2.5
                    if (isNow && continuesToEnd) {
                      return (
                        <>
                          <div className="statLabel">Rain duration</div>
                          <div className="statValue">{Math.round(lastEta)} min</div>
                        </>
                      )
                    }
                    if (isNow) {
                      return (
                        <>
                          <div className="statLabel">Rain ends</div>
                          <div className="statValue">
                            {Math.round(tl.closest.endMin)} min
                          </div>
                        </>
                      )
                    }
                    return (
                      <>
                        <div className="statLabel">Rain starts</div>
                        <div className="statValue">
                          {Math.round(firstEta)} min
                        </div>
                      </>
                    )
                  })()}
                </div>
              </div>

{/* Map (rendered as soon as the route polyline is ready,
                  even before the rain predict response lands) */}
              <div style={{ position: 'relative' }}>
                <Suspense
                  fallback={
                    <div className="map-container">
                      <div style={{ height: 320, display: 'grid', placeItems: 'center' }}>
                        Loading map…
                      </div>
                    </div>
                  }
                >
                  <RouteMap
                    routeCoords={routeCoords}
                    routeSegments={routeSegments}
                    activeSeg={activeSeg}
                    setActiveSeg={setActiveSeg}
                    openSegmentPopup={openSegmentPopup}
                  />
                </Suspense>
                {scanning && (
                  <div
                    className="scanOverlay"
                    role="status"
                    aria-live="polite"
                    style={{
                      position: 'absolute',
                      inset: 0,
                      display: 'grid',
                      placeItems: 'center',
                      background: 'rgba(8, 12, 24, 0.55)',
                      backdropFilter: 'blur(2px)',
                      borderRadius: 12,
                      pointerEvents: 'none',
                      zIndex: 500,
                      padding: 16,
                      textAlign: 'center',
                    }}
                  >
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        <span className="spinner" aria-hidden="true" />
                        <span style={{ fontWeight: 800, letterSpacing: 1 }}>
                          SCANNING RADAR…
                        </span>
                      </div>
                      {scanStatus && scanStatus !== 'Scanning radar…' && (
                        <div style={{ fontSize: 12, opacity: 0.85, maxWidth: 320 }}>
                          {scanStatus}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>

              <div className="legendWrap">
                <div className="legendTitle">Route colors</div>
                <div className="legendChips">
                  {[
                    { cls: 'veryheavy', label: 'Very Heavy' },
                    { cls: 'heavy',     label: 'Heavy' },
                    { cls: 'moderate',  label: 'Moderate' },
                    { cls: 'light',     label: 'Light' },
                    { cls: 'verylight', label: 'Very Light' },
                    { cls: 'norain',    label: 'No Rain' },
                    { cls: 'unknown',   label: 'Out of radar' },
                  ].map(({ cls, label }) => (
                    <div key={cls} className="legendChip">
                      <span className={`legendSwatch legendSwatch--${cls}`} />
                      <span className="legendText">{label}</span>
                    </div>
                  ))}
                </div>
              </div>
            </section>
          </section>
        )}
        </>
      </div>
    </div>
  )
}
