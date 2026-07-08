import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import axios from 'axios'
import './App.css'

const API_BASE = import.meta.env.DEV
  ? 'http://127.0.0.1:8000'
  : (import.meta.env.VITE_API_BASE || 'https://garaj-baras-api.onrender.com')
const PREDICT_WAYPOINTS_URL = `${API_BASE}/predict_waypoints`
const NOWCAST_URL = `${API_BASE}/nowcast`

const NOMINATIM_SEARCH_URL = 'https://nominatim.openstreetmap.org/search'
const NOMINATIM_REVERSE_URL = 'https://nominatim.openstreetmap.org/reverse'

const ORS_KEY = import.meta.env.VITE_ORS_API_KEY

const RouteMap = lazy(() => import('./RouteMap.jsx'))

function warmBackend() {
  try {
    axios.get(`${API_BASE}/health`, { timeout: 90000 }).catch(() => {})
  } catch {
    // ignore
  }
}

function isRadarNotReady(err) {
  const detail = err?.response?.data?.detail || ''
  return typeof detail === 'string' && /not yet loaded/i.test(detail)
}

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
      if (!(isTimeout || isServerErr || isNetwork)) throw err
    }
  }
  throw lastErr
}

// Handles Render free-tier cold start: retries for up to 3 minutes,
// showing elapsed time, never surfacing "not yet loaded" as a user error.
async function postWithWarmup(url, body, config = {}, onStatus = null) {
  const INTERVAL_MS = 10000
  const MAX_WAIT_MS = 180000
  const start = Date.now()
  let attempt = 0

  while (true) {
    attempt++
    const elapsed = Math.round((Date.now() - start) / 1000)
    if (typeof onStatus === 'function') {
      if (attempt === 1) {
        onStatus('Scanning radar…')
      } else {
        onStatus(`Server warming up… ${elapsed}s (radar downloads on first load)`)
      }
    }
    try {
      return await axios.post(url, body, { timeout: 90000, ...config })
    } catch (err) {
      const status = err?.response?.status
      const isTimeout = err?.code === 'ECONNABORTED' || /timeout/i.test(err?.message || '')
      const isWarmup = isRadarNotReady(err) || (status === 503) || isTimeout || (!status && !err?.response)
      const fatal = !isWarmup || (Date.now() - start > MAX_WAIT_MS)
      if (fatal) throw err
      await new Promise((r) => setTimeout(r, INTERVAL_MS))
    }
  }
}

function computeViewboxAround(lat, lon, radiusKm = 180) {
  const la = Number(lat)
  const lo = Number(lon)
  if (!Number.isFinite(la) || !Number.isFinite(lo)) return null
  const dLat = radiusKm / 111
  const dLon = radiusKm / (111 * Math.max(0.2, Math.cos((la * Math.PI) / 180)))
  return `${lo - dLon},${la + dLat},${lo + dLon},${la - dLat}`
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
  const isNow = closest.startMin <= firstEta + 2
  const continuesToEnd = closest.endMin >= lastEta - 2.5

  const fmt = (m) => `${Math.max(0, Math.round(Number(m) || 0))} min`

  const closestDecay = closest.decayStatus || 'stable'
  const isDying = closestDecay === 'dying' || closestDecay === 'dead'
  const isWeakening = closestDecay === 'weakening'

  let headline, secondary, decayNote = null

  if (isDying) {
    headline = isNow
      ? "Rain nearby — but it's fading fast"
      : `Rain detected in ${fmt(closest.startMin)} — likely to clear`
    secondary = 'This patch is losing intensity. By the time you reach it, skies may already be clearing.'
    decayNote = 'dying'
  } else if (isWeakening) {
    headline = isNow
      ? 'Light rain right now — weakening as you travel'
      : `Rain in ${fmt(closest.startMin)} — and it's weakening`
    secondary = 'This patch is losing intensity. Rain will likely be lighter than current radar shows.'
    decayNote = 'weakening'
  } else {
    if (isNow) {
      headline = continuesToEnd
        ? 'Rain right now — continues to destination'
        : `Rain right now — clearing in ${fmt(closest.endMin)}`
      secondary = continuesToEnd
        ? `Expect rain for the full ${fmt(lastEta)} trip.`
        : 'After that, skies clear for the rest of the route.'
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
  const res = await axios.get(
    `${NOMINATIM_SEARCH_URL}?q=${encodeURIComponent(q)}&format=json&limit=1&countrycodes=in`,
    { timeout: 15000, headers: { Accept: 'application/json' } }
  )
  const data = Array.isArray(res.data) ? res.data[0] : null
  if (!data?.lat || !data?.lon) throw new Error('No geocoding results.')
  return { lat: parseFloat(data.lat), lon: parseFloat(data.lon), display_name: data.display_name }
}

async function searchPlaces(query, signal, opts = {}) {
  const q = String(query ?? '').trim()
  if (!q) return []
  const res = await axios.get(NOMINATIM_SEARCH_URL, {
    timeout: 15000,
    signal,
    params: {
      q,
      format: 'jsonv2',
      limit: 6,
      addressdetails: 1,
      ...(opts?.viewbox ? { viewbox: opts.viewbox, bounded: 0 } : {}),
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
    params: { lat, lon, format: 'jsonv2', zoom: 16, addressdetails: 1 },
    headers: { Accept: 'application/json' },
  })
  const name = res.data?.display_name
  return typeof name === 'string' && name.trim() ? name.trim() : null
}

function sampleRouteEvery5Min(routeCoords, speedKmh, intervalMin = 5) {
  if (!Array.isArray(routeCoords) || routeCoords.length < 2) return []
  const v = Number(speedKmh)
  if (!Number.isFinite(v) || v <= 0) return []
  const stepKm = v * (intervalMin / 60)
  const sampled = []
  let cumKm = 0
  let distAcc = 0
  const [lon0, lat0] = routeCoords[0]
  sampled.push({ lat: lat0, lon: lon0, eta_mins: 0, cumKm: 0 })
  for (let i = 1; i < routeCoords.length; i++) {
    const [lon1, lat1] = routeCoords[i - 1]
    const [lon2, lat2] = routeCoords[i]
    const dKm = haversine(lat1, lon1, lat2, lon2)
    cumKm += dKm
    distAcc += dKm
    if (distAcc >= stepKm) {
      sampled.push({ lat: lat2, lon: lon2, eta_mins: (cumKm / v) * 60, cumKm })
      distAcc = 0
    }
  }
  const [lonLast, latLast] = routeCoords[routeCoords.length - 1]
  const last = sampled[sampled.length - 1]
  if (!last || Math.abs(last.lat - latLast) > 1e-9 || Math.abs(last.lon - lonLast) > 1e-9) {
    sampled.push({ lat: latLast, lon: lonLast, eta_mins: (cumKm / v) * 60, cumKm })
  }
  return sampled
}

function binarySearchNearestIndex(sortedNums, target) {
  if (!Array.isArray(sortedNums) || !sortedNums.length) return -1
  let lo = 0, hi = sortedNums.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const v = sortedNums[mid]
    if (v === target) return mid
    if (v < target) lo = mid + 1
    else hi = mid - 1
  }
  if (lo <= 0) return 0
  if (lo >= sortedNums.length) return sortedNums.length - 1
  return Math.abs(sortedNums[lo - 1] - target) <= Math.abs(sortedNums[lo] - target) ? lo - 1 : lo
}

function buildColoredSegments(routeLonLat, predictedWaypoints) {
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
    return (maxEta > 0 ? Math.max(0, Math.min(1, eta / maxEta)) : 0) * totalKm
  })
  const segments = []
  for (let i = 1; i < routeLonLat.length; i++) {
    const midKm = (cumKm[i - 1] + cumKm[i]) / 2
    const idx = binarySearchNearestIndex(wpKm, midKm)
    const wp = idx >= 0 ? predictedWaypoints[idx] : null
    const inBounds = !!wp?.in_radar_bounds
    const label = wp?.label || 'Unknown'
    const color = inBounds ? getRainColor(label) : '#64748B'
    segments.push({
      positions: [
        [routeLonLat[i - 1][1], routeLonLat[i - 1][0]],
        [routeLonLat[i][1], routeLonLat[i][0]],
      ],
      color, inBounds, label,
      eta_mins: wp?.eta_mins ?? null,
      dbz: wp?.dbz ?? null,
      rain_expected: !!wp?.rain_expected,
      mid: {
        lat: (routeLonLat[i - 1][1] + routeLonLat[i][1]) / 2,
        lon: (routeLonLat[i - 1][0] + routeLonLat[i][0]) / 2,
      },
    })
  }
  return segments
}

function toIST(etaMins) {
  const ist = new Date(Date.now() + etaMins * 60 * 1000 + 5.5 * 60 * 60 * 1000)
  return `${String(ist.getUTCHours()).padStart(2, '0')}:${String(ist.getUTCMinutes()).padStart(2, '0')}`
}

// ── Shared Components ─────────────────────────────────────────────────────────

function RadarDownModal({ onClose }) {
  return (
    <div className="radar-down-overlay" role="dialog" aria-modal="true" aria-labelledby="radar-down-title">
      <div className="radar-down-modal">
        <div className="radar-down-icon" aria-hidden>
          <svg viewBox="0 0 48 48" fill="none" width="48" height="48">
            <circle cx="24" cy="24" r="22" stroke="#EF4444" strokeWidth="2.5" strokeDasharray="6 4" />
            <line x1="24" y1="24" x2="24" y2="24" stroke="#EF4444" strokeWidth="2.5" strokeLinecap="round" />
            <path d="M24 14v12M24 32v2" stroke="#EF4444" strokeWidth="2.5" strokeLinecap="round" />
          </svg>
        </div>
        <h2 className="radar-down-title" id="radar-down-title">Radar Unavailable</h2>
        <p className="radar-down-msg">
          Sorry for the inconvenience.<br />Radar is down for now.
        </p>
        <button className="radar-down-btn" type="button" onClick={onClose}>OK</button>
      </div>
    </div>
  )
}

function LongJourneyModal({ onContinue, onDismiss }) {
  return (
    <div className="radar-down-overlay" role="dialog" aria-modal="true" aria-labelledby="lj-title">
      <div className="radar-down-modal">
        <div className="radar-down-icon" aria-hidden>
          <svg viewBox="0 0 48 48" fill="none" width="48" height="48">
            <circle cx="24" cy="24" r="22" stroke="#f59e0b" strokeWidth="2.5" />
            <path d="M24 14v12M24 32v2" stroke="#f59e0b" strokeWidth="2.5" strokeLinecap="round" />
          </svg>
        </div>
        <h2 className="radar-down-title" id="lj-title" style={{ color: '#f59e0b' }}>Long Journey</h2>
        <p className="radar-down-msg">
          This journey is over 3 hours.<br />
          Radar predictions beyond 2 hours are less reliable.<br /><br />
          Try planning the journey <strong>in parts</strong> for better accuracy.
        </p>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
          <button className="radar-down-btn" type="button" onClick={onDismiss}
            style={{ background: 'rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.7)' }}>
            Got it
          </button>
          <button className="radar-down-btn" type="button" onClick={onContinue}
            style={{ background: '#f59e0b', color: '#000' }}>
            Continue anyway
          </button>
        </div>
      </div>
    </div>
  )
}

function TabBar({ activeTab, onChangeTab }) {
  return (
    <div className="tab-bar" role="tablist">
      <button
        role="tab"
        aria-selected={activeTab === 'route'}
        className={`tab-bar__btn${activeTab === 'route' ? ' tab-bar__btn--active' : ''}`}
        onClick={() => onChangeTab('route')}
      >
        Route
      </button>
      <button
        role="tab"
        aria-selected={activeTab === 'nowcast'}
        className={`tab-bar__btn${activeTab === 'nowcast' ? ' tab-bar__btn--active' : ''}`}
        onClick={() => onChangeTab('nowcast')}
      >
        Nowcast
      </button>
      <button
        role="tab"
        aria-selected={activeTab === 'chat'}
        className={`tab-bar__btn${activeTab === 'chat' ? ' tab-bar__btn--active' : ''}`}
        onClick={() => onChangeTab('chat')}
      >
        Ask AI
      </button>
    </div>
  )
}

// ── Ask AI (rain chatbot) ──────────────────────────────────────────────────────
const CHAT_URL = `${API_BASE}/chat`

const TOOL_LABELS = {
  geocode_place: 'Finding location…',
  get_nowcast: 'Checking radar…',
  get_route_rain: 'Scanning your route…',
  get_rain_movement: 'Reading rain movement…',
  get_accuracy_stats: 'Fetching accuracy stats…',
}

const CHAT_SUGGESTIONS = [
  'Will it rain in Connaught Place in the next hour?',
  'Should I leave now or wait 30 minutes?',
  'Is it raining on the route from Noida to Gurgaon?',
]

function ChatPage({ activeTab, onChangeTab }) {
  const [messages, setMessages] = useState([])   // {role:'user'|'model', text}
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [toolStatus, setToolStatus] = useState('')
  const scrollRef = useRef(null)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, toolStatus, sending])

  async function send(text) {
    const trimmed = (text ?? input).trim()
    if (!trimmed || sending) return
    setInput('')
    setToolStatus('')

    // Optimistically add the user message + an empty model bubble to stream into.
    const history = [...messages, { role: 'user', text: trimmed }]
    setMessages([...history, { role: 'model', text: '' }])
    setSending(true)

    // Warm the backend (Render cold start) without blocking the request.
    warmBackend()

    try {
      const resp = await fetch(CHAT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: history }),
      })

      if (resp.status === 503) {
        appendToModel('⚠️ The AI assistant isn’t configured yet on the server (missing API key).')
        return
      }
      if (!resp.ok || !resp.body) {
        appendToModel(`⚠️ Something went wrong (HTTP ${resp.status}). Please try again.`)
        return
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // SSE frames are separated by a blank line.
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''
        for (const frame of frames) {
          const line = frame.split('\n').find((l) => l.startsWith('data:'))
          if (!line) continue
          let evt
          try { evt = JSON.parse(line.slice(5).trim()) } catch { continue }

          if (evt.type === 'text') {
            setToolStatus('')
            appendToModel(evt.delta)
          } else if (evt.type === 'tool') {
            setToolStatus(TOOL_LABELS[evt.name] || 'Working…')
          } else if (evt.type === 'error') {
            setToolStatus('')
            appendToModel((prev) => (prev ? prev + '\n\n' : '') + `⚠️ ${evt.message}`)
          } else if (evt.type === 'done') {
            setToolStatus('')
          }
        }
      }
    } catch (err) {
      appendToModel('⚠️ Couldn’t reach the server. Check your connection and try again.')
    } finally {
      setSending(false)
      setToolStatus('')
    }
  }

  // Append text (string) or transform (fn) into the last model bubble.
  function appendToModel(deltaOrFn) {
    setMessages((prev) => {
      const next = [...prev]
      const last = next[next.length - 1]
      if (!last || last.role !== 'model') return prev
      const add = typeof deltaOrFn === 'function' ? deltaOrFn(last.text) : last.text + deltaOrFn
      next[next.length - 1] = { ...last, text: add }
      return next
    })
  }

  const isEmpty = messages.length === 0

  return (
    <div className="pg-chat">
      <nav className="nav">
        <span className="nav__brand">GARAJ BARAS</span>
        <span className="nav__live" aria-hidden>
          <span className="nav__live-dot" />
          LIVE
        </span>
      </nav>

      <TabBar activeTab={activeTab} onChangeTab={onChangeTab} />

      <div className="chat" ref={scrollRef}>
        {isEmpty && (
          <div className="chat__intro">
            <div className="chat__intro-title">Ask about the rain 🌧️</div>
            <div className="chat__suggestions">
              {CHAT_SUGGESTIONS.map((s) => (
                <button key={s} className="chat__chip" type="button" onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`chat__row chat__row--${m.role}`}>
            <div className={`chat__bubble chat__bubble--${m.role}`}>
              {m.text || (m.role === 'model' && sending ? <span className="chat__dots"><i /><i /><i /></span> : '')}
            </div>
          </div>
        ))}

        {toolStatus && (
          <div className="chat__row chat__row--model">
            <div className="chat__tool-pill">{toolStatus}</div>
          </div>
        )}
      </div>

      <form
        className="chat__inputbar"
        onSubmit={(e) => { e.preventDefault(); send() }}
      >
        <input
          className="chat__input"
          type="text"
          placeholder="Ask about the rain…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={sending}
        />
        <button className="chat__send" type="submit" disabled={sending || !input.trim()}>
          {sending ? '…' : 'Send'}
        </button>
      </form>
    </div>
  )
}

// ── Rain Timeline Bar ─────────────────────────────────────────────────────────
function RainTimelineBar({ patches, lastEta, showBreakdown, onToggleBreakdown }) {
  if (!patches?.length || !lastEta || lastEta <= 0) return null
  const first = patches[0]
  const duration = Math.round(first.endMin - first.startMin)
  const firstLabel = duration <= 1
    ? `Rain at ${toIST(first.startMin)}`
    : `${toIST(first.startMin)} – ${toIST(first.endMin)}`

  return (
    <div className="timeline">
      <div className="timeline__track">
        <div className="timeline__cap timeline__cap--start" aria-hidden />
        {patches.map((p, i) => {
          const left = Math.max(0, (p.startMin / lastEta) * 100)
          const width = Math.max(3, Math.min(100 - left, ((p.endMin - p.startMin) / lastEta) * 100))
          return (
            <div
              key={i}
              className="timeline__patch"
              style={{ left: `${left}%`, width: `${width}%` }}
            />
          )
        })}
        <div className="timeline__cap timeline__cap--end" aria-hidden />
      </div>
      <div className="timeline__meta">
        <span className="timeline__time">{toIST(0)}</span>
        <span className="timeline__rain-note">{firstLabel}</span>
        <span className="timeline__time">{toIST(lastEta)}</span>
      </div>

      <button className="breakdown-toggle" type="button" onClick={onToggleBreakdown}>
        {showBreakdown
          ? 'Hide breakdown'
          : `See full breakdown (${patches.length} rain ${patches.length === 1 ? 'zone' : 'zones'})`}
      </button>

      {showBreakdown && (
        <div className="breakdown">
          {patches.map((p, i) => {
            const decay = p.decayStatus || 'stable'
            const decayLabel =
              decay === 'dead' ? 'Likely clear'
              : decay === 'dying' ? 'Fading fast'
              : decay === 'weakening' ? 'Weakening'
              : null
            return (
              <div
                key={i}
                className={`breakdown__row${decay !== 'stable' ? ' breakdown__row--fading' : ''}`}
              >
                <span
                  className={`breakdown__dot breakdown__dot--${(p.intensity || 'Light').toLowerCase()}`}
                  aria-hidden
                />
                <div className="breakdown__info">
                  <span className="breakdown__time">{toIST(p.startMin)} – {toIST(p.endMin)}</span>
                  <span className="breakdown__intensity">{p.intensity || 'Light'} Rain</span>
                </div>
                {decayLabel && (
                  <span className={`decay-chip decay-chip--${decay}`}>{decayLabel}</span>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Nowcast Components ────────────────────────────────────────────────────────

function NowcastSlots({ slots }) {
  if (!Array.isArray(slots) || !slots.length) return null
  return (
    <div className="nc-slots">
      {slots.map((slot, i) => {
        const timeIST = toIST(slot.slot_mins)
        const conf = slot.arrival_confidence ?? slot.probability ?? 0
        const filled = Math.round(conf / 10)
        const hasRain = slot.has_rain
        const decayLabel =
          slot.decay_status === 'dying' ? 'Fading'
          : slot.decay_status === 'dead' ? 'Clearing'
          : slot.decay_status === 'weakening' ? 'Weakening'
          : slot.decay_status === 'new_cell' ? 'New storm'
          : null
        const isNow = i === 0

        return (
          <div
            key={i}
            className={`nc-slot${hasRain ? ' nc-slot--rain' : ' nc-slot--clear'}${isNow ? ' nc-slot--now' : ''}`}
          >
            <span className="nc-slot__time">
              {isNow ? 'Now' : timeIST}
            </span>
            <div className="nc-slot__bar" aria-hidden>
              {Array.from({ length: 10 }, (_, j) => (
                <div
                  key={j}
                  className={`nc-slot__seg${j < filled ? ' nc-slot__seg--filled' : ''}`}
                />
              ))}
            </div>
            <span className="nc-slot__label">
              {hasRain ? (slot.intensity || 'Rain') : 'No Rain'}
            </span>
            <span className={`nc-slot__prob${!hasRain ? ' nc-slot__prob--clear' : ''}`}>
              {hasRain ? `${conf}%` : '—'}
            </span>
            {decayLabel && hasRain && (
              <span className={`decay-chip decay-chip--${slot.decay_status}`}>{decayLabel}</span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function ForecastRadarPlayer({ lat, lon, requestId, highlightEta = null }) {
  const [frames, setFrames] = useState(null)
  const [idx, setIdx] = useState(0)
  const [playing, setPlaying] = useState(true)
  const [fcError, setFcError] = useState(null)

  // Frame whose slot time is nearest the traveller's ETA at this point.
  // Only meaningful inside the 1-hour animation window.
  const highlightIdx = useMemo(() => {
    const eta = Number(highlightEta)
    if (!frames?.length || !Number.isFinite(eta) || eta < 0 || eta > 60) return null
    let best = 0
    for (let i = 1; i < frames.length; i++) {
      if (Math.abs(frames[i].slot_mins - eta) < Math.abs(frames[best].slot_mins - eta)) best = i
    }
    return best
  }, [frames, highlightEta])

  useEffect(() => {
    let alive = true
    setFrames(null); setIdx(0); setPlaying(true); setFcError(null)
    axios.get(`${API_BASE}/nowcast/forecast_frames`, { params: { lat, lon, _: requestId } })
      .then((r) => { if (alive && Array.isArray(r.data?.frames) && r.data.frames.length) setFrames(r.data.frames) })
      .catch(() => { if (alive) setFcError('Forecast animation unavailable right now.') })
    return () => { alive = false }
  }, [lat, lon, requestId])

  // When an ETA highlight exists, open the player on that frame
  useEffect(() => {
    if (frames?.length && highlightIdx != null) setIdx(highlightIdx)
  }, [frames, highlightIdx])

  useEffect(() => {
    if (!playing || !frames?.length) return
    const iv = setInterval(() => setIdx((i) => (i + 1) % frames.length), 1000)
    return () => clearInterval(iv)
  }, [playing, frames])

  if (fcError) return <p className="nc-forecast-note">{fcError}</p>
  if (!frames) return <p className="nc-forecast-note">Rendering forecast animation…</p>

  const cur = frames[idx]
  return (
    <>
      <div className="nc-forecast-stage">
        <img className="nc-forecast-gif" src={cur.data} alt={cur.label} />
        <button
          type="button"
          className="nc-forecast-playbtn"
          onClick={() => setPlaying((p) => !p)}
          aria-label={playing ? 'Pause animation' : 'Play animation'}
        >
          {playing ? (
            <svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor" aria-hidden>
              <rect x="4" y="3" width="4.5" height="14" rx="1" />
              <rect x="11.5" y="3" width="4.5" height="14" rx="1" />
            </svg>
          ) : (
            <svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor" aria-hidden>
              <path d="M6 3.5v13l11-6.5-11-6.5z" />
            </svg>
          )}
        </button>
      </div>
      <div className="nc-forecast-scrub" role="tablist" aria-label="Forecast frames">
        {frames.map((f, i) => (
          <button
            key={f.slot_mins}
            type="button"
            role="tab"
            aria-selected={i === idx}
            className={
              `nc-forecast-dot${i === idx ? ' nc-forecast-dot--active' : ''}` +
              (i === highlightIdx ? ' nc-forecast-dot--eta' : '')
            }
            onClick={() => { setIdx(i); setPlaying(false) }}
          >
            {f.slot_mins === 0 ? 'Now' : `+${f.slot_mins}m`}
            {i === highlightIdx && <span className="nc-forecast-dot__eta-badge">ETA</span>}
          </button>
        ))}
      </div>
    </>
  )
}

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = window.atob(base64)
  const arr = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i)
  return arr
}

function RainAlertsCard({ lat, lon, label }) {
  const [status, setStatus] = useState('idle') // idle|working|enabled|unsupported|denied|error
  const [note, setNote] = useState(null)

  useEffect(() => {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      setStatus('unsupported')
      return
    }
    navigator.serviceWorker.getRegistration().then(async (reg) => {
      try {
        const sub = reg && (await reg.pushManager.getSubscription())
        if (sub && localStorage.getItem('gb_alerts_endpoint') === sub.endpoint) {
          setStatus('enabled')
          setNote(localStorage.getItem('gb_alerts_label') || null)
        }
      } catch {}
    }).catch(() => {})
  }, [])

  async function enable() {
    setStatus('working'); setNote(null)
    try {
      await navigator.serviceWorker.register('/sw.js')
      // Wait until the worker is ACTIVE — subscribing on a fresh, still-
      // installing registration throws InvalidStateError.
      const reg = await navigator.serviceWorker.ready
      const perm = await Notification.requestPermission()
      if (perm !== 'granted') { setStatus('denied'); return }
      const { data } = await axios.get(`${API_BASE}/alerts/vapid_public_key`)
      const key = urlBase64ToUint8Array(data.public_key)
      let sub
      try {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: key,
        })
      } catch (e) {
        // An old subscription with a different server key blocks resubscribe
        const old = await reg.pushManager.getSubscription()
        if (old) {
          await old.unsubscribe()
          sub = await reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: key,
          })
        } else {
          throw e
        }
      }
      await axios.post(`${API_BASE}/alerts/subscribe`, {
        subscription: sub.toJSON(), lat, lon, label: label || null,
      })
      localStorage.setItem('gb_alerts_endpoint', sub.endpoint)
      localStorage.setItem('gb_alerts_label', label || '')
      setStatus('enabled')
      setNote(label || null)
    } catch (e) {
      console.error('rain alerts enable failed:', e)
      setStatus('error')
      setNote(`${e?.name || 'Error'}: ${(e?.message || String(e)).slice(0, 140)}`)
    }
  }

  async function disable() {
    setStatus('working')
    try {
      const reg = await navigator.serviceWorker.getRegistration()
      const sub = reg && (await reg.pushManager.getSubscription())
      if (sub) {
        try { await axios.post(`${API_BASE}/alerts/unsubscribe`, { endpoint: sub.endpoint }) } catch {}
        await sub.unsubscribe()
      }
      localStorage.removeItem('gb_alerts_endpoint')
      localStorage.removeItem('gb_alerts_label')
      setStatus('idle')
    } catch {
      setStatus('idle')
    }
  }

  if (status === 'unsupported') return null

  return (
    <div className="alerts-card">
      <div className="alerts-card__row">
        <div className="alerts-card__text">
          <span className="alerts-card__title">🔔 Rain alerts</span>
          <span className="alerts-card__sub">
            {status === 'enabled'
              ? `Watching ${note || 'your saved location'} — you'll be notified when rain is here or ~15 min away.`
              : status === 'denied'
                ? 'Notifications are blocked in your browser settings.'
                : status === 'error'
                  ? `Could not enable alerts${note ? ` — ${note}` : ' — try again in a moment.'}`
                  : 'Get notified when rain reaches this location, or is ~15 min away (radar-based estimate).'}
          </span>
        </div>
        {status === 'enabled' ? (
          <button type="button" className="alerts-card__btn alerts-card__btn--off" onClick={disable}>
            Disable
          </button>
        ) : (
          <button
            type="button"
            className="alerts-card__btn"
            disabled={status === 'working' || lat == null || lon == null}
            onClick={enable}
          >
            {status === 'working' ? 'Enabling…' : 'Enable'}
          </button>
        )}
      </div>
    </div>
  )
}

function JourneyStopCard({ stop, onClose }) {
  const [nc, setNc] = useState(null)
  const [ncErr, setNcErr] = useState(null)
  const [placeName, setPlaceName] = useState(null)

  useEffect(() => {
    let alive = true
    setNc(null); setNcErr(null); setPlaceName(null)
    axios.post(NOWCAST_URL, { lat: stop.lat, lon: stop.lon })
      .then((r) => { if (alive) setNc(r.data) })
      .catch(() => { if (alive) setNcErr('Nowcast unavailable for this point right now.') })
    const ac = new AbortController()
    reversePlaceName(stop.lat, stop.lon, ac.signal)
      .then((n) => { if (alive && n) setPlaceName(n) })
      .catch(() => {})
    return () => { alive = false; ac.abort() }
  }, [stop.lat, stop.lon, stop.requestId])

  const etaRounded = Math.round(Number(stop.eta_mins) || 0)
  return (
    <div className="nc-forecast-card journey-stop-card">
      <div className="journey-stop-card__head">
        <div>
          <div className="nc-section-label">RAIN STOP · {stop.label?.toUpperCase() || 'RAIN'}</div>
          <div className="journey-stop-card__place">
            {placeName || `${stop.lat.toFixed(3)}, ${stop.lon.toFixed(3)}`}
          </div>
          <div className="journey-stop-card__meta">
            You arrive here ~{etaRounded} min into the trip · {toIST(stop.eta_mins)} IST
          </div>
        </div>
        <button type="button" className="journey-chip__close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>

      {ncErr && <p className="nc-forecast-note">{ncErr}</p>}
      {!nc && !ncErr && <p className="nc-forecast-note">Scanning radar at this point…</p>}
      {nc && nc.in_radar_bounds && (
        <>
          <div className={`banner banner--${(nc.rain_slots ?? 0) > 0 ? 'rain' : 'clear'}`} style={{ marginTop: 10 }}>
            <p className="banner__head">{nc.summary}</p>
          </div>
          <NowcastSlots slots={nc.slots} />
        </>
      )}
      {nc && !nc.in_radar_bounds && (
        <p className="nc-forecast-note">This point is outside radar coverage.</p>
      )}

      <div className="nc-section-label" style={{ marginTop: 14 }}>
        FORECAST RADAR AT THIS POINT
        {etaRounded <= 60 ? ' · YOUR ETA FRAME MARKED' : ''}
      </div>
      <ForecastRadarPlayer
        lat={stop.lat}
        lon={stop.lon}
        requestId={stop.requestId}
        highlightEta={stop.eta_mins}
      />
      <p className="nc-forecast-note">
        {etaRounded <= 60
          ? 'The frame marked ETA shows the predicted radar at the time you reach this point.'
          : 'Your arrival here is beyond the 1-hour animation window.'}
      </p>
    </div>
  )
}

function NowcastPage({ userLoc, activeTab, onChangeTab }) {
  const [ncLat, setNcLat] = useState(null)
  const [ncLon, setNcLon] = useState(null)
  const [ncName, setNcName] = useState('')
  const [isMyLoc, setIsMyLoc] = useState(false)

  const [ncSearchQuery, setNcSearchQuery] = useState('')
  const [ncSuggestions, setNcSuggestions] = useState([])
  const [ncSugOpen, setNcSugOpen] = useState(false)
  const ncDebounceRef = useRef(null)
  const ncAbortRef = useRef(null)

  const [ncResult, setNcResult] = useState(null)
  const [ncLoading, setNcLoading] = useState(false)
  const [ncError, setNcError] = useState(null)
  const [ncScanStatus, setNcScanStatus] = useState('')
  const [radarDown, setRadarDown] = useState(false)
  const [forecastGif, setForecastGif] = useState(null)  // { url, loading }

  useEffect(() => {
    if (userLoc && !ncLat) {
      setNcLat(userLoc.lat)
      setNcLon(userLoc.lon)
      setNcName('My Location')
      setIsMyLoc(true)
    }
  }, [userLoc])

  useEffect(() => {
    const q = ncSearchQuery.trim()
    if (ncAbortRef.current) ncAbortRef.current.abort()
    if (ncDebounceRef.current) clearTimeout(ncDebounceRef.current)
    if (q.length < 3) { setNcSuggestions([]); return }
    ncDebounceRef.current = setTimeout(async () => {
      const ac = new AbortController()
      ncAbortRef.current = ac
      try {
        const viewbox = userLoc ? computeViewboxAround(userLoc.lat, userLoc.lon, 220) : null
        setNcSuggestions(await searchPlaces(q, ac.signal, { viewbox }))
      } catch (e) {
        if (e?.name !== 'CanceledError' && e?.name !== 'AbortError') setNcSuggestions([])
      }
    }, 350)
    return () => { if (ncDebounceRef.current) clearTimeout(ncDebounceRef.current) }
  }, [ncSearchQuery, userLoc])

  function useMyLocation() {
    if (!userLoc) return
    setNcLat(userLoc.lat)
    setNcLon(userLoc.lon)
    setNcName('My Location')
    setIsMyLoc(true)
    setNcSearchQuery('')
    setNcSuggestions([])
    setNcResult(null)
    setNcError(null)
  }

  async function handleScan() {
    if (!ncLat || !ncLon) { setNcError('Please select a location first.'); return }
    setNcError(null)
    setNcLoading(true)
    setNcResult(null)
    setForecastGif(null)
    setNcScanStatus('Scanning radar…')
    try {
      const res = await postWithWarmup(
        NOWCAST_URL,
        { lat: ncLat, lon: ncLon },
        {},
        (msg) => setNcScanStatus(msg),
      )
      setNcResult(res.data)
      if (res.data?.in_radar_bounds) {
        setForecastGif({ lat: ncLat, lon: ncLon, requestId: Date.now() })
      }
      if ((res.data?.lag_mins ?? 0) > 75) {
        setRadarDown(true)
      }
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || 'Something went wrong.'
      setNcError(typeof detail === 'string' ? detail.slice(0, 300) : 'Nowcast failed.')
    } finally {
      setNcLoading(false)
      setNcScanStatus('')
    }
  }

  const hasLocation = ncLat != null && ncLon != null

  return (
    <div className="pg-nowcast">
      <nav className="nav">
        <span className="nav__brand">GARAJ BARAS</span>
        <span className="nav__live" aria-hidden>
          <span className="nav__live-dot" />
          LIVE
        </span>
      </nav>

      <TabBar activeTab={activeTab} onChangeTab={onChangeTab} />

      <div className="nc-card">
        <div className="nc-section-label">SCAN LOCATION</div>

        {/* Current selected location display */}
        {hasLocation && (
          <div className="nc-loc-display">
            <svg className="nc-loc-icon" viewBox="0 0 20 20" fill="none" aria-hidden>
              <circle cx="10" cy="9" r="3" stroke="currentColor" strokeWidth="1.8" />
              <path d="M10 2C6.13 2 3 5.13 3 9c0 5.25 7 11 7 11s7-5.75 7-11c0-3.87-3.13-7-7-7z"
                stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
            </svg>
            <div className="nc-loc-text">
              <span className="nc-loc-name">{ncName || 'Selected Location'}</span>
              <span className="nc-loc-coords">
                {Number(ncLat).toFixed(4)}°N, {Number(ncLon).toFixed(4)}°E
              </span>
            </div>
            {userLoc && !isMyLoc && (
              <button className="nc-use-me-btn" type="button" onClick={useMyLocation}>
                Use me
              </button>
            )}
          </div>
        )}

        {/* Location search */}
        <div className="typeahead-wrap" style={{ marginTop: hasLocation ? 12 : 0 }}>
          <div className="rf-shell">
            <input
              className="rf-input"
              placeholder={hasLocation ? 'Search a different location…' : 'Search location…'}
              value={ncSearchQuery}
              onChange={(e) => { setNcSearchQuery(e.target.value); setNcSugOpen(true) }}
              onFocus={() => setNcSugOpen(true)}
              onBlur={() => setTimeout(() => setNcSugOpen(false), 140)}
            />
          </div>
          {ncSugOpen && ncSuggestions.length > 0 && (
            <div className="dropdown" role="listbox">
              {ncSuggestions.map((it) => (
                <button
                  key={it.id}
                  type="button"
                  className="dropdown__item"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    setNcLat(it.lat); setNcLon(it.lon)
                    setNcName(toShortCityName(it.display_name))
                    setIsMyLoc(false)
                    setNcSearchQuery(''); setNcSuggestions([]); setNcSugOpen(false)
                    setNcResult(null); setNcError(null)
                  }}
                >
                  <div className="dropdown__primary">{it.display_name}</div>
                  {it.type && <div className="dropdown__secondary">{it.type}</div>}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Detect location button if no location yet */}
        {!hasLocation && userLoc && (
          <button className="nc-detect-btn" type="button" onClick={useMyLocation}>
            <svg viewBox="0 0 20 20" fill="none" width="15" height="15" aria-hidden>
              <circle cx="10" cy="10" r="3" stroke="currentColor" strokeWidth="2" />
              <circle cx="10" cy="10" r="7" stroke="currentColor" strokeWidth="1.5" strokeDasharray="3 3" />
              <line x1="10" y1="1" x2="10" y2="4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              <line x1="10" y1="16" x2="10" y2="19" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              <line x1="1" y1="10" x2="4" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              <line x1="16" y1="10" x2="19" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
            Use my current location
          </button>
        )}

        <button
          className="scan-btn"
          style={{ marginTop: 16 }}
          type="button"
          onClick={handleScan}
          disabled={!hasLocation || ncLoading}
        >
          {ncLoading ? (
            <>
              <span className="spinner" style={{ borderTopColor: '#05101F', borderColor: 'rgba(5,16,31,0.25)' }} />
              {ncScanStatus || 'Scanning…'}
            </>
          ) : (
            <>
              Scan Next 2 Hours
              <svg className="scan-btn__icon" viewBox="0 0 20 20" fill="none" aria-hidden>
                <path d="M4 10h12M11 5l5 5-5 5" stroke="currentColor" strokeWidth="2.2"
                  strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </>
          )}
        </button>
      </div>

      {hasLocation && (
        <RainAlertsCard lat={ncLat} lon={ncLon} label={ncName || null} />
      )}

      {ncError && (
        <div className="error-toast" role="alert" aria-live="polite">
          <span className="error-toast__icon" aria-hidden>!</span>
          <div>
            <div className="error-toast__title">Scan failed</div>
            <div className="error-toast__body">{ncError}</div>
          </div>
        </div>
      )}

      {ncResult && !ncLoading && (
        <div className="nc-results">

          {!ncResult.in_radar_bounds ? (
            <div className="banner banner--dying">
              <p className="banner__head">Outside radar coverage</p>
              <p className="banner__sub">
                This location is outside IMD radar coverage. Try a location in Delhi NCR or Uttar Pradesh.
              </p>
            </div>
          ) : (
            <>
              <div className={`banner banner--${(ncResult.rain_slots ?? 0) > 0 ? 'rain' : 'clear'}`}>
                <p className="banner__head">{ncResult.summary}</p>
              </div>
              <NowcastSlots slots={ncResult.slots} />
              {forecastGif && (
                <div className="nc-forecast-card">
                  <div className="nc-section-label">FORECAST RADAR · NEXT 1 HOUR</div>
                  <ForecastRadarPlayer
                    lat={forecastGif.lat}
                    lon={forecastGif.lon}
                    requestId={forecastGif.requestId}
                  />
                  <p className="nc-forecast-note">
                    Live simulation behind the predictions above — each rain patch moves
                    with its own measured velocity and fades with its decay trend.
                    Blue circle = radar scan zone around you.
                  </p>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {radarDown && <RadarDownModal onClose={() => setRadarDown(false)} />}
    </div>
  )
}

// ── App ───────────────────────────────────────────────────────────────────────
export default function App() {
  const [activeTab, setActiveTab] = useState('route')

  const [source, setSource] = useState('')
  const [destination, setDestination] = useState('')
  const [avgSpeedKmh, setAvgSpeedKmh] = useState('')
  const [sourcePlace, setSourcePlace] = useState(null)
  const [destPlace, setDestPlace] = useState(null)
  const [userLoc, setUserLoc] = useState(null)

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
  const [radarDown, setRadarDown] = useState(false)
  const [routeCoords, setRouteCoords] = useState([])
  const [routeSegments, setRouteSegments] = useState([])
  const [activeSeg, setActiveSeg] = useState(null)
  const [journeyStop, setJourneyStop] = useState(null)
  const [routeDistanceKm, setRouteDistanceKm] = useState(null)
  const [showBreakdown, setShowBreakdown] = useState(false)
  const [error, setError] = useState(null)
  const [showLongJourneyModal, setShowLongJourneyModal] = useState(false)
  const longJourneyResolveRef = useRef(null)

  const reverseAbortRef = useRef(null)
  const reverseCacheRef = useRef(new Map())

  const routeName = useMemo(() => toCityRouteName(source, destination), [source, destination])

  useEffect(() => { warmBackend() }, [])


  useEffect(() => {
    let alive = true
    try {
      if (!('geolocation' in navigator)) return
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          if (!alive) return
          const lat = Number(pos?.coords?.latitude)
          const lon = Number(pos?.coords?.longitude)
          if (Number.isFinite(lat) && Number.isFinite(lon)) setUserLoc({ lat, lon })
        },
        () => {},
        { enableHighAccuracy: false, timeout: 6000, maximumAge: 5 * 60 * 1000 }
      )
    } catch {}
    return () => { alive = false }
  }, [])

  useEffect(() => {
    const q = String(source || '').trim()
    if (sourceAbortRef.current) sourceAbortRef.current.abort()
    if (sourceDebounceRef.current) clearTimeout(sourceDebounceRef.current)
    if (q.length < 3) { setSourceSug([]); return }
    sourceDebounceRef.current = setTimeout(async () => {
      const ac = new AbortController()
      sourceAbortRef.current = ac
      try {
        const viewbox = userLoc ? computeViewboxAround(userLoc.lat, userLoc.lon, 220) : null
        setSourceSug(await searchPlaces(q, ac.signal, { viewbox }))
      } catch (e) {
        if (e?.name !== 'CanceledError' && e?.name !== 'AbortError') setSourceSug([])
      }
    }, 350)
    return () => { if (sourceDebounceRef.current) clearTimeout(sourceDebounceRef.current) }
  }, [source, userLoc])

  useEffect(() => {
    const q = String(destination || '').trim()
    if (destAbortRef.current) destAbortRef.current.abort()
    if (destDebounceRef.current) clearTimeout(destDebounceRef.current)
    if (q.length < 3) { setDestSug([]); return }
    destDebounceRef.current = setTimeout(async () => {
      const ac = new AbortController()
      destAbortRef.current = ac
      try {
        const viewbox = userLoc ? computeViewboxAround(userLoc.lat, userLoc.lon, 220) : null
        setDestSug(await searchPlaces(q, ac.signal, { viewbox }))
      } catch (e) {
        if (e?.name !== 'CanceledError' && e?.name !== 'AbortError') setDestSug([])
      }
    }, 350)
    return () => { if (destDebounceRef.current) clearTimeout(destDebounceRef.current) }
  }, [destination, userLoc])

  async function handlePredict() {
    const startCity = source.trim()
    const endCity = destination.trim()
    const speedNum = Number(avgSpeedKmh)
    if (!startCity || !endCity) { setError('Please enter both Source and Destination city names.'); return }
    if (!Number.isFinite(speedNum) || speedNum <= 0) { setError('Please enter a valid average speed (km/h).'); return }
    if (!ORS_KEY) { setError('Missing ORS API key. Set `VITE_ORS_API_KEY` in frontend/.env.'); return }

    setError(null); setLoading(true); setResult(null)
    setRouteCoords([]); setRouteSegments([]); setRouteDistanceKm(null)
    setScanning(false); setScanStatus('')
    warmBackend()

    try {
      const [start, end] = await Promise.all([
        sourcePlace ? Promise.resolve(sourcePlace) : geocode(startCity),
        destPlace ? Promise.resolve(destPlace) : geocode(endCity),
      ])

      let routeLonLat = null
      try {
        const orsRes = await axios.post(
          'https://api.openrouteservice.org/v2/directions/driving-car/geojson',
          { coordinates: [[start.lon, start.lat], [end.lon, end.lat]], radiuses: [5000, 5000] },
          { timeout: 90000, headers: { Authorization: ORS_KEY, 'Content-Type': 'application/json' } }
        )
        routeLonLat = orsRes.data?.features?.[0]?.geometry?.coordinates || null
      } catch {
        routeLonLat = [[start.lon, start.lat], [end.lon, end.lat]]
      }

      if (!Array.isArray(routeLonLat) || routeLonLat.length < 2) {
        throw new Error('Route planning failed (ORS returned empty geometry).')
      }

      let totalKm = 0
      for (let i = 1; i < routeLonLat.length; i++) {
        const [lon1, lat1] = routeLonLat[i - 1]
        const [lon2, lat2] = routeLonLat[i]
        totalKm += haversine(lat1, lon1, lat2, lon2)
      }
      setRouteDistanceKm(totalKm)

      if ((totalKm / speedNum) * 60 > 180) {
        setShowLongJourneyModal(true)
        const proceed = await new Promise((resolve) => { longJourneyResolveRef.current = resolve })
        setShowLongJourneyModal(false)
        if (!proceed) { setLoading(false); setScanning(false); return }
      }

      const sampled = sampleRouteEvery5Min(routeLonLat, speedNum, 5)
      if (!sampled.length) throw new Error('Could not sample route into waypoints.')

      setJourneyStop(null)
      setRouteCoords(routeLonLat.map(([lon, lat]) => [lat, lon]))
      setResult({
        total_waypoints: sampled.length, rain_waypoints: 0, clear_waypoints: sampled.length,
        first_rain_eta: null, first_rain_label: null, rain_direction_from: '—', rain_direction_to: '—',
        rain_speed_kmh: 0, radar_lag_mins: null, radar_freshness: 'pending',
        radar_message: 'Scanning radar…', route_distance_km: totalKm, waypoints: [], _pending: true,
      })
      setLoading(false)
      setScanning(true)

      const predictRes = await postWithWarmup(
        PREDICT_WAYPOINTS_URL,
        { waypoints: sampled.map(({ lat, lon, eta_mins }) => ({ lat, lon, eta_mins })) },
        {},
        (msg) => setScanStatus(msg),
      )

      const predictWaypoints = Array.isArray(predictRes.data?.waypoints) ? predictRes.data.waypoints : []
      const mergedWaypoints = predictWaypoints.map((wp) => ({
        ...wp,
        rainGroup: getRainGroupLabel(wp.label),
        rainColor: getRainColor(wp.label),
      }))

      setRouteSegments(buildColoredSegments(routeLonLat, mergedWaypoints))
      setResult({ ...predictRes.data, route_distance_km: totalKm, waypoints: mergedWaypoints })
      if ((predictRes.data?.radar_lag_mins ?? 0) > 75) {
        setRadarDown(true)
      }
    } catch (e) {
      const status = e?.response?.status
      const detail = e?.response?.data?.detail || e?.response?.data?.error?.message || e?.response?.data?.message
      const isTimeout = e?.code === 'ECONNABORTED' || /timeout/i.test(e?.message || '')
      const isNetwork = !status && !e?.response
      const msg = (isTimeout || isNetwork)
        ? "Couldn't reach the radar server. It may still be waking up — please try again in a minute."
        : status
          ? `Request failed (${status}): ${detail || e?.message || 'Unknown error'}`
          : e?.message || 'Something went wrong while scanning the radar.'
      setError(typeof msg === 'string' ? msg : 'Something went wrong.')
      setResult(null); setRouteCoords([]); setRouteSegments([]); setRouteDistanceKm(null)
    } finally {
      setLoading(false); setScanning(false); setScanStatus('')
    }
  }

  async function openSegmentPopup(seg) {
    if (!seg?.mid) return
    const lat = Number(seg.mid.lat)
    const lon = Number(seg.mid.lon)
    const key = `${lat.toFixed(4)},${lon.toFixed(4)}`
    setActiveSeg({ ...seg, locationName: reverseCacheRef.current.get(key) || null })
    if (reverseCacheRef.current.has(key)) return
    if (reverseAbortRef.current) reverseAbortRef.current.abort()
    const ac = new AbortController()
    reverseAbortRef.current = ac
    try {
      const name = await reversePlaceName(lat, lon, ac.signal)
      if (name) reverseCacheRef.current.set(key, name)
      setActiveSeg((prev) => prev ? { ...prev, locationName: name } : prev)
    } catch {}
  }

  const hasRain = !!result && (result.rain_waypoints ?? 0) > 0
  const shownDistanceKm = Number.isFinite(Number(routeDistanceKm))
    ? Number(routeDistanceKm)
    : Number.isFinite(Number(result?.route_distance_km))
      ? Number(result.route_distance_km)
      : null

  const rainTimeline = useMemo(() => {
    if (!result || result._pending) return null
    return computeRainTimeline(result.waypoints)
  }, [result])

  function handleBackToPlanner() {
    setResult(null); setError(null); setActiveSeg(null); setJourneyStop(null)
    setRouteCoords([]); setRouteSegments([]); setRouteDistanceKm(null); setShowBreakdown(false)
    setRadarDown(false)
  }

  function handleTabChange(tab) {
    setActiveTab(tab)
    setError(null)
  }

  return (
    <div className="app">

      {/* ── NOWCAST PAGE ── */}
      {activeTab === 'nowcast' && (
        <NowcastPage
          userLoc={userLoc}
          activeTab={activeTab}
          onChangeTab={handleTabChange}
        />
      )}

      {/* ── ASK AI (CHAT) PAGE ── */}
      {activeTab === 'chat' && (
        <ChatPage
          activeTab={activeTab}
          onChangeTab={handleTabChange}
        />
      )}

      {radarDown && <RadarDownModal onClose={() => setRadarDown(false)} />}

      {showLongJourneyModal && (
        <LongJourneyModal
          onContinue={() => longJourneyResolveRef.current?.(true)}
          onDismiss={() => { longJourneyResolveRef.current?.(false); setShowLongJourneyModal(false) }}
        />
      )}

      {/* ── ROUTE TAB SCREENS ── */}
      {activeTab === 'route' && (
        <>
          {/* PLANNER SCREEN */}
          {!loading && !result && (
            <div className="pg-planner">
              <nav className="nav">
                <span className="nav__brand">GARAJ BARAS</span>
                <span className="nav__live" aria-hidden>
                  <span className="nav__live-dot" />
                  LIVE
                </span>
              </nav>

              <TabBar activeTab={activeTab} onChangeTab={handleTabChange} />

              <section className="hero">
                <div className="hero__glow" aria-hidden />
                <h1 className="hero__title">Know the rain<br />before you leave.</h1>
                <p className="hero__sub">Delhi NCR &amp; UP · IMD radar · Route-aware</p>
              </section>

              <div className="planner-card">
                {/* Route inputs — vertical stack with left connector */}
                <div className="rf-stack">
                  {/* Source field */}
                  <div className="rf-field">
                    <div className="rf-track" aria-hidden>
                      <div className="rf-dot rf-dot--src" />
                    </div>
                    <div className="rf-body">
                      <label className="rf-label">FROM</label>
                      <div className="typeahead-wrap">
                        <div className="rf-shell">
                          <input
                            className="rf-input"
                            placeholder="Starting city"
                            value={source}
                            onChange={(e) => { setSource(e.target.value); setSourcePlace(null); setSourceOpen(true) }}
                            onFocus={() => setSourceOpen(true)}
                            onBlur={() => setTimeout(() => setSourceOpen(false), 140)}
                          />
                        </div>
                        {sourceOpen && (userLoc || sourceSug.length > 0) && (
                          <div className="dropdown" role="listbox">
                            {userLoc && (
                              <button
                                type="button"
                                className="dropdown__item dropdown__item--myloc"
                                onMouseDown={(e) => e.preventDefault()}
                                onClick={() => {
                                  setSource('My Location')
                                  setSourcePlace({ lat: userLoc.lat, lon: userLoc.lon, display_name: 'My Location' })
                                  setSourceSug([])
                                  setSourceOpen(false)
                                }}
                              >
                                <svg width="14" height="14" viewBox="0 0 20 20" fill="none" style={{ flexShrink: 0, marginRight: 6 }}>
                                  <circle cx="10" cy="10" r="3" fill="currentColor" />
                                  <circle cx="10" cy="10" r="7" stroke="currentColor" strokeWidth="1.8" strokeDasharray="3 3" />
                                  <line x1="10" y1="1" x2="10" y2="4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                                  <line x1="10" y1="16" x2="10" y2="19" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                                  <line x1="1" y1="10" x2="4" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                                  <line x1="16" y1="10" x2="19" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                                </svg>
                                <div>
                                  <div className="dropdown__primary">My Location</div>
                                  <div className="dropdown__secondary">Use your current location</div>
                                </div>
                              </button>
                            )}
                            {sourceSug.map((it) => (
                              <button
                                key={it.id}
                                type="button"
                                className="dropdown__item"
                                onMouseDown={(e) => e.preventDefault()}
                                onClick={() => { setSource(it.display_name); setSourcePlace(it); setSourceSug([]); setSourceOpen(false) }}
                              >
                                <div className="dropdown__primary">{it.display_name}</div>
                                {it.type && <div className="dropdown__secondary">{it.type}</div>}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Vertical connector line */}
                  <div className="rf-connector" aria-hidden>
                    <div className="rf-connector__line" />
                  </div>

                  {/* Destination field */}
                  <div className="rf-field">
                    <div className="rf-track" aria-hidden>
                      <div className="rf-dot rf-dot--dst" />
                    </div>
                    <div className="rf-body">
                      <label className="rf-label">TO</label>
                      <div className="typeahead-wrap">
                        <div className="rf-shell">
                          <input
                            className="rf-input"
                            placeholder="Destination city"
                            value={destination}
                            onChange={(e) => { setDestination(e.target.value); setDestPlace(null); setDestOpen(true) }}
                            onFocus={() => setDestOpen(true)}
                            onBlur={() => setTimeout(() => setDestOpen(false), 140)}
                          />
                        </div>
                        {destOpen && destSug.length > 0 && (
                          <div className="dropdown" role="listbox">
                            {destSug.map((it) => (
                              <button
                                key={it.id}
                                type="button"
                                className="dropdown__item"
                                onMouseDown={(e) => e.preventDefault()}
                                onClick={() => { setDestination(it.display_name); setDestPlace(it); setDestSug([]); setDestOpen(false) }}
                              >
                                <div className="dropdown__primary">{it.display_name}</div>
                                {it.type && <div className="dropdown__secondary">{it.type}</div>}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                {/* Speed */}
                <div className="speed-field">
                  <label className="rf-label">AVG SPEED</label>
                  <div className="speed-row">
                    <div className="rf-shell rf-shell--speed">
                      <input
                        className="rf-input"
                        inputMode="decimal"
                        placeholder="55"
                        value={avgSpeedKmh}
                        onChange={(e) => setAvgSpeedKmh(e.target.value)}
                      />
                    </div>
                    <span className="speed-unit">km/h</span>
                  </div>
                </div>

                {/* Scan CTA */}
                <button
                  className="scan-btn"
                  type="button"
                  onClick={handlePredict}
                  disabled={!source.trim() || !destination.trim() || !String(avgSpeedKmh).trim() || loading}
                >
                  Scan My Route
                  <svg className="scan-btn__icon" viewBox="0 0 20 20" fill="none" aria-hidden>
                    <path
                      d="M4 10h12M11 5l5 5-5 5"
                      stroke="currentColor"
                      strokeWidth="2.2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              </div>

              {error && (
                <div className="error-toast" role="alert" aria-live="polite">
                  <span className="error-toast__icon" aria-hidden>!</span>
                  <div>
                    <div className="error-toast__title">Scan failed</div>
                    <div className="error-toast__body">{error}</div>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* LOADING SCREEN */}
          {loading && (
            <div className="pg-loading" aria-live="polite">
              <div className="radar-anim" aria-hidden>
                <div className="radar-ring radar-ring--1" />
                <div className="radar-ring radar-ring--2" />
                <div className="radar-ring radar-ring--3" />
                <div className="radar-center" />
              </div>
              <p className="loading-label">SCANNING RADAR</p>
              <p className="loading-sub">Reading IMD frames · Mapping your route</p>
            </div>
          )}

          {/* RESULTS SCREEN */}
          {result && (
            <div className="pg-results">
              {/* Nav */}
              <nav className="nav">
                <button className="back-btn" type="button" onClick={handleBackToPlanner}>
                  <svg viewBox="0 0 20 20" fill="none" width="15" height="15" aria-hidden>
                    <path d="M13 4l-6 6 6 6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  Back
                </button>
                <span className={`status-pill status-pill--${result._pending ? 'pending' : hasRain ? 'rain' : 'clear'}`}>
                  {result._pending ? 'Scanning…' : hasRain ? 'Rain ahead' : 'Clear skies'}
                </span>
              </nav>

              {/* Route title */}
              <h2 className="route-title">{routeName}</h2>

              {/* Rain narrative banner */}
              {rainTimeline && !result._pending && (
                <div
                  className={`banner banner--${
                    rainTimeline.tone !== 'rain' ? 'clear'
                    : rainTimeline.decayNote === 'dying' ? 'dying'
                    : rainTimeline.decayNote === 'weakening' ? 'weakening'
                    : 'rain'
                  }`}
                  role="status"
                >
                  {rainTimeline.decayNote && (
                    <span className={`decay-badge decay-badge--${rainTimeline.decayNote}`}>
                      {rainTimeline.decayNote === 'dying' ? 'Patch fading' : 'Weakening'}
                    </span>
                  )}
                  <p className="banner__head">{rainTimeline.headline}</p>
                  {rainTimeline.secondary && <p className="banner__sub">{rainTimeline.secondary}</p>}
                </div>
              )}

              {/* Timeline */}
              {rainTimeline?.tone === 'rain' && !result._pending && (
                <RainTimelineBar
                  patches={rainTimeline.patches}
                  lastEta={rainTimeline.lastEta}
                  showBreakdown={showBreakdown}
                  onToggleBreakdown={() => setShowBreakdown((s) => !s)}
                />
              )}

              {/* Stats strip */}
              <div className="stats-strip">
                <div className="stat">
                  <span className="stat__label">Distance</span>
                  <span className="stat__value">
                    {shownDistanceKm == null ? '—' : `${shownDistanceKm.toFixed(1)} km`}
                  </span>
                </div>
                <div className="stat-div" aria-hidden />
                <div className="stat">
                  {(() => {
                    if (result._pending) return (<><span className="stat__label">Rain status</span><span className="stat__value">—</span></>)
                    const tl = rainTimeline
                    if (!tl || tl.tone === 'clear' || !tl.closest) return (<><span className="stat__label">Rain</span><span className="stat__value">None</span></>)
                    const firstEta = Number(tl.closest.startMin) || 0
                    const lastEta = Number(tl.lastEta) || 0
                    const isNow = firstEta <= 2
                    const continuesToEnd = tl.closest.endMin >= lastEta - 2.5
                    if (isNow && continuesToEnd) return (<><span className="stat__label">Rain duration</span><span className="stat__value">{Math.round(lastEta)} min</span></>)
                    if (isNow) return (<><span className="stat__label">Rain ends</span><span className="stat__value">{Math.round(tl.closest.endMin)} min</span></>)
                    return (<><span className="stat__label">Rain starts</span><span className="stat__value">{Math.round(firstEta)} min</span></>)
                  })()}
                </div>
              </div>

              {/* Map */}
              <div className="map-wrap">
                <Suspense
                  fallback={
                    <div className="map-container">
                      <div style={{ height: 320, display: 'grid', placeItems: 'center', color: 'var(--text-secondary)' }}>
                        Loading map…
                      </div>
                    </div>
                  }
                >
                  <RouteMap
                    routeCoords={routeCoords}
                    routeSegments={routeSegments}
                    waypoints={result?._pending ? [] : (result?.waypoints || [])}
                    activeSeg={activeSeg}
                    setActiveSeg={setActiveSeg}
                    openSegmentPopup={openSegmentPopup}
                    onStopDetails={(stop) => setJourneyStop({ ...stop, requestId: Date.now() })}
                  />
                </Suspense>
                {scanning && (
                  <div className="scan-overlay" role="status" aria-live="polite">
                    <span className="spinner" aria-hidden />
                    <span className="scan-overlay__label">SCANNING RADAR…</span>
                    {scanStatus && scanStatus !== 'Scanning radar…' && (
                      <span className="scan-overlay__sub">{scanStatus}</span>
                    )}
                  </div>
                )}
              </div>

              {/* Rain-stop detail: nowcast + forecast radar at the tapped stop */}
              {journeyStop && !result._pending && (
                <JourneyStopCard
                  stop={journeyStop}
                  onClose={() => setJourneyStop(null)}
                />
              )}

              {/* Legend */}
              <div className="legend">
                <span className="legend__title">Route colors</span>
                <div className="legend__chips">
                  {[
                    { cls: 'veryheavy', label: 'Very Heavy' },
                    { cls: 'heavy',     label: 'Heavy' },
                    { cls: 'moderate',  label: 'Moderate' },
                    { cls: 'light',     label: 'Light' },
                    { cls: 'verylight', label: 'Very Light' },
                    { cls: 'norain',    label: 'No Rain' },
                    { cls: 'unknown',   label: 'Out of radar' },
                  ].map(({ cls, label }) => (
                    <div key={cls} className="legend__chip">
                      <span className={`legend__swatch legend__swatch--${cls}`} aria-hidden />
                      <span className="legend__text">{label}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
