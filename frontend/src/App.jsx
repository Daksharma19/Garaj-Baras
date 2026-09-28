import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import axios from 'axios'
import { fetchRouteFog, fogZones, fmtVisibility } from './fog'
import { ManeuverIcon, fmtDist, maneuverText, streetName } from './maneuvers'
import './App.css'
import { useAuth, AccountButton, SignInGate } from './auth'
import SavedMenu from './SavedMenu'
import Onboarding, { ONBOARDING_KEY } from './Onboarding'
import { useT, tr, LangToggle } from './i18n'

const API_BASE = import.meta.env.DEV
  ? 'http://127.0.0.1:8000'
  : (import.meta.env.VITE_API_BASE || 'https://garaj-baras-api.onrender.com')
const PREDICT_WAYPOINTS_URL = `${API_BASE}/predict_waypoints`
const NOWCAST_URL = `${API_BASE}/nowcast`

const NOMINATIM_SEARCH_URL = 'https://nominatim.openstreetmap.org/search'
const NOMINATIM_REVERSE_URL = 'https://nominatim.openstreetmap.org/reverse'

const ORS_KEY = import.meta.env.VITE_ORS_API_KEY

const RouteMap = lazy(() => import('./RouteMap.jsx'))
const LiveJourneyPanel = lazy(() => import('./LiveJourney.jsx'))
const IndiaRadarMap = lazy(() => import('./IndiaRadarMap.jsx'))

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

// Phased warmup copy: reads like progress instead of a raw seconds counter.
// (The phases are approximate — Render cold boot ≈ 30-60 s, then the first
// radar GIF download + processing ≈ 5-10 s.)
function warmupStatusMessage(elapsedSec) {
  if (elapsedSec < 15) return tr('Waking the radar server…', 'रडार सर्वर जगाया जा रहा है…')
  if (elapsedSec < 50) return tr('Server starting up… downloading radar imagery', 'सर्वर शुरू हो रहा है… रडार इमेज डाउनलोड हो रही है')
  if (elapsedSec < 100) return tr('Analyzing the latest radar frames…', 'नवीनतम रडार फ्रेम का विश्लेषण हो रहा है…')
  return tr('Almost there — first load can take a couple of minutes', 'बस थोड़ा और — पहली बार लोड होने में कुछ मिनट लग सकते हैं')
}

// Handles Render free-tier cold start: retries for up to 3 minutes,
// showing warmup progress, never surfacing "not yet loaded" as a user error.
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
        onStatus(tr('Scanning radar…', 'रडार स्कैन हो रहा है…'))
      } else {
        onStatus(warmupStatusMessage(elapsed))
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

// Translate a rain-intensity label for DISPLAY only. The raw English value is
// still used everywhere for logic/CSS classes — this just localizes the text.
function tRainLabel(label) {
  const map = {
    'No Rain': ['No Rain', 'बारिश नहीं'],
    'Rain': ['Rain', 'बारिश'],
    'Very Light Rain': ['Very Light Rain', 'बहुत हल्की बारिश'],
    'Light Rain': ['Light Rain', 'हल्की बारिश'],
    'Moderate Rain': ['Moderate Rain', 'मध्यम बारिश'],
    'Heavy Rain': ['Heavy Rain', 'तेज़ बारिश'],
    'Very Heavy Rain': ['Very Heavy Rain', 'बहुत तेज़ बारिश'],
    'Light': ['Light', 'हल्की'],
    'Medium': ['Medium', 'मध्यम'],
    'Heavy': ['Heavy', 'तेज़'],
    'Unknown': ['Unknown', 'अज्ञात'],
  }
  const m = map[String(label || '').trim()]
  return m ? tr(m[0], m[1]) : label
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
      headline: tr('No rain on route', 'रास्ते में बारिश नहीं'),
      secondary: tr('Clear skies expected all the way.', 'पूरे रास्ते साफ आसमान की उम्मीद है।'),
      patches: [],
      closest: null,
      lastEta,
    }
  }

  const closest = patches[0]
  const isNow = closest.startMin <= firstEta + 2
  const continuesToEnd = closest.endMin >= lastEta - 2.5

  const fmt = (m) => `${Math.max(0, Math.round(Number(m) || 0))} ${tr('min', 'मिनट')}`

  const closestDecay = closest.decayStatus || 'stable'
  const isDying = closestDecay === 'dying' || closestDecay === 'dead'
  const isWeakening = closestDecay === 'weakening'

  let headline, secondary, decayNote = null

  if (isDying) {
    headline = isNow
      ? tr("Rain nearby — but it's fading fast", 'पास में बारिश — पर तेज़ी से कम हो रही है')
      : tr(`Rain detected in ${fmt(closest.startMin)} — likely to clear`, `${fmt(closest.startMin)} में बारिश — पर साफ होने की संभावना`)
    secondary = tr('This patch is losing intensity. By the time you reach it, skies may already be clearing.', 'यह बादल कमज़ोर हो रहा है। जब तक आप वहाँ पहुँचेंगे, आसमान शायद साफ हो चुका होगा।')
    decayNote = 'dying'
  } else if (isWeakening) {
    headline = isNow
      ? tr('Light rain right now — weakening as you travel', 'अभी हल्की बारिश — सफ़र के साथ कम होती जाएगी')
      : tr(`Rain in ${fmt(closest.startMin)} — and it's weakening`, `${fmt(closest.startMin)} में बारिश — और यह कमज़ोर हो रही है`)
    secondary = tr('This patch is losing intensity. Rain will likely be lighter than current radar shows.', 'यह बादल कमज़ोर हो रहा है। बारिश शायद अभी रडार में दिख रही बारिश से हल्की होगी।')
    decayNote = 'weakening'
  } else {
    if (isNow) {
      headline = continuesToEnd
        ? tr('Rain right now — continues to destination', 'अभी बारिश — मंज़िल तक जारी रहेगी')
        : tr(`Rain right now — clearing in ${fmt(closest.endMin)}`, `अभी बारिश — ${fmt(closest.endMin)} में साफ होगी`)
      secondary = continuesToEnd
        ? tr(`Expect rain for the full ${fmt(lastEta)} trip.`, `पूरे ${fmt(lastEta)} के सफ़र में बारिश की उम्मीद रखें।`)
        : tr('After that, skies clear for the rest of the route.', 'उसके बाद बाकी रास्ते में आसमान साफ रहेगा।')
    } else {
      if (continuesToEnd) {
        headline = tr(`Rain starts in ${fmt(closest.startMin)}`, `${fmt(closest.startMin)} में बारिश शुरू होगी`)
        secondary = tr('Once it starts, rain continues to your destination.', 'एक बार शुरू होने पर बारिश आपकी मंज़िल तक जारी रहेगी।')
      } else {
        const duration = Math.max(1, Math.round(closest.endMin - closest.startMin))
        headline = tr(`Rain starts in ${fmt(closest.startMin)}, clearing in ${fmt(closest.endMin)}`, `${fmt(closest.startMin)} में बारिश शुरू, ${fmt(closest.endMin)} में साफ`)
        secondary = tr(`Rainy stretch ~${duration} min.`, `बारिश वाला हिस्सा ~${duration} मिनट।`)
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

/**
 * ORS driving route between two {lat, lon} points →
 * { lonLat: [[lon,lat],...], steps: [{type, name, exit, distance(m), wp}] }.
 * Each step starts at geometry index wp. Throws on failure.
 */
async function fetchOrsRoute(from, to) {
  const res = await axios.post(
    'https://api.openrouteservice.org/v2/directions/driving-car/geojson',
    { coordinates: [[from.lon, from.lat], [to.lon, to.lat]], radiuses: [5000, 5000] },
    { timeout: 90000, headers: { Authorization: ORS_KEY, 'Content-Type': 'application/json' } }
  )
  const feature = res.data?.features?.[0]
  const lonLat = feature?.geometry?.coordinates || null
  const steps = (feature?.properties?.segments || [])
    .flatMap((seg) => seg?.steps || [])
    .filter((s) => Array.isArray(s?.way_points))
    .map((s) => ({ type: s.type, name: s.name, exit: s.exit_number ?? null, distance: Number(s.distance) || 0, wp: s.way_points[0] }))
  return { lonLat, steps }
}

function toCityRouteName(a, b) {
  const left = toShortCityName(a) || tr('Source', 'शुरुआत')
  const right = toShortCityName(b) || tr('Destination', 'मंज़िल')
  return `${left} → ${right}`
}

async function geocode(place) {
  const q = String(place ?? '').trim()
  if (!q) throw new Error(tr('Please enter both Source and Destination.', 'कृपया शुरुआत और मंज़िल दोनों दर्ज करें।'))
  const res = await axios.get(
    `${NOMINATIM_SEARCH_URL}?q=${encodeURIComponent(q)}&format=json&limit=1&countrycodes=in`,
    { timeout: 15000, headers: { Accept: 'application/json' } }
  )
  const data = Array.isArray(res.data) ? res.data[0] : null
  if (!data?.lat || !data?.lon) throw new Error(tr('No geocoding results.', 'कोई स्थान नहीं मिला।'))
  return { lat: parseFloat(data.lat), lon: parseFloat(data.lon), display_name: data.display_name }
}

async function searchPlaces(query, signal) {
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
  // Prefer the real sampled distance (_cumKm) when present — during a live
  // journey the passed waypoints all have eta 0, which breaks the
  // eta-proportional fallback mapping.
  const wpKm = predictedWaypoints.map((w) => {
    if (Number.isFinite(w?._cumKm)) return w._cumKm
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
  const t = useT()
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
        <h2 className="radar-down-title" id="radar-down-title">{t('Radar Unavailable', 'रडार उपलब्ध नहीं')}</h2>
        <p className="radar-down-msg">
          {t('Sorry for the inconvenience.', 'असुविधा के लिए क्षमा करें।')}<br />{t('Radar is down for now.', 'रडार अभी बंद है।')}
        </p>
        <button className="radar-down-btn" type="button" onClick={onClose}>{t('OK', 'ठीक है')}</button>
      </div>
    </div>
  )
}

function LongJourneyModal({ onContinue, onDismiss }) {
  const t = useT()
  return (
    <div className="radar-down-overlay" role="dialog" aria-modal="true" aria-labelledby="lj-title">
      <div className="radar-down-modal">
        <div className="radar-down-icon" aria-hidden>
          <svg viewBox="0 0 48 48" fill="none" width="48" height="48">
            <circle cx="24" cy="24" r="22" stroke="#f59e0b" strokeWidth="2.5" />
            <path d="M24 14v12M24 32v2" stroke="#f59e0b" strokeWidth="2.5" strokeLinecap="round" />
          </svg>
        </div>
        <h2 className="radar-down-title" id="lj-title" style={{ color: '#f59e0b' }}>{t('Long Journey', 'लंबा सफ़र')}</h2>
        <p className="radar-down-msg">
          {t('This journey is over 3 hours.', 'यह सफ़र 3 घंटे से ज़्यादा का है।')}<br />
          {t('Radar predictions beyond 2 hours are less reliable.', '2 घंटे के बाद रडार अनुमान कम भरोसेमंद होते हैं।')}<br /><br />
          {t('Try planning the journey', 'सफ़र की योजना')} <strong>{t('in parts', 'हिस्सों में')}</strong> {t('for better accuracy.', 'बनाएँ ताकि सटीकता बेहतर हो।')}
        </p>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
          <button className="radar-down-btn" type="button" onClick={onDismiss}
            style={{ background: 'rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.7)' }}>
            {t('Got it', 'समझ गया')}
          </button>
          <button className="radar-down-btn" type="button" onClick={onContinue}
            style={{ background: '#f59e0b', color: '#000' }}>
            {t('Continue anyway', 'फिर भी जारी रखें')}
          </button>
        </div>
      </div>
    </div>
  )
}

function TabBar({ activeTab, onChangeTab }) {
  const t = useT()
  return (
    <div className="tab-bar" role="tablist">
      <button
        role="tab"
        aria-selected={activeTab === 'route'}
        className={`tab-bar__btn${activeTab === 'route' ? ' tab-bar__btn--active' : ''}`}
        onClick={() => onChangeTab('route')}
      >
        {t('Route', 'रास्ता')}
      </button>
      <button
        role="tab"
        aria-selected={activeTab === 'nowcast'}
        className={`tab-bar__btn${activeTab === 'nowcast' ? ' tab-bar__btn--active' : ''}`}
        onClick={() => onChangeTab('nowcast')}
      >
        {t('Nowcast', 'तात्कालिक पूर्वानुमान')}
      </button>
      <button
        role="tab"
        aria-selected={activeTab === 'chat'}
        className={`tab-bar__btn${activeTab === 'chat' ? ' tab-bar__btn--active' : ''}`}
        onClick={() => onChangeTab('chat')}
      >
        {t('Ask AI', 'AI से पूछें')}
      </button>
      <button role="tab" aria-selected={activeTab === 'india-radar'} className={`tab-bar__btn${activeTab === 'india-radar' ? ' tab-bar__btn--active' : ''}`} onClick={() => onChangeTab('india-radar')}>India Radar</button>
    </div>
  )
}

// ── Ask AI (rain chatbot) ──────────────────────────────────────────────────────
const CHAT_URL = `${API_BASE}/chat`

function IndiaRadarPage({ activeTab, onChangeTab, onPickSaved }) {
  return (
    <div className="pg-india-radar">
      <nav className="nav"><span className="nav__brand">GARAJ BARAS</span><span className="nav__right"><LangToggle /><AccountButton /><SavedMenu apiBase={API_BASE} onPick={onPickSaved} /></span></nav>
      <TabBar activeTab={activeTab} onChangeTab={onChangeTab} />
      <section className="india-radar-hero"><p className="eyebrow">NATIONAL RADAR NETWORK</p><h1>India Radar Mosaic</h1><p>One live view, blended from every available IMD radar. Coverage outlines show where observations are available.</p></section>
      <Suspense fallback={<div className="india-radar-loading">Loading India radar…</div>}><IndiaRadarMap /></Suspense>
    </div>
  )
}

const toolLabel = (key) => ({
  geocode_place: tr('Finding location…', 'स्थान खोजा जा रहा है…'),
  get_nowcast: tr('Checking radar…', 'रडार जाँचा जा रहा है…'),
  get_route_rain: tr('Scanning your route…', 'आपका रास्ता स्कैन हो रहा है…'),
  get_rain_movement: tr('Reading rain movement…', 'बारिश की गति पढ़ी जा रही है…'),
  get_accuracy_stats: tr('Fetching accuracy stats…', 'सटीकता आँकड़े लाए जा रहे हैं…'),
}[key])

const chatSuggestions = () => [
  tr('Will it rain in Connaught Place in the next hour?', 'क्या अगले एक घंटे में कनॉट प्लेस में बारिश होगी?'),
  tr('Should I leave now or wait 30 minutes?', 'क्या मैं अभी निकलूँ या 30 मिनट रुकूँ?'),
  tr('Is it raining on the route from Noida to Gurgaon?', 'क्या नोएडा से गुड़गांव के रास्ते में बारिश हो रही है?'),
]

function ChatPage({ activeTab, onChangeTab, onPickSaved }) {
  const t = useT()
  const { user } = useAuth()
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
        appendToModel(t('⚠️ The AI assistant isn’t configured yet on the server (missing API key).', '⚠️ AI सहायक अभी सर्वर पर कॉन्फ़िगर नहीं है (API key नहीं है)।'))
        return
      }
      if (!resp.ok || !resp.body) {
        appendToModel(t(`⚠️ Something went wrong (HTTP ${resp.status}). Please try again.`, `⚠️ कुछ गड़बड़ हो गई (HTTP ${resp.status})। कृपया फिर से कोशिश करें।`))
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
            setToolStatus(toolLabel(evt.name) || t('Working…', 'काम चल रहा है…'))
          } else if (evt.type === 'error') {
            setToolStatus('')
            appendToModel((prev) => (prev ? prev + '\n\n' : '') + `⚠️ ${evt.message}`)
          } else if (evt.type === 'done') {
            setToolStatus('')
          }
        }
      }
    } catch (err) {
      appendToModel(t('⚠️ Couldn’t reach the server. Check your connection and try again.', '⚠️ सर्वर तक नहीं पहुँच पाए। अपना कनेक्शन जाँचें और फिर कोशिश करें।'))
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

  // Ask AI is a signed-in feature (route + nowcast stay free).
  if (!user) {
    return (
      <div className="pg-chat">
        <nav className="nav">
          <span className="nav__brand">GARAJ BARAS</span>
          <span className="nav__right">
            <span className="nav__live" aria-hidden>
              <span className="nav__live-dot" />
              {t('LIVE', 'लाइव')}
            </span>
            <LangToggle />
            <AccountButton />
            <SavedMenu apiBase={API_BASE} onPick={onPickSaved} />
          </span>
        </nav>
        <TabBar activeTab={activeTab} onChangeTab={onChangeTab} />
        <SignInGate
          title={t('Sign in to ask the AI', 'AI से पूछने के लिए साइन इन करें')}
          sub={t('The rain assistant is tied to your account. Route check and nowcast stay free — no login needed there.', 'बारिश सहायक आपके खाते से जुड़ा है। रास्ता जाँच और तात्कालिक पूर्वानुमान मुफ़्त हैं — वहाँ लॉगिन की ज़रूरत नहीं।')}
        />
      </div>
    )
  }

  return (
    <div className="pg-chat">
      <nav className="nav">
        <span className="nav__brand">GARAJ BARAS</span>
        <span className="nav__right">
          <span className="nav__live" aria-hidden>
            <span className="nav__live-dot" />
            {t('LIVE', 'लाइव')}
          </span>
          <LangToggle />
          <AccountButton />
          <SavedMenu apiBase={API_BASE} onPick={onPickSaved} />
        </span>
      </nav>

      <TabBar activeTab={activeTab} onChangeTab={onChangeTab} />

      <div className="chat" ref={scrollRef}>
        {isEmpty && (
          <div className="chat__intro">
            <div className="chat__intro-title">{t('Ask about the rain 🌧️', 'बारिश के बारे में पूछें 🌧️')}</div>
            <div className="chat__suggestions">
              {chatSuggestions().map((s) => (
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
          placeholder={t('Ask about the rain…', 'बारिश के बारे में पूछें…')}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={sending}
        />
        <button className="chat__send" type="submit" disabled={sending || !input.trim()}>
          {sending ? '…' : t('Send', 'भेजें')}
        </button>
      </form>
    </div>
  )
}

// ── Rain Timeline Bar ─────────────────────────────────────────────────────────
function RainTimelineBar({ patches, lastEta, showBreakdown, onToggleBreakdown }) {
  const t = useT()
  if (!patches?.length || !lastEta || lastEta <= 0) return null
  const first = patches[0]
  const duration = Math.round(first.endMin - first.startMin)
  const firstLabel = duration <= 1
    ? t(`Rain at ${toIST(first.startMin)}`, `${toIST(first.startMin)} पर बारिश`)
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
          ? t('Hide breakdown', 'विवरण छिपाएँ')
          : t(`See full breakdown (${patches.length} rain ${patches.length === 1 ? 'zone' : 'zones'})`, `पूरा विवरण देखें (${patches.length} बारिश ${patches.length === 1 ? 'क्षेत्र' : 'क्षेत्र'})`)}
      </button>

      {showBreakdown && (
        <div className="breakdown">
          {patches.map((p, i) => {
            const decay = p.decayStatus || 'stable'
            const decayLabel =
              decay === 'dead' ? t('Likely clear', 'साफ होने की संभावना')
              : decay === 'dying' ? t('Fading fast', 'तेज़ी से कम हो रही')
              : decay === 'weakening' ? t('Weakening', 'कमज़ोर हो रही')
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
                  <span className="breakdown__intensity">{tRainLabel(p.intensity || 'Light')} {t('Rain', 'बारिश')}</span>
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
  const t = useT()
  if (!Array.isArray(slots) || !slots.length) return null
  return (
    <div className="nc-slots">
      {slots.map((slot, i) => {
        const timeIST = toIST(slot.slot_mins)
        const conf = slot.arrival_confidence ?? slot.probability ?? 0
        const filled = Math.round(conf / 10)
        const hasRain = slot.has_rain
        const decayLabel =
          slot.decay_status === 'dying' ? t('Fading', 'कम हो रही')
          : slot.decay_status === 'dead' ? t('Clearing', 'साफ हो रही')
          : slot.decay_status === 'weakening' ? t('Weakening', 'कमज़ोर हो रही')
          : slot.decay_status === 'new_cell' ? t('New storm', 'नया बादल')
          : slot.decay_status === 'growing' ? t('Intensifying', 'तेज़ हो रही')
          : null
        const isNow = i === 0

        return (
          <div
            key={i}
            className={`nc-slot${hasRain ? ' nc-slot--rain' : ' nc-slot--clear'}${isNow ? ' nc-slot--now' : ''}`}
          >
            <span className="nc-slot__time">
              {isNow ? t('Now', 'अभी') : timeIST}
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
              {hasRain ? tRainLabel(slot.intensity || 'Rain') : t('No Rain', 'बारिश नहीं')}
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
  const t = useT()
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
      .catch(() => { if (alive) setFcError(t('Forecast animation unavailable right now.', 'पूर्वानुमान एनिमेशन अभी उपलब्ध नहीं है।')) })
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
  if (!frames) return <p className="nc-forecast-note">{t('Rendering forecast animation…', 'पूर्वानुमान एनिमेशन बन रहा है…')}</p>

  const cur = frames[idx]
  return (
    <>
      <div className="nc-forecast-stage">
        <img className="nc-forecast-gif" src={cur.data} alt={cur.label} />
        <button
          type="button"
          className="nc-forecast-playbtn"
          onClick={() => setPlaying((p) => !p)}
          aria-label={playing ? t('Pause animation', 'एनिमेशन रोकें') : t('Play animation', 'एनिमेशन चलाएँ')}
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
      <div className="nc-forecast-scrub" role="tablist" aria-label={t('Forecast frames', 'पूर्वानुमान फ्रेम')}>
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
            {f.slot_mins === 0 ? t('Now', 'अभी') : `+${f.slot_mins}${t('m', 'मि')}`}
            {i === highlightIdx && <span className="nc-forecast-dot__eta-badge">{t('ETA', 'पहुँच')}</span>}
          </button>
        ))}
      </div>
    </>
  )
}

// ── Radar v2: canvas player driven by /nowcast/radar_scene ─────────────────
// History = real cached frames (10-min cadence, true timestamps), placed on
// the timeline lag-corrected (latest frame sits at t = -lag). Future = the
// nowcast simulation advected continuously: each rain cell moves with its
// owner patch's velocity and fades with its decay trend.

// Vivid, continuously-interpolated reflectivity ramp (colors lerp between
// stops instead of hard steps — pro-radar-app look).
const DBZ_STOPS = [
  [8, [22, 48, 105]],
  [20, [37, 108, 199]], [25, [40, 160, 228]], [30, [58, 200, 178]],
  [35, [94, 217, 100]], [38, [172, 227, 64]], [41, [249, 208, 46]],
  [44, [251, 156, 38]], [50, [246, 106, 34]], [55, [238, 54, 62]],
  [60, [200, 70, 255]], [70, [255, 172, 255]],
]
// LUT over 0..70 dBZ in 0.5 steps → [r,g,b,a]; alpha combines a soft outer
// edge (below ~18 dBZ fades out) with an intensity ramp (heavy rain = denser).
const DBZ_LUT = (() => {
  const lut = new Uint8ClampedArray(141 * 4)
  for (let i = 0; i <= 140; i++) {
    const v = i / 2
    let k = 0
    while (k < DBZ_STOPS.length - 2 && v > DBZ_STOPS[k + 1][0]) k++
    const [d0, c0] = DBZ_STOPS[k]
    const [d1, c1] = DBZ_STOPS[k + 1]
    const f = Math.min(1, Math.max(0, (v - d0) / (d1 - d0)))
    const edge = Math.min(1, Math.max(0, (v - 9) / 9))
    const body = 0.6 + 0.4 * Math.min(1, Math.max(0, (v - 20) / 26))
    lut[i * 4] = c0[0] + (c1[0] - c0[0]) * f
    lut[i * 4 + 1] = c0[1] + (c1[1] - c0[1]) * f
    lut[i * 4 + 2] = c0[2] + (c1[2] - c0[2]) * f
    lut[i * 4 + 3] = Math.round(255 * edge * body)
  }
  return lut
})()

// Upscale factor for the field renderer: dBZ grids are bilinearly interpolated
// 4× BEFORE colorizing (interpolate-data-then-colorize — smooth gradients with
// crisp cores, no canvas blur needed).
const FIELD_UP = 4

function paintField(img, dbz, fade, gw, gh) {
  const W = gw * FIELD_UP, H = gh * FIELD_UP
  const data = img.data
  data.fill(0)
  for (let y = 0; y < H; y++) {
    let fy = (y + 0.5) / FIELD_UP - 0.5
    fy = Math.max(0, Math.min(gh - 1, fy))
    const y0 = Math.floor(fy), y1 = Math.min(gh - 1, y0 + 1), wy = fy - y0
    for (let x = 0; x < W; x++) {
      let fx = (x + 0.5) / FIELD_UP - 0.5
      fx = Math.max(0, Math.min(gw - 1, fx))
      const x0 = Math.floor(fx), x1 = Math.min(gw - 1, x0 + 1), wx = fx - x0
      const w00 = (1 - wy) * (1 - wx), w01 = (1 - wy) * wx
      const w10 = wy * (1 - wx), w11 = wy * wx
      const v = dbz[y0 * gw + x0] * w00 + dbz[y0 * gw + x1] * w01 +
                dbz[y1 * gw + x0] * w10 + dbz[y1 * gw + x1] * w11
      if (v < 9.5) continue
      const li = Math.min(140, Math.round(v * 2)) * 4
      let a = DBZ_LUT[li + 3]
      if (fade) {
        // fade weighted by each neighbor's rain contribution, so empty cells
        // (fade 0, dbz 0) don't darken blob edges
        const c00 = dbz[y0 * gw + x0] * w00, c01 = dbz[y0 * gw + x1] * w01
        const c10 = dbz[y1 * gw + x0] * w10, c11 = dbz[y1 * gw + x1] * w11
        const cs = c00 + c01 + c10 + c11
        if (cs > 0) {
          a *= (fade[y0 * gw + x0] * c00 + fade[y0 * gw + x1] * c01 +
                fade[y1 * gw + x0] * c10 + fade[y1 * gw + x1] * c11) / cs
        }
      }
      if (a < 4) continue
      const j = (y * W + x) * 4
      data[j] = DBZ_LUT[li]; data[j + 1] = DBZ_LUT[li + 1]; data[j + 2] = DBZ_LUT[li + 2]
      data[j + 3] = a
    }
  }
}
function b64ToBytes(b64) {
  const raw = atob(b64)
  const arr = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i)
  return arr
}

function RadarScenePlayer({ lat, lon, requestId, highlightEta = null }) {
  const t = useT()
  const [scene, setScene] = useState(null)
  const [error, setError] = useState(null)
  const [playing, setPlaying] = useState(true)
  const canvasRef = useRef(null)
  const trackRef = useRef(null)
  const tRef = useRef(null)          // current timeline minute
  const playingRef = useRef(true)
  const [badge, setBadge] = useState({ time: '', mode: '' })

  useEffect(() => {
    let alive = true
    setScene(null); setError(null); setPlaying(true); playingRef.current = true; tRef.current = null
    axios.get(`${API_BASE}/nowcast/radar_scene`, { params: { lat, lon, _: requestId } })
      .then((r) => { if (alive && r.data?.history?.length) setScene(r.data) })
      .catch(() => { if (alive) setError(t('Radar animation unavailable right now.', 'रडार एनिमेशन अभी उपलब्ध नहीं है।')) })
    return () => { alive = false }
  }, [lat, lon, requestId])

  // Decode grids + prebuild offscreen canvases once per scene
  const model = useMemo(() => {
    if (!scene) return null
    const { w: gw, h: gh } = scene.grid
    const lag = scene.lag_mins
    const W = gw * FIELD_UP, H = gh * FIELD_UP
    const mkFieldCanvas = (dbz) => {
      const c = document.createElement('canvas')
      c.width = W; c.height = H
      const g = c.getContext('2d')
      const img = g.createImageData(W, H)
      paintField(img, dbz, null, gw, gh)
      g.putImageData(img, 0, 0)
      return c
    }
    const history = scene.history.map((h) => ({
      t: h.mins - lag,               // lag-corrected timeline position
      timeIst: h.time_ist,
      dbz: b64ToBytes(h.dbz),
      canvas: null,                  // built lazily below
    }))
    history.forEach((h) => { h.canvas = mkFieldCanvas(h.dbz) })
    const patchById = {}
    for (const p of scene.patches) patchById[p.id] = p
    const simCanvas = document.createElement('canvas')
    simCanvas.width = W; simCanvas.height = H
    const simCtx = simCanvas.getContext('2d')
    return {
      gw, gh, lag,
      history,
      tMin: history[0].t,
      nowDbz: history[history.length - 1].dbz,
      owner: b64ToBytes(scene.owner),
      patchById,
      global: scene.global,
      cellPx: scene.grid.cell_px,
      userGx: scene.crop.user_gx, userGy: scene.crop.user_gy,
      radiusCells: scene.crop.radius_cells,
      places: scene.places || [],
      // reusable buffers for the simulated (t > -lag) half: bilinear splat
      // accumulators + upscaled field canvas (no per-frame allocations)
      sim: simCanvas, simCtx,
      simImg: simCtx.createImageData(W, H),
      simVal: new Float32Array(gw * gh),
      simWt: new Float32Array(gw * gh),
      simFade: new Float32Array(gw * gh),
      simDbz: new Float32Array(gw * gh),
    }
  }, [scene])

  // Mirrors backend _patch_fade / forecast_gif rules
  const patchFade = (p, eff, slotMins) => {
    if (!p) return { fade: 0.9, ddbz: 0 } // unclaimed rain: global drift, gentle fade
    const raw = p.raw_dbz || 0
    if (raw <= 0) return { fade: 1, ddbz: 0 }
    if (p.decay_mode === 'new' && slotMins > p.max_assert_mins) return null
    const proj = raw + p.decay_rate * (eff / 10)
    if (proj < p.min_dbz) return null
    return { fade: Math.max(0.3, Math.min(1, proj / raw)), ddbz: p.decay_rate * (eff / 10) }
  }

  const drawFrame = (t) => {
    const m = model
    const cv = canvasRef.current
    if (!m || !cv) return
    const ctx = cv.getContext('2d')
    const S = cv.width
    const scale = S / m.gw
    const k = S / 480                       // UI scale (hi-DPI / responsive)
    // basemap
    ctx.fillStyle = '#08101f'
    ctx.fillRect(0, 0, S, S)
    ctx.strokeStyle = '#16233c'
    ctx.lineWidth = k
    const cx = m.userGx * scale, cy = m.userGy * scale
    for (const rr of [0.33, 0.66, 1.0]) {
      ctx.beginPath(); ctx.arc(cx, cy, m.radiusCells * scale * rr, 0, 7); ctx.stroke()
    }
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    // field canvases are pre-smoothed (bilinear dBZ interpolation) — a single
    // sharp draw, no blur filter needed
    const drawRain = (src, alpha = 1) => {
      ctx.globalAlpha = alpha
      ctx.drawImage(src, 0, 0, S, S)
      ctx.globalAlpha = 1
    }
    if (t <= -m.lag + 0.01 && m.history.length) {
      // observed half: eased cross-fade between the two neighboring frames
      let i = 0
      while (i < m.history.length - 1 && m.history[i + 1].t <= t) i++
      const a = m.history[i]
      const b = m.history[Math.min(i + 1, m.history.length - 1)]
      const w = b.t > a.t ? Math.min(1, Math.max(0, (t - a.t) / (b.t - a.t))) : 0
      const e = w * w * (3 - 2 * w)         // smoothstep easing
      drawRain(a.canvas, 1)
      if (e > 0.004) drawRain(b.canvas, e)
    } else {
      // simulated half: advect the latest frame's cells forward by eff mins.
      // Bilinear splat at the FRACTIONAL target position (weight-normalized)
      // so blobs glide continuously instead of snapping cell to cell.
      const eff = t + m.lag                 // minutes since the latest frame
      const slotMins = Math.max(0, t)
      const shifts = eff / 10
      const { gw, gh } = m
      const val = m.simVal, wt = m.simWt, fad = m.simFade, out = m.simDbz
      val.fill(0); wt.fill(0); fad.fill(0)
      for (let gy = 0; gy < gh; gy++) {
        for (let gx = 0; gx < gw; gx++) {
          const i = gy * gw + gx
          const dbz = m.nowDbz[i]
          if (!dbz) continue
          const p = m.patchById[m.owner[i]] || null
          const f = patchFade(p, eff, slotMins)
          if (!f) continue
          const vx = p ? p.vx : m.global.vx
          const vy = p ? p.vy : m.global.vy
          const fxp = gx + (vx * shifts) / m.cellPx
          const fyp = gy + (vy * shifts) / m.cellPx
          const x0 = Math.floor(fxp), y0 = Math.floor(fyp)
          const dx = fxp - x0, dy = fyp - y0
          const pd = Math.max(10, dbz + f.ddbz)
          for (let sy = 0; sy < 2; sy++) {
            const ny = y0 + sy
            if (ny < 0 || ny >= gh) continue
            const wy = sy ? dy : 1 - dy
            for (let sx = 0; sx < 2; sx++) {
              const nx = x0 + sx
              if (nx < 0 || nx >= gw) continue
              const ww = wy * (sx ? dx : 1 - dx)
              if (ww < 0.001) continue
              const j = ny * gw + nx
              val[j] += pd * ww
              fad[j] += f.fade * ww
              wt[j] += ww
            }
          }
        }
      }
      for (let j = 0; j < gw * gh; j++) {
        if (wt[j] > 0) { out[j] = val[j] / wt[j]; fad[j] /= wt[j] } else out[j] = 0
      }
      paintField(m.simImg, out, fad, gw, gh)
      m.simCtx.putImageData(m.simImg, 0, 0)
      drawRain(m.sim, t > 0 ? 0.94 : 1)
    }
    // place labels (like IMD's city abbreviations, but readable)
    ctx.font = `600 ${10 * k}px ui-monospace, Consolas, monospace`
    for (const pl of m.places) {
      const px2 = pl.gx * scale, py2 = pl.gy * scale
      if (Math.hypot(px2 - cx, py2 - cy) < 16 * k) continue
      ctx.fillStyle = 'rgba(226,232,240,0.9)'
      ctx.fillRect(px2 - 1.5 * k, py2 - 1.5 * k, 3 * k, 3 * k)
      ctx.fillStyle = 'rgba(5,16,31,0.75)'
      ctx.fillText(pl.name, px2 + 6 * k, py2 + 4 * k)
      ctx.fillText(pl.name, px2 + 5 * k, py2 + 3 * k)
      ctx.fillStyle = 'rgba(196,209,230,0.95)'
      ctx.fillText(pl.name, px2 + 5.5 * k, py2 + 3.5 * k)
    }
    // scan-zone ring + user marker
    ctx.strokeStyle = t > 0 ? 'rgba(96,165,250,0.9)' : 'rgba(96,165,250,0.5)'
    if (t > 0) ctx.setLineDash([6 * k, 5 * k])
    ctx.lineWidth = 1.6 * k
    ctx.beginPath(); ctx.arc(cx, cy, m.radiusCells * scale - 2 * k, 0, 7); ctx.stroke()
    ctx.setLineDash([])
    ctx.fillStyle = '#fff'
    ctx.beginPath(); ctx.arc(cx, cy, 5 * k, 0, 7); ctx.fill()
    ctx.strokeStyle = 'rgba(255,255,255,0.7)'; ctx.lineWidth = 2 * k
    ctx.beginPath(); ctx.arc(cx, cy, 10 * k, 0, 7); ctx.stroke()
    // badge + thumb
    const tt = Math.round(t)
    let timeLabel
    if (t <= -m.lag) {
      let nearest = m.history[0]
      for (const h of m.history) if (Math.abs(h.t - t) < Math.abs(nearest.t - t)) nearest = h
      timeLabel = `${nearest.timeIst} ${tr('IST', 'IST')}`
    } else {
      timeLabel = tt === 0 ? tr('NOW', 'अभी') : (tt > 0 ? `+${tt} ${tr('MIN', 'मिनट')}` : `−${-tt} ${tr('MIN', 'मिनट')}`)
    }
    // mode stays an English token (used for CSS class); translated at render.
    const mode = t <= -m.lag ? 'OBSERVED' : t <= 0 ? 'RADAR LAG · EST' : 'FORECAST'
    setBadge((old) => (old.time === timeLabel && old.mode === mode ? old : { time: timeLabel, mode }))
    const track = trackRef.current
    if (track) {
      const pct = ((t - m.tMin) / (60 - m.tMin)) * 100
      track.style.setProperty('--pos', `${pct}%`)
    }
  }

  // hi-DPI responsive backing store: match the canvas buffer to its CSS size
  // × devicePixelRatio so it renders pixel-sharp on mobile and retina screens
  useEffect(() => {
    const cv = canvasRef.current
    if (!cv || typeof ResizeObserver === 'undefined') return
    const fit = () => {
      const rect = cv.getBoundingClientRect()
      if (!rect.width) return
      const dpr = Math.min(2.5, window.devicePixelRatio || 1)
      const px = Math.max(320, Math.round(rect.width * dpr))
      if (cv.width !== px) { cv.width = px; cv.height = px }
    }
    fit()
    const ro = new ResizeObserver(fit)
    ro.observe(cv)
    return () => ro.disconnect()
  }, [scene])

  // playback loop
  useEffect(() => {
    if (!model) return
    if (tRef.current == null) tRef.current = model.tMin
    let raf, last = performance.now()
    const loop = (now) => {
      const dt = now - last; last = now
      if (playingRef.current) {
        tRef.current += dt * 0.014           // ~14 timeline-min per second
        if (tRef.current > 60) tRef.current = model.tMin
      }
      drawFrame(tRef.current)
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [model]) // eslint-disable-line react-hooks/exhaustive-deps

  const seek = (e) => {
    const m = model
    const el = trackRef.current
    if (!m || !el) return
    const r = el.getBoundingClientRect()
    const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width))
    tRef.current = m.tMin + frac * (60 - m.tMin)
    playingRef.current = false; setPlaying(false)
  }

  if (error) return <p className="nc-forecast-note">{error}</p>
  if (!scene) return <p className="nc-forecast-note">{t('Loading radar scene…', 'रडार दृश्य लोड हो रहा है…')}</p>

  const m = model
  const modeLabel = (mode) => (
    mode === 'FORECAST' ? t('FORECAST', 'पूर्वानुमान')
    : mode === 'OBSERVED' ? t('OBSERVED', 'देखा गया')
    : t('RADAR LAG · EST', 'रडार देरी · अनुमान')
  )
  const jumpTargets = [
    ...m.history.map((h) => ({ t: h.t, label: h.timeIst })),
    { t: 0, label: t('Now', 'अभी') },
    ...[15, 30, 45, 60].map((x) => ({ t: x, label: `+${x}${t('m', 'मि')}` })),
  ]
  const etaNum = Number(highlightEta)
  const etaPct = Number.isFinite(etaNum) && etaNum >= 0 && etaNum <= 60
    ? ((etaNum - m.tMin) / (60 - m.tMin)) * 100 : null

  return (
    <>
      <div className="nc-forecast-stage rsp-stage">
        <canvas ref={canvasRef} width={480} height={480} className="rsp-canvas" aria-label={t('Radar animation', 'रडार एनिमेशन')} />
        <span className="rsp-badge rsp-badge--time">{badge.time}</span>
        <span className={`rsp-badge rsp-badge--mode rsp-mode-${badge.mode === 'FORECAST' ? 'fc' : badge.mode === 'OBSERVED' ? 'obs' : 'est'}`}>
          {modeLabel(badge.mode)}
        </span>
        <button
          type="button"
          className="nc-forecast-playbtn"
          onClick={() => { playingRef.current = !playingRef.current; setPlaying(playingRef.current) }}
          aria-label={playing ? t('Pause animation', 'एनिमेशन रोकें') : t('Play animation', 'एनिमेशन चलाएँ')}
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
      <div
        ref={trackRef}
        className="rsp-track"
        onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); seek(e) }}
        onPointerMove={(e) => { if (e.buttons) seek(e) }}
        role="slider"
        aria-label={t('Timeline', 'समयरेखा')}
        tabIndex={0}
      >
        <div className="rsp-rail" style={{ '--zero': `${((0 - m.tMin) / (60 - m.tMin)) * 100}%` }} />
        {jumpTargets.map((j) => (
          <span
            key={j.label}
            className={`rsp-tick${j.t === 0 ? ' rsp-tick--zero' : ''}`}
            style={{ left: `${((j.t - m.tMin) / (60 - m.tMin)) * 100}%` }}
          />
        ))}
        {etaPct != null && <span className="rsp-tick rsp-tick--eta" style={{ left: `${etaPct}%` }} title={t('Your ETA', 'आपकी पहुँच')} />}
        <div className="rsp-thumb" />
      </div>
      <div className="nc-forecast-scrub">
        {jumpTargets.filter((j) => j.t >= 0 || m.history.length <= 6).map((j) => (
          <button
            key={j.label}
            type="button"
            className="nc-forecast-dot"
            onClick={() => { tRef.current = j.t; playingRef.current = false; setPlaying(false) }}
          >
            {j.label}
          </button>
        ))}
      </div>
      <div className="rsp-legend" aria-label={t('Rain intensity colors', 'बारिश तीव्रता के रंग')}>
        {[
          ['#256cc7', t('Drizzle', 'बूँदाबाँदी')],
          ['#3ac8b2', t('Light', 'हल्की')],
          ['#f9d02e', t('Moderate', 'मध्यम')],
          ['#fb9c26', t('Heavy', 'तेज़')],
          ['#ee363e', t('Very heavy', 'बहुत तेज़')],
          ['#c846ff', t('Extreme', 'अत्यधिक')],
        ].map(([c, label]) => (
          <span key={label} className="rsp-legend__item">
            <span className="rsp-legend__chip" style={{ background: c }} />
            {label}
          </span>
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
  const t = useT()
  const { authHeaders } = useAuth()
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
      }, { headers: authHeaders() })
      localStorage.setItem('gb_alerts_endpoint', sub.endpoint)
      localStorage.setItem('gb_alerts_label', label || '')
      setStatus('enabled')
      setNote(label || null)
    } catch (e) {
      console.error('rain alerts enable failed:', e)
      setStatus('error')
      setNote(`${e?.name || tr('Error', 'त्रुटि')}: ${(e?.message || String(e)).slice(0, 140)}`)
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
          <span className="alerts-card__title">{t('🔔 Rain alerts', '🔔 बारिश अलर्ट')}</span>
          <span className="alerts-card__sub">
            {status === 'enabled'
              ? t(`Watching ${note || 'your saved location'} — you'll be notified when rain is here or up to ~105 min away.`, `${note || 'आपका सहेजा गया स्थान'} पर नज़र रखी जा रही है — जब यहाँ बारिश हो या ~105 मिनट दूर हो, आपको सूचित किया जाएगा।`)
              : status === 'denied'
                ? t('Notifications are blocked in your browser settings.', 'आपके ब्राउज़र सेटिंग्स में सूचनाएँ अवरुद्ध हैं।')
                : status === 'error'
                  ? (note ? t(`Could not enable alerts — ${note}`, `अलर्ट चालू नहीं हो सके — ${note}`) : t('Could not enable alerts — try again in a moment.', 'अलर्ट चालू नहीं हो सके — कुछ देर में फिर कोशिश करें।'))
                  : t('Get notified when rain reaches this location, or is up to ~105 min away (radar-based estimate).', 'जब बारिश इस स्थान पर पहुँचे या ~105 मिनट दूर हो, सूचना पाएँ (रडार-आधारित अनुमान)।')}
          </span>
        </div>
        {status === 'enabled' ? (
          <button type="button" className="alerts-card__btn alerts-card__btn--off" onClick={disable}>
            {t('Disable', 'बंद करें')}
          </button>
        ) : (
          <button
            type="button"
            className="alerts-card__btn"
            disabled={status === 'working' || lat == null || lon == null}
            onClick={enable}
          >
            {status === 'working' ? t('Enabling…', 'चालू हो रहा है…') : t('Enable', 'चालू करें')}
          </button>
        )}
      </div>
    </div>
  )
}

function JourneyStopCard({ stop, onClose }) {
  const t = useT()
  const [nc, setNc] = useState(null)
  const [ncErr, setNcErr] = useState(null)
  const [placeName, setPlaceName] = useState(null)

  useEffect(() => {
    let alive = true
    setNc(null); setNcErr(null); setPlaceName(null)
    axios.post(NOWCAST_URL, { lat: stop.lat, lon: stop.lon })
      .then((r) => { if (alive) setNc(r.data) })
      .catch(() => { if (alive) setNcErr(t('Nowcast unavailable for this point right now.', 'इस स्थान के लिए तात्कालिक पूर्वानुमान अभी उपलब्ध नहीं है।')) })
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
          <div className="nc-section-label">{t('RAIN STOP', 'बारिश पड़ाव')} · {stop.label ? tRainLabel(stop.label).toUpperCase() : t('RAIN', 'बारिश')}</div>
          <div className="journey-stop-card__place">
            {placeName || `${stop.lat.toFixed(3)}, ${stop.lon.toFixed(3)}`}
          </div>
          <div className="journey-stop-card__meta">
            {t(`You arrive here ~${etaRounded} min into the trip`, `आप सफ़र में ~${etaRounded} मिनट पर यहाँ पहुँचेंगे`)} · {toIST(stop.eta_mins)} {t('IST', 'IST')}
          </div>
        </div>
        <button type="button" className="journey-chip__close" onClick={onClose} aria-label={t('Close', 'बंद करें')}>
          ×
        </button>
      </div>

      {ncErr && <p className="nc-forecast-note">{ncErr}</p>}
      {!nc && !ncErr && <p className="nc-forecast-note">{t('Scanning radar at this point…', 'इस स्थान पर रडार स्कैन हो रहा है…')}</p>}
      {nc && nc.in_radar_bounds && (
        <>
          <div className={`banner banner--${(nc.rain_slots ?? 0) > 0 ? 'rain' : 'clear'}`} style={{ marginTop: 10 }}>
            <p className="banner__head">{nc.summary}</p>
          </div>
          <NowcastSlots slots={nc.slots} />
        </>
      )}
      {nc && !nc.in_radar_bounds && (
        <p className="nc-forecast-note">{t('This point is outside radar coverage.', 'यह स्थान रडार कवरेज के बाहर है।')}</p>
      )}

      <div className="nc-section-label" style={{ marginTop: 14 }}>
        {t('FORECAST RADAR AT THIS POINT', 'इस स्थान पर पूर्वानुमान रडार')}
        {etaRounded <= 60 ? t(' · YOUR ETA FRAME MARKED', ' · आपकी पहुँच का फ्रेम चिह्नित') : ''}
      </div>
      <RadarScenePlayer
        lat={stop.lat}
        lon={stop.lon}
        requestId={stop.requestId}
        highlightEta={stop.eta_mins}
      />
      <p className="nc-forecast-note">
        {etaRounded <= 60
          ? t('The frame marked ETA shows the predicted radar at the time you reach this point.', 'पहुँच वाला फ्रेम दिखाता है कि जब आप यहाँ पहुँचेंगे तब रडार का अनुमान क्या होगा।')
          : t('Your arrival here is beyond the 1-hour animation window.', 'यहाँ आपकी पहुँच 1-घंटे की एनिमेशन सीमा से आगे है।')}
      </p>
    </div>
  )
}

function NowcastPage({ userLoc, activeTab, onChangeTab, onPickSaved, pendingLoc, onPendingConsumed }) {
  const t = useT()
  const [ncLat, setNcLat] = useState(null)
  const [ncLon, setNcLon] = useState(null)
  const [ncName, setNcName] = useState('')
  const [isMyLoc, setIsMyLoc] = useState(false)

  // A saved place picked from the SavedMenu (in any tab) lands here.
  useEffect(() => {
    if (!pendingLoc) return
    setNcLat(pendingLoc.lat); setNcLon(pendingLoc.lon)
    setNcName(pendingLoc.label || t('Saved place', 'सहेजा गया स्थान'))
    setIsMyLoc(false); setNcResult(null); setNcError(null)
    onPendingConsumed && onPendingConsumed()
  }, [pendingLoc])

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
  const [refreshing, setRefreshing] = useState(false)
  const [refreshMsg, setRefreshMsg] = useState('')      // one-shot "new frame" / "already latest"
  const [cooldownLeft, setCooldownLeft] = useState(0)   // seconds until button re-enables
  const { user, authHeaders } = useAuth()
  const [saveState, setSaveState] = useState('idle')    // idle | saving | saved | error

  // Reset the save button whenever the selected location changes.
  useEffect(() => { setSaveState('idle') }, [ncLat, ncLon])

  async function saveThisLocation() {
    if (ncLat == null || ncLon == null) return
    setSaveState('saving')
    try {
      const label = (ncName || `${ncLat.toFixed(3)}, ${ncLon.toFixed(3)}`).slice(0, 60)
      await axios.post(`${API_BASE}/locations`,
        { label, lat: ncLat, lon: ncLon }, { headers: authHeaders() })
      setSaveState('saved')
    } catch {
      setSaveState('error')
    }
  }

  useEffect(() => {
    if (userLoc && !ncLat) {
      setNcLat(userLoc.lat)
      setNcLon(userLoc.lon)
      setNcName(t('My Location', 'मेरा स्थान'))
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
        setNcSuggestions(await searchPlaces(q, ac.signal))
      } catch (e) {
        if (e?.name !== 'CanceledError' && e?.name !== 'AbortError') setNcSuggestions([])
      }
    }, 350)
    return () => { if (ncDebounceRef.current) clearTimeout(ncDebounceRef.current) }
  }, [ncSearchQuery])

  function useMyLocation() {
    if (!userLoc) return
    setNcLat(userLoc.lat)
    setNcLon(userLoc.lon)
    setNcName(t('My Location', 'मेरा स्थान'))
    setIsMyLoc(true)
    setNcSearchQuery('')
    setNcSuggestions([])
    setNcResult(null)
    setNcError(null)
  }

  async function handleScan() {
    if (!ncLat || !ncLon) { setNcError(t('Please select a location first.', 'कृपया पहले एक स्थान चुनें।')); return }
    setNcError(null)
    setNcLoading(true)
    setNcResult(null)
    setForecastGif(null)
    setRefreshMsg('')
    setNcScanStatus(t('Scanning radar…', 'रडार स्कैन हो रहा है…'))
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
      const detail = e?.response?.data?.detail || e?.message || t('Something went wrong.', 'कुछ गड़बड़ हो गई।')
      setNcError(typeof detail === 'string' ? detail.slice(0, 300) : t('Nowcast failed.', 'तात्कालिक पूर्वानुमान विफल रहा।'))
    } finally {
      setNcLoading(false)
      setNcScanStatus('')
    }
  }

  // Cooldown ticker: IMD publishes ~every 10 min, so gate the refresh button
  // for 60 s after each check to avoid pointless full-pipeline re-runs.
  const REFRESH_COOLDOWN_SEC = 60
  useEffect(() => {
    if (cooldownLeft <= 0) return
    const id = setInterval(() => setCooldownLeft((s) => Math.max(0, s - 1)), 1000)
    return () => clearInterval(id)
  }, [cooldownLeft])

  async function handleRefreshFrame() {
    if (!ncLat || !ncLon || refreshing || cooldownLeft > 0) return
    setRefreshing(true)
    setRefreshMsg('')
    try {
      const res = await postWithWarmup(
        `${API_BASE}/radar/refresh`,
        { lat: ncLat, lon: ncLon },
        {},
        (msg) => setRefreshMsg(msg),
      )
      const newFrame = !!res.data?.new_frame
      if (newFrame) {
        setRefreshMsg(t('New radar frame arrived — updating…', 'नया रडार फ्रेम आया — अपडेट हो रहा है…'))
        await handleScan()
        setRefreshMsg(t('Updated to the newest radar frame.', 'नवीनतम रडार फ्रेम पर अपडेट हो गया।'))
      } else {
        setRefreshMsg(t('Already the latest frame — IMD refreshes about every 10 min.', 'पहले से नवीनतम फ्रेम — IMD लगभग हर 10 मिनट में अपडेट करता है।'))
      }
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || t('Refresh failed.', 'रिफ्रेश विफल रहा।')
      setRefreshMsg(typeof detail === 'string' ? detail.slice(0, 200) : t('Refresh failed.', 'रिफ्रेश विफल रहा।'))
    } finally {
      setRefreshing(false)
      setCooldownLeft(REFRESH_COOLDOWN_SEC)
    }
  }

  const hasLocation = ncLat != null && ncLon != null

  return (
    <div className="pg-nowcast">
      <nav className="nav">
        <span className="nav__brand">GARAJ BARAS</span>
        <span className="nav__right">
          <span className="nav__live" aria-hidden>
            <span className="nav__live-dot" />
            {t('LIVE', 'लाइव')}
          </span>
          <LangToggle />
          <AccountButton />
          <SavedMenu
            apiBase={API_BASE}
            onPick={onPickSaved}
            currentLoc={ncLat != null && ncLon != null
              ? { label: ncName, lat: ncLat, lon: ncLon } : null}
          />
        </span>
      </nav>

      <TabBar activeTab={activeTab} onChangeTab={onChangeTab} />

      <div className="nc-card">
        <div className="nc-section-label">{t('SCAN LOCATION', 'स्कैन स्थान')}</div>

        {/* Current selected location display */}
        {hasLocation && (
          <div className="nc-loc-display">
            <svg className="nc-loc-icon" viewBox="0 0 20 20" fill="none" aria-hidden>
              <circle cx="10" cy="9" r="3" stroke="currentColor" strokeWidth="1.8" />
              <path d="M10 2C6.13 2 3 5.13 3 9c0 5.25 7 11 7 11s7-5.75 7-11c0-3.87-3.13-7-7-7z"
                stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
            </svg>
            <div className="nc-loc-text">
              <span className="nc-loc-name">{ncName || t('Selected Location', 'चयनित स्थान')}</span>
              <span className="nc-loc-coords">
                {Number(ncLat).toFixed(4)}°N, {Number(ncLon).toFixed(4)}°E
              </span>
            </div>
            {userLoc && !isMyLoc && (
              <button className="nc-use-me-btn" type="button" onClick={useMyLocation}>
                {t('Use me', 'मुझे चुनें')}
              </button>
            )}
          </div>
        )}

        {/* Location search */}
        <div className="typeahead-wrap" style={{ marginTop: hasLocation ? 12 : 0 }}>
          <div className="rf-shell">
            <input
              className="rf-input"
              placeholder={hasLocation ? t('Search a different location…', 'कोई और स्थान खोजें…') : t('Search location…', 'स्थान खोजें…')}
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
            {t('Use my current location', 'मेरा वर्तमान स्थान उपयोग करें')}
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
              {ncScanStatus || t('Scanning…', 'स्कैन हो रहा है…')}
            </>
          ) : (
            <>
              {t('Scan Next 2 Hours', 'अगले 2 घंटे स्कैन करें')}
              <svg className="scan-btn__icon" viewBox="0 0 20 20" fill="none" aria-hidden>
                <path d="M4 10h12M11 5l5 5-5 5" stroke="currentColor" strokeWidth="2.2"
                  strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </>
          )}
        </button>

        {/* Quick-save the selected location (signed-in only) */}
        {hasLocation && user && (
          <button
            className="nc-save-loc-btn"
            type="button"
            onClick={saveThisLocation}
            disabled={saveState === 'saving' || saveState === 'saved'}
          >
            {saveState === 'saved' ? t('✓ Added to your saved places', '✓ आपके सहेजे गए स्थानों में जोड़ा गया')
              : saveState === 'saving' ? t('Saving…', 'सहेजा जा रहा है…')
              : saveState === 'error' ? t('Couldn’t save — tap to retry', 'सहेजा नहीं जा सका — फिर कोशिश करें')
              : t('＋ Add to your saved locations', '＋ अपने सहेजे गए स्थानों में जोड़ें')}
          </button>
        )}
      </div>

      {/* Signed-in extra: rain alerts (nowcast itself stays free; saved
          places live in the ⋮ menu in the top nav). */}
      {hasLocation && (
        <SignInGate
          title={t('Sign in for rain alerts & saved places', 'बारिश अलर्ट और सहेजे गए स्थानों के लिए साइन इन करें')}
        >
          <RainAlertsCard lat={ncLat} lon={ncLon} label={ncName || null} />
        </SignInGate>
      )}

      {ncError && (
        <div className="error-toast" role="alert" aria-live="polite">
          <span className="error-toast__icon" aria-hidden>!</span>
          <div>
            <div className="error-toast__title">{t('Scan failed', 'स्कैन विफल')}</div>
            <div className="error-toast__body">{ncError}</div>
          </div>
        </div>
      )}

      {ncResult && !ncLoading && (
        <div className="nc-results">

          {!ncResult.in_radar_bounds ? (
            <div className="banner banner--dying">
              <p className="banner__head">{t('Outside radar coverage', 'रडार कवरेज के बाहर')}</p>
              <p className="banner__sub">
                {t('This location is outside IMD radar coverage. Try a location in Delhi NCR or Uttar Pradesh.', 'यह स्थान IMD रडार कवरेज के बाहर है। दिल्ली NCR या उत्तर प्रदेश में कोई स्थान आज़माएँ।')}
              </p>
            </div>
          ) : (
            <>
              <div className={`banner banner--${(ncResult.rain_slots ?? 0) > 0 ? 'rain' : 'clear'}`}>
                <p className="banner__head">{ncResult.summary}</p>
              </div>

              <div className="nc-refresh-row">
                <span className="nc-refresh-age">
                  {t('Radar as of', 'रडार समय')} {ncResult.radar_as_of || t('unknown', 'अज्ञात')}
                  {ncResult.lag_mins != null ? ` · ${Math.round(ncResult.lag_mins)} ${t('min ago', 'मिनट पहले')}` : ''}
                </span>
                <button
                  type="button"
                  className="nc-refresh-btn"
                  disabled={refreshing || cooldownLeft > 0}
                  onClick={handleRefreshFrame}
                >
                  {refreshing
                    ? t('Checking…', 'जाँचा जा रहा है…')
                    : cooldownLeft > 0
                      ? t(`Check again in ${cooldownLeft}s`, `${cooldownLeft}से में फिर जाँचें`)
                      : t('↻ Check for new frame', '↻ नया फ्रेम जाँचें')}
                </button>
              </div>
              {refreshMsg && <p className="nc-refresh-msg">{refreshMsg}</p>}

              <NowcastSlots slots={ncResult.slots} />
              {forecastGif && (
                <div className="nc-forecast-card">
                  <div className="nc-section-label">{t('FORECAST RADAR · NEXT 1 HOUR', 'पूर्वानुमान रडार · अगला 1 घंटा')}</div>
                  <RadarScenePlayer
                    lat={forecastGif.lat}
                    lon={forecastGif.lon}
                    requestId={forecastGif.requestId}
                  />
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
function fmtDuration(mins) {
  if (mins == null || !Number.isFinite(Number(mins))) return '—'
  const total = Math.round(Number(mins))
  if (total < 60) return `${total} ${tr('min', 'मिनट')}`
  const h = Math.floor(total / 60)
  const m = total % 60
  return m === 0 ? `${h} ${tr('h', 'घं')}` : `${h} ${tr('h', 'घं')} ${m} ${tr('min', 'मिनट')}`
}

// Pings /health on app load. If the server hasn't answered within ~3 s it is
// almost certainly cold-booting on Render, so set expectations up front with
// a friendly note instead of letting the first scan feel broken. Hides itself
// the moment the server answers; shown at most once per app load.
function ServerWakeNote() {
  const t = useT()
  const [waking, setWaking] = useState(false)
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    let alive = true
    const timer = setTimeout(() => { if (alive) setWaking(true) }, 3000)
    axios.get(`${API_BASE}/health`, { timeout: 120000 })
      .catch(() => {})
      .finally(() => {
        if (!alive) return
        clearTimeout(timer)
        setWaking(false)
      })
    return () => { alive = false; clearTimeout(timer) }
  }, [])

  if (!waking || dismissed) return null

  return (
    <div className="wake-note" role="status" aria-live="polite">
      <span className="wake-note__spinner" aria-hidden />
      <span className="wake-note__text">
        <strong>{t('Radar server is waking up.', 'रडार सर्वर जाग रहा है।')}</strong> {t('Your first scan can take up to a minute — after that everything is fast.', 'आपका पहला स्कैन एक मिनट तक ले सकता है — उसके बाद सब कुछ तेज़ है।')}
      </span>
      <button
        type="button"
        className="wake-note__close"
        onClick={() => setDismissed(true)}
        aria-label={t('Dismiss', 'हटाएँ')}
      >
        ×
      </button>
    </div>
  )
}

function InstallPrompt() {
  // Install banner temporarily disabled — will revisit later. All the logic
  // below is intact; remove this early return to re-enable the popup.
  return null

  // eslint-disable-next-line no-unreachable
  const [deferredPrompt, setDeferredPrompt] = useState(null)
  const [visible, setVisible] = useState(false)
  const [helpVisible, setHelpVisible] = useState(false)

  useEffect(() => {
    if (localStorage.getItem('gb_install_dismissed')) return
    if (window.matchMedia('(display-mode: standalone)').matches) return // already installed
    const onPrompt = (e) => {
      e.preventDefault()
      setDeferredPrompt(e)
      setVisible(true)
    }
    const onInstalled = () => { setVisible(false); setDeferredPrompt(null); setHelpVisible(false) }
    window.addEventListener('beforeinstallprompt', onPrompt)
    window.addEventListener('appinstalled', onInstalled)
    return () => {
      window.removeEventListener('beforeinstallprompt', onPrompt)
      window.removeEventListener('appinstalled', onInstalled)
    }
  }, [])

  const dismiss = () => {
    setVisible(false)
    setHelpVisible(false)
    localStorage.setItem('gb_install_dismissed', '1')
  }

  const install = async () => {
    const ev = deferredPrompt
    if (!ev) { setHelpVisible(true); return }
    try {
      // prompt() is single-use and can be rejected (stale event, browser
      // heuristics). Await it so BOTH sync throws and async rejections land
      // in the catch instead of silently doing nothing.
      await ev.prompt()
      const choice = await ev.userChoice.catch(() => null)
      setDeferredPrompt(null)
      if (choice?.outcome === 'accepted') {
        setVisible(false)
      }
      // dismissed → keep the banner; a fresh beforeinstallprompt may re-arm it
    } catch (err) {
      console.warn('PWA install prompt failed:', err)
      setDeferredPrompt(null)
      setHelpVisible(true) // fall back to manual instructions
    }
  }

  if (helpVisible) {
    const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent)
    return (
      <div className="install-banner" role="dialog" aria-label="How to install">
        <img src="/icon-192.png" alt="" className="install-banner__icon" />
        <div className="install-banner__text">
          <strong>Install manually</strong>
          <span>
            {isIOS
              ? 'In Safari: tap Share (□↑) → “Add to Home Screen”.'
              : 'In the browser menu (⋮): tap “Install app” or “Add to Home screen”.'}
          </span>
        </div>
        <button className="install-banner__close" onClick={dismiss} aria-label="Dismiss">×</button>
      </div>
    )
  }

  if (!visible || !deferredPrompt) return null

  return (
    <div className="install-banner" role="dialog" aria-label="Install app">
      <img src="/icon-192.png" alt="" className="install-banner__icon" />
      <div className="install-banner__text">
        <strong>Install Garaj Baras</strong>
        <span>Get rain alerts &amp; forecasts from your home screen</span>
      </div>
      <button className="install-banner__btn" onClick={install}>Install</button>
      <button className="install-banner__close" onClick={dismiss} aria-label="Dismiss">×</button>
    </div>
  )
}

export default function App() {
  const t = useT()
  const [activeTab, setActiveTab] = useState('route')
  // Tabs mount on first visit and then stay mounted (hidden with CSS) so
  // their state survives tab switches. Route mounts immediately (default tab).
  const [visitedTabs, setVisitedTabs] = useState({ route: true, nowcast: false, chat: false, 'india-radar': false })
  const [pendingNcLoc, setPendingNcLoc] = useState(null)  // saved place → Nowcast

  // First-run onboarding: shown until dismissed once; re-openable from the
  // "How it works" link on the planner screen.
  const [showOnboarding, setShowOnboarding] = useState(() => {
    try { return !localStorage.getItem(ONBOARDING_KEY) } catch { return false }
  })

  const [source, setSource] = useState('')
  const [destination, setDestination] = useState('')
  const [avgSpeedKmh, setAvgSpeedKmh] = useState('')
  const [tripInputMode, setTripInputMode] = useState('speed') // 'speed' (km/h) | 'time' (journey minutes)
  const [journeyMins, setJourneyMins] = useState('')
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

  // ── Live journey mode ──
  const [liveActive, setLiveActive] = useState(false)
  const [livePos, setLivePos] = useState(null)
  const sampledRef = useRef([])          // original sampled waypoints (with cumKm)
  const routeLonLatRef = useRef(null)    // ORS geometry ([lon,lat]) for recoloring
  const liveSpeedRef = useRef(null)      // planned avg speed (km/h)
  const [routeSteps, setRouteSteps] = useState([]) // ORS turn-by-turn maneuvers
  const [routeFog, setRouteFog] = useState(null)   // Open-Meteo visibility along the route
  const [approachLeg, setApproachLeg] = useState(null) // {coords, steps, name}: drive to the route start first
  const [navStarting, setNavStarting] = useState(false)
  const [showSteps, setShowSteps] = useState(false)
  const [shareNote, setShareNote] = useState(null)

  const reverseAbortRef = useRef(null)
  const reverseCacheRef = useRef(new Map())

  const routeName = useMemo(() => toCityRouteName(source, destination), [source, destination])

  // Live pre-scan estimate from approximate road distance (straight-line × 1.3):
  // speed mode → estimated journey time; time mode → estimated avg speed.
  const plannerEstimate = useMemo(() => {
    if (!sourcePlace || !destPlace) return null
    const straight = haversine(sourcePlace.lat, sourcePlace.lon, destPlace.lat, destPlace.lon)
    const roadKm = straight * 1.3
    if (!(roadKm > 0)) return null
    if (tripInputMode === 'speed') {
      const spd = Number(avgSpeedKmh)
      if (!Number.isFinite(spd) || spd <= 0) return null
      return { km: roadKm, mins: (roadKm / spd) * 60, kmh: spd }
    }
    const mins = Number(journeyMins)
    if (!Number.isFinite(mins) || mins <= 0) return null
    return { km: roadKm, mins, kmh: roadKm / (mins / 60) }
  }, [sourcePlace, destPlace, avgSpeedKmh, journeyMins, tripInputMode])

  // App-load /health warmup lives in <ServerWakeNote /> (it both warms the
  // backend and surfaces a "waking up" note when the cold boot is slow).

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
        setSourceSug(await searchPlaces(q, ac.signal))
      } catch (e) {
        if (e?.name !== 'CanceledError' && e?.name !== 'AbortError') setSourceSug([])
      }
    }, 350)
    return () => { if (sourceDebounceRef.current) clearTimeout(sourceDebounceRef.current) }
  }, [source])

  useEffect(() => {
    const q = String(destination || '').trim()
    if (destAbortRef.current) destAbortRef.current.abort()
    if (destDebounceRef.current) clearTimeout(destDebounceRef.current)
    if (q.length < 3) { setDestSug([]); return }
    destDebounceRef.current = setTimeout(async () => {
      const ac = new AbortController()
      destAbortRef.current = ac
      try {
        setDestSug(await searchPlaces(q, ac.signal))
      } catch (e) {
        if (e?.name !== 'CanceledError' && e?.name !== 'AbortError') setDestSug([])
      }
    }, 350)
    return () => { if (destDebounceRef.current) clearTimeout(destDebounceRef.current) }
  }, [destination])

  async function handlePredict() {
    const startCity = source.trim()
    const endCity = destination.trim()
    let speedNum = Number(avgSpeedKmh)
    const journeyMinsNum = Number(journeyMins)
    if (!startCity || !endCity) { setError(t('Please enter both Source and Destination city names.', 'कृपया शुरुआत और मंज़िल दोनों शहरों के नाम दर्ज करें।')); return }
    if (tripInputMode === 'speed') {
      if (!Number.isFinite(speedNum) || speedNum <= 0) { setError(t('Please enter a valid average speed (km/h).', 'कृपया एक वैध औसत गति (किमी/घंटा) दर्ज करें।')); return }
    } else {
      if (!Number.isFinite(journeyMinsNum) || journeyMinsNum <= 0) { setError(t('Please enter a valid journey time (minutes).', 'कृपया एक वैध सफ़र समय (मिनट) दर्ज करें।')); return }
    }
    if (!ORS_KEY) { setError(t('Missing ORS API key. Set `VITE_ORS_API_KEY` in frontend/.env.', 'ORS API key नहीं है। frontend/.env में `VITE_ORS_API_KEY` सेट करें।')); return }

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
      let steps = []
      try {
        const ors = await fetchOrsRoute(start, end)
        routeLonLat = ors.lonLat
        steps = ors.steps
      } catch {
        routeLonLat = [[start.lon, start.lat], [end.lon, end.lat]]
      }

      if (!Array.isArray(routeLonLat) || routeLonLat.length < 2) {
        throw new Error(t('Route planning failed (ORS returned empty geometry).', 'रास्ता योजना विफल (ORS ने खाली ज्यामिति लौटाई)।'))
      }

      let totalKm = 0
      for (let i = 1; i < routeLonLat.length; i++) {
        const [lon1, lat1] = routeLonLat[i - 1]
        const [lon2, lat2] = routeLonLat[i]
        totalKm += haversine(lat1, lon1, lat2, lon2)
      }
      setRouteDistanceKm(totalKm)

      // Time mode: derive the avg speed from the real road distance,
      // then the existing speed-based waypoint logic works unchanged.
      if (tripInputMode === 'time') {
        speedNum = totalKm / (journeyMinsNum / 60)
        if (!Number.isFinite(speedNum) || speedNum <= 0) {
          throw new Error(t('Could not derive speed from the journey time.', 'सफ़र समय से गति नहीं निकाली जा सकी।'))
        }
      }

      if ((totalKm / speedNum) * 60 > 180) {
        setShowLongJourneyModal(true)
        const proceed = await new Promise((resolve) => { longJourneyResolveRef.current = resolve })
        setShowLongJourneyModal(false)
        if (!proceed) { setLoading(false); setScanning(false); return }
      }

      const sampled = sampleRouteEvery5Min(routeLonLat, speedNum, 5)
      if (!sampled.length) throw new Error(t('Could not sample route into waypoints.', 'रास्ते को वेपॉइंट में विभाजित नहीं किया जा सका।'))

      sampledRef.current = sampled
      routeLonLatRef.current = routeLonLat
      liveSpeedRef.current = speedNum
      setLiveActive(false)
      setLivePos(null)
      setJourneyStop(null)
      setRouteSteps(steps)
      setRouteFog(null)
      setRouteCoords(routeLonLat.map(([lon, lat]) => [lat, lon]))
      setResult({
        total_waypoints: sampled.length, rain_waypoints: 0, clear_waypoints: sampled.length,
        first_rain_eta: null, first_rain_label: null, rain_direction_from: '—', rain_direction_to: '—',
        rain_speed_kmh: 0, radar_lag_mins: null, radar_freshness: 'pending',
        radar_message: t('Scanning radar…', 'रडार स्कैन हो रहा है…'), route_distance_km: totalKm, waypoints: [], _pending: true,
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
      const mergedWaypoints = predictWaypoints.map((wp, i) => ({
        ...wp,
        rainGroup: getRainGroupLabel(wp.label),
        rainColor: getRainColor(wp.label),
        _cumKm: Number.isFinite(sampled[i]?.cumKm) ? sampled[i].cumKm : null,
      }))

      setRouteSegments(buildColoredSegments(routeLonLat, mergedWaypoints))
      setResult({ ...predictRes.data, route_distance_km: totalKm, waypoints: mergedWaypoints })
      // fog is optional — never block or fail the route on it
      fetchRouteFog(mergedWaypoints)
        .then((fog) => { if (routeLonLatRef.current === routeLonLat) setRouteFog(fog) })
        .catch(() => {})
      if ((predictRes.data?.radar_lag_mins ?? 0) > 75) {
        setRadarDown(true)
      }
    } catch (e) {
      const status = e?.response?.status
      const detail = e?.response?.data?.detail || e?.response?.data?.error?.message || e?.response?.data?.message
      const isTimeout = e?.code === 'ECONNABORTED' || /timeout/i.test(e?.message || '')
      const isNetwork = !status && !e?.response
      const msg = (isTimeout || isNetwork)
        ? t("Couldn't reach the radar server. It may still be waking up — please try again in a minute.", 'रडार सर्वर तक नहीं पहुँच पाए। यह अभी भी जाग रहा हो सकता है — कृपया एक मिनट में फिर कोशिश करें।')
        : status
          ? t(`Request failed (${status}): ${detail || e?.message || 'Unknown error'}`, `अनुरोध विफल (${status}): ${detail || e?.message || 'अज्ञात त्रुटि'}`)
          : e?.message || t('Something went wrong while scanning the radar.', 'रडार स्कैन करते समय कुछ गड़बड़ हो गई।')
      setError(typeof msg === 'string' ? msg : t('Something went wrong.', 'कुछ गड़बड़ हो गई।'))
      setResult(null); setRouteCoords([]); setRouteSegments([]); setRouteDistanceKm(null)
    } finally {
      setLoading(false); setScanning(false); setScanStatus('')
    }
  }

  // Live journey re-prediction landed: re-merge waypoints (keeping their real
  // route distance) and recolor the map segments from the fresh radar picture.
  function applyLivePrediction(data) {
    const rawWps = Array.isArray(data?.waypoints) ? data.waypoints : []
    if (!rawWps.length || !routeLonLatRef.current) return
    const merged = rawWps.map((wp, i) => ({
      ...wp,
      rainGroup: getRainGroupLabel(wp.label),
      rainColor: getRainColor(wp.label),
      _cumKm: Number.isFinite(sampledRef.current[i]?.cumKm) ? sampledRef.current[i].cumKm : null,
    }))
    setRouteSegments(buildColoredSegments(routeLonLatRef.current, merged))
    setResult((prev) => ({
      ...(prev || {}),
      ...data,
      route_distance_km: prev?.route_distance_km ?? data?.route_distance_km,
      waypoints: merged,
    }))
  }

  function endLiveJourney() {
    setLiveActive(false)
    setLivePos(null)
    setApproachLeg(null)
  }

  // Start: like Google Maps, if the user isn't at the route's start yet, first
  // guide them there (approach leg), then hand over to the planned route.
  async function startNavigation() {
    if (navStarting || routeCoords.length < 2) return
    setNavStarting(true)
    let leg = null
    try {
      const pos = await new Promise((resolve, reject) => {
        if (!('geolocation' in navigator)) { reject(new Error('no geolocation')); return }
        navigator.geolocation.getCurrentPosition(resolve, reject, { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 })
      })
      const here = { lat: pos.coords.latitude, lon: pos.coords.longitude }
      const start = { lat: routeCoords[0][0], lon: routeCoords[0][1] }
      if (haversine(here.lat, here.lon, start.lat, start.lon) > 0.3) {
        const ors = await fetchOrsRoute(here, start)
        if (Array.isArray(ors.lonLat) && ors.lonLat.length >= 2) {
          leg = {
            coords: ors.lonLat.map(([lon, lat]) => [lat, lon]),
            steps: ors.steps,
            name: String(source || '').split(',')[0].trim() || tr('start', 'शुरुआत'),
          }
        }
      }
    } catch {
      // no GPS / no route to start → just start on the planned route
    }
    setApproachLeg(leg)
    setLivePos(null)
    setLiveActive(true)
    setNavStarting(false)
  }

  async function shareRoute() {
    const text = `${routeName}: ${fmtDuration(tripMinutes)}, ${shownDistanceKm != null ? shownDistanceKm.toFixed(1) : '—'} km. ${rainSummary?.text || ''}`.trim()
    try {
      if (navigator.share) await navigator.share({ title: 'Garaj Baras', text })
      else {
        await navigator.clipboard.writeText(text)
        setShareNote(tr('Copied to clipboard', 'क्लिपबोर्ड पर कॉपी किया'))
        setTimeout(() => setShareNote(null), 2000)
      }
    } catch { /* user cancelled */ }
  }

  // While navigating, refresh the fog picture every 20 min (visibility
  // forecasts are hourly, so this is plenty).
  useEffect(() => {
    if (!liveActive) return
    const id = setInterval(() => {
      fetchRouteFog(result?.waypoints || []).then(setRouteFog).catch(() => {})
    }, 20 * 60 * 1000)
    return () => clearInterval(id)
  }, [liveActive]) // eslint-disable-line react-hooks/exhaustive-deps

  // Lock page scroll behind the full-screen navigation view
  useEffect(() => {
    if (!liveActive) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = prev }
  }, [liveActive])

  const fogSummary = useMemo(() => {
    const zones = fogZones(routeFog)
    if (!zones.length) return null
    const km = zones.reduce((a, z) => a + Math.max(0.5, z.endKm - z.startKm), 0)
    const minVis = Math.min(...zones.map((z) => z.minVis))
    return { km, minVis }
  }, [routeFog])

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

  // Total journey time: prefer the last waypoint ETA, else distance ÷ avg speed.
  const tripMinutes = useMemo(() => {
    const etas = (result?.waypoints || [])
      .map((w) => Number(w?.eta_mins))
      .filter((n) => Number.isFinite(n))
    if (etas.length) return Math.max(...etas)
    if (tripInputMode === 'time') {
      const mins = Number(journeyMins)
      return Number.isFinite(mins) && mins > 0 ? mins : null
    }
    const spd = Number(avgSpeedKmh)
    if (shownDistanceKm != null && Number.isFinite(spd) && spd > 0) {
      return (shownDistanceKm / spd) * 60
    }
    return null
  }, [result, avgSpeedKmh, journeyMins, tripInputMode, shownDistanceKm])

  // One-line rain status for the route card
  const rainSummary = useMemo(() => {
    if (!result || result._pending) return null
    const tl = rainTimeline
    if (!tl || tl.tone === 'clear' || !tl.closest) return { tone: 'clear', text: tr('No rain on route', 'रास्ते में बारिश नहीं') }
    const firstEta = Number(tl.closest.startMin) || 0
    const lastEta = Number(tl.lastEta) || 0
    const label = `${tRainLabel(tl.closest.intensity || 'Light')} ${tr('rain', 'बारिश')}`
    if (firstEta <= 2 && tl.closest.endMin >= lastEta - 2.5) return { tone: 'rain', text: tr(`${label} for the whole trip`, `पूरे सफ़र ${label}`) }
    if (firstEta <= 2) return { tone: 'rain', text: tr(`${label} now · clears in ${Math.round(tl.closest.endMin)} min`, `अभी ${label} · ${Math.round(tl.closest.endMin)} मिनट में साफ`) }
    return { tone: 'rain', text: tr(`${label} in ${Math.round(firstEta)} min`, `${Math.round(firstEta)} मिनट में ${label}`) }
  }, [result, rainTimeline])

  // Google-style "via <road>": the named road with the most distance on it
  const viaName = useMemo(() => {
    const byName = new Map()
    for (const s of routeSteps) {
      const n = String(s?.name || '').trim()
      if (!n || n === '-') continue
      byName.set(n, (byName.get(n) || 0) + (Number(s.distance) || 0))
    }
    let best = null, bestD = 0
    for (const [n, d] of byName) if (d > bestD) { best = n; bestD = d }
    return best
  }, [routeSteps])

  const arrivalClock = tripMinutes != null
    ? new Date(Date.now() + tripMinutes * 60000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : null

  function handleBackToPlanner() {
    endLiveJourney()
    setResult(null); setError(null); setActiveSeg(null); setJourneyStop(null)
    setRouteCoords([]); setRouteSegments([]); setRouteDistanceKm(null); setShowBreakdown(false)
    setRadarDown(false); setRouteSteps([]); setRouteFog(null)
  }

  function handleTabChange(tab) {
    setActiveTab(tab)
    setVisitedTabs((v) => (v[tab] ? v : { ...v, [tab]: true }))
    setError(null)
  }

  // A saved place picked from the ⋮ menu (in any tab) → jump to Nowcast + load it.
  function onPickSaved(loc) {
    setPendingNcLoc(loc)
    handleTabChange('nowcast')
  }

  return (
    <div className="app">

      <InstallPrompt />
      <ServerWakeNote />

      {showOnboarding && <Onboarding onClose={() => setShowOnboarding(false)} />}

      {/* ── NOWCAST PAGE ──
          Tabs stay MOUNTED after their first visit and are hidden with CSS,
          so switching tabs never destroys their state (results, chat, etc.). */}
      {visitedTabs.nowcast && (
        <div style={{ display: activeTab === 'nowcast' ? '' : 'none' }}>
          <NowcastPage
            userLoc={userLoc}
            activeTab={activeTab}
            onChangeTab={handleTabChange}
            onPickSaved={onPickSaved}
            pendingLoc={pendingNcLoc}
            onPendingConsumed={() => setPendingNcLoc(null)}
          />
        </div>
      )}

      {visitedTabs['india-radar'] && (
        <div style={{ display: activeTab === 'india-radar' ? '' : 'none' }}>
          <IndiaRadarPage activeTab={activeTab} onChangeTab={handleTabChange} onPickSaved={onPickSaved} />
        </div>
      )}

      {/* ── ASK AI (CHAT) PAGE ── */}
      {visitedTabs.chat && (
        <div style={{ display: activeTab === 'chat' ? '' : 'none' }}>
          <ChatPage
            activeTab={activeTab}
            onChangeTab={handleTabChange}
            onPickSaved={onPickSaved}
          />
        </div>
      )}

      {radarDown && <RadarDownModal onClose={() => setRadarDown(false)} />}

      {showLongJourneyModal && (
        <LongJourneyModal
          onContinue={() => longJourneyResolveRef.current?.(true)}
          onDismiss={() => { longJourneyResolveRef.current?.(false); setShowLongJourneyModal(false) }}
        />
      )}

      {/* ── ROUTE TAB SCREENS ── */}
      <div style={{ display: activeTab === 'route' ? '' : 'none' }}>
          {/* PLANNER SCREEN */}
          {!loading && !result && (
            <div className="pg-planner">
              <nav className="nav">
                <span className="nav__brand">GARAJ BARAS</span>
                <span className="nav__right">
                  <span className="nav__live" aria-hidden>
                    <span className="nav__live-dot" />
                    {t('LIVE', 'लाइव')}
                  </span>
                  <LangToggle />
                  <AccountButton />
                  <SavedMenu apiBase={API_BASE} onPick={onPickSaved} />
                </span>
              </nav>

              <TabBar activeTab={activeTab} onChangeTab={handleTabChange} />

              <section className="hero">
                <div className="hero__glow" aria-hidden />
                <h1 className="hero__title">{t('Know the rain', 'बारिश जानें')}<br />{t('before you leave.', 'निकलने से पहले।')}</h1>
                <button
                  type="button"
                  className="hero__how"
                  onClick={() => setShowOnboarding(true)}
                >
                  <svg viewBox="0 0 20 20" fill="none" width="14" height="14" aria-hidden>
                    <circle cx="10" cy="10" r="8" stroke="currentColor" strokeWidth="1.8" />
                    <path d="M8 8a2 2 0 1 1 2.8 1.83c-.5.22-.8.62-.8 1.17v.3" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                    <circle cx="10" cy="14.2" r="1" fill="currentColor" />
                  </svg>
                  {t('How it works', 'यह कैसे काम करता है')}
                </button>
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
                      <label className="rf-label">{t('FROM', 'कहाँ से')}</label>
                      <div className="typeahead-wrap">
                        <div className="rf-shell">
                          <input
                            className="rf-input"
                            placeholder={t('Starting city', 'शुरुआती शहर')}
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
                                  setSource(t('My Location', 'मेरा स्थान'))
                                  setSourcePlace({ lat: userLoc.lat, lon: userLoc.lon, display_name: t('My Location', 'मेरा स्थान') })
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
                                  <div className="dropdown__primary">{t('My Location', 'मेरा स्थान')}</div>
                                  <div className="dropdown__secondary">{t('Use your current location', 'अपना वर्तमान स्थान उपयोग करें')}</div>
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
                      <label className="rf-label">{t('TO', 'कहाँ तक')}</label>
                      <div className="typeahead-wrap">
                        <div className="rf-shell">
                          <input
                            className="rf-input"
                            placeholder={t('Destination city', 'मंज़िल शहर')}
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

                {/* Speed / journey time (either one; backend works off speed) */}
                <div className="speed-field">
                  <div className="trip-mode-row">
                    <label className="rf-label">
                      {tripInputMode === 'speed' ? t('AVG SPEED', 'औसत गति') : t('JOURNEY TIME', 'सफ़र समय')}
                    </label>
                    <div className="trip-mode-toggle" role="tablist" aria-label={t('Input mode', 'इनपुट मोड')}>
                      <button
                        type="button"
                        className={`trip-mode-btn${tripInputMode === 'speed' ? ' is-active' : ''}`}
                        onClick={() => setTripInputMode('speed')}
                      >
                        {t('Speed', 'गति')}
                      </button>
                      <button
                        type="button"
                        className={`trip-mode-btn${tripInputMode === 'time' ? ' is-active' : ''}`}
                        onClick={() => setTripInputMode('time')}
                      >
                        {t('Time', 'समय')}
                      </button>
                    </div>
                  </div>
                  <div className="speed-row">
                    <div className="rf-shell rf-shell--speed">
                      {tripInputMode === 'speed' ? (
                        <input
                          className="rf-input"
                          inputMode="decimal"
                          placeholder="55"
                          value={avgSpeedKmh}
                          onChange={(e) => setAvgSpeedKmh(e.target.value)}
                        />
                      ) : (
                        <input
                          className="rf-input"
                          inputMode="decimal"
                          placeholder="90"
                          value={journeyMins}
                          onChange={(e) => setJourneyMins(e.target.value)}
                        />
                      )}
                    </div>
                    <span className="speed-unit">{tripInputMode === 'speed' ? t('km/h', 'किमी/घं') : t('min', 'मिनट')}</span>
                  </div>
                  {plannerEstimate && (
                    <p className="speed-estimate">
                      {tripInputMode === 'speed' ? (
                        <>
                          ≈ {fmtDuration(plannerEstimate.mins)} {t('journey', 'सफ़र')}
                          <span className="speed-estimate__dim"> · ~{Math.round(plannerEstimate.km)} {t('km at', 'किमी @')} {Math.round(plannerEstimate.kmh)} {t('km/h', 'किमी/घं')}</span>
                        </>
                      ) : (
                        <>
                          ≈ {Math.round(plannerEstimate.kmh)} {t('km/h avg speed', 'किमी/घं औसत गति')}
                          <span className="speed-estimate__dim"> · ~{Math.round(plannerEstimate.km)} {t('km in', 'किमी में')} {fmtDuration(plannerEstimate.mins)}</span>
                        </>
                      )}
                    </p>
                  )}
                </div>

                {/* Scan CTA */}
                <button
                  className="scan-btn"
                  type="button"
                  onClick={handlePredict}
                  disabled={
                    !source.trim() || !destination.trim() || loading ||
                    !(tripInputMode === 'speed' ? String(avgSpeedKmh).trim() : String(journeyMins).trim())
                  }
                >
                  {t('Scan My Route', 'मेरा रास्ता स्कैन करें')}
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
                    <div className="error-toast__title">{t('Scan failed', 'स्कैन विफल')}</div>
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
              <p className="loading-label">{t('SCANNING RADAR', 'रडार स्कैन हो रहा है')}</p>
              <p className="loading-sub">{t('Reading IMD frames · Mapping your route', 'IMD फ्रेम पढ़े जा रहे हैं · आपका रास्ता मैप हो रहा है')}</p>
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
                  {t('Back', 'वापस')}
                </button>
                <span className={`status-pill status-pill--${result._pending ? 'pending' : liveActive ? 'live' : hasRain ? 'rain' : 'clear'}`}>
                  {result._pending ? t('Scanning…', 'स्कैन हो रहा है…') : liveActive ? t('● LIVE', '● लाइव') : hasRain ? t('Rain ahead', 'आगे बारिश') : t('Clear skies', 'साफ आसमान')}
                </span>
              </nav>

              {/* Map */}
              <div className="map-wrap">
                <Suspense
                  fallback={
                    <div className="map-container">
                      <div style={{ height: '46vh', display: 'grid', placeItems: 'center', color: 'var(--text-secondary)' }}>
                        {t('Loading map…', 'नक्शा लोड हो रहा है…')}
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
                    livePos={null}
                    fog={routeFog}
                    height="46vh"
                  />
                </Suspense>
                {scanning && (
                  <div className="scan-overlay" role="status" aria-live="polite">
                    <span className="spinner" aria-hidden />
                    <span className="scan-overlay__label">{t('SCANNING RADAR…', 'रडार स्कैन हो रहा है…')}</span>
                    {scanStatus && scanStatus !== tr('Scanning radar…', 'रडार स्कैन हो रहा है…') && (
                      <span className="scan-overlay__sub">{scanStatus}</span>
                    )}
                  </div>
                )}
              </div>

              {/* Route card — Google Maps style summary + actions */}
              <section className="route-card" aria-label={t('Route summary', 'रास्ता सारांश')}>
                <div className="rc-places">
                  <div className="rc-place"><span className="rc-dot rc-dot--a" aria-hidden />{String(source || '').split(',')[0] || '—'}</div>
                  <div className="rc-place"><span className="rc-dot rc-dot--b" aria-hidden />{String(destination || '').split(',')[0] || '—'}</div>
                </div>

                <div className="rc-main">
                  <span className="rc-time">{result._pending ? t('Scanning…', 'स्कैन हो रहा है…') : fmtDuration(tripMinutes)}</span>
                  {shownDistanceKm != null && <span className="rc-dist">({shownDistanceKm.toFixed(1)} {t('km', 'किमी')})</span>}
                </div>
                <div className="rc-sub">
                  {arrivalClock && !result._pending && <span>{t('Arrive', 'पहुँच')} {arrivalClock}</span>}
                  {viaName && <span>{t('via', 'होकर')} {viaName}</span>}
                </div>

                {!result._pending && (
                  <div className="rc-chips">
                    {rainSummary && (
                      <span className={`rc-chip rc-chip--${rainSummary.tone}`}>
                        <span aria-hidden>{rainSummary.tone === 'clear' ? '☀' : '🌧'}</span> {rainSummary.text}
                      </span>
                    )}
                    <span className={`rc-chip rc-chip--${fogSummary ? 'fog' : 'muted'}`}>
                      <span aria-hidden>🌫</span>{' '}
                      {fogSummary
                        ? t(`Fog on ~${Math.round(fogSummary.km)} km · ${fmtVisibility(fogSummary.minVis)}`, `~${Math.round(fogSummary.km)} किमी पर कोहरा · ${fmtVisibility(fogSummary.minVis)}`)
                        : routeFog ? t('No fog', 'कोहरा नहीं') : t('Checking fog…', 'कोहरा जाँच रहे हैं…')}
                    </span>
                    {result.radar_message && <span className="rc-chip rc-chip--muted"><span aria-hidden>📡</span> {result.radar_message}</span>}
                  </div>
                )}

                <div className="rc-actions">
                  <button
                    type="button"
                    className="rc-btn rc-btn--primary"
                    onClick={startNavigation}
                    disabled={result._pending || routeCoords.length < 2 || navStarting}
                  >
                    {navStarting ? <span className="spinner spinner--sm" aria-hidden /> : <span aria-hidden>▲</span>}
                    {navStarting ? t('Locating…', 'स्थान खोज रहे हैं…') : t('Start', 'शुरू करें')}
                  </button>
                  <button type="button" className={`rc-btn${showSteps ? ' is-on' : ''}`} onClick={() => setShowSteps((v) => !v)} disabled={!routeSteps.length}>
                    <span aria-hidden>☰</span> {t('Steps', 'दिशाएँ')}
                  </button>
                  <button type="button" className="rc-btn" onClick={handlePredict} disabled={loading || scanning}>
                    <span aria-hidden>↻</span> {t('Refresh', 'ताज़ा करें')}
                  </button>
                  <button type="button" className="rc-btn" onClick={shareRoute} disabled={result._pending}>
                    <span aria-hidden>⤴</span> {shareNote || t('Share', 'साझा करें')}
                  </button>
                </div>

                {showSteps && routeSteps.length > 0 && (
                  <ol className="rc-steps">
                    {routeSteps.map((s, i) => (
                      <li key={`${s.wp}-${i}`}>
                        <span className="rc-steps__icon"><ManeuverIcon type={s.type} size={22} /></span>
                        <span className="rc-steps__text">
                          <span className="rc-steps__instr">{maneuverText(s)}</span>
                          {streetName(s) && <span className="rc-steps__street">{streetName(s)}</span>}
                        </span>
                        {s.distance > 0 && s.type !== 10 && <span className="rc-steps__dist">{fmtDist(s.distance / 1000)}</span>}
                      </li>
                    ))}
                  </ol>
                )}
              </section>

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
                      {rainTimeline.decayNote === 'dying' ? t('Patch fading', 'बादल कम हो रहा') : t('Weakening', 'कमज़ोर हो रहा')}
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

              {liveActive && !result._pending && routeCoords.length >= 2 && createPortal(
                <div className="nav-screen" role="dialog" aria-label={t('Navigation', 'नेविगेशन')}>
                  <Suspense fallback={<div className="nav-loading">{t('Loading map…', 'नक्शा लोड हो रहा है…')}</div>}>
                    {approachLeg ? (
                      <>
                        <RouteMap
                          key="nav-approach"
                          navMode
                          hideStartMarker
                          routeCoords={approachLeg.coords}
                          routeSegments={[]}
                          waypoints={[]}
                          activeSeg={null}
                          setActiveSeg={() => {}}
                          openSegmentPopup={() => {}}
                          livePos={livePos}
                          fog={null}
                        />
                        <LiveJourneyPanel
                          key="approach"
                          apiBase={API_BASE}
                          routeCoords={approachLeg.coords}
                          waypoints={[]}
                          plannedSpeedKmh={30}
                          steps={approachLeg.steps}
                          approach={{ name: approachLeg.name }}
                          onArrive={() => { setLivePos(null); setApproachLeg(null) }}
                          onLivePos={setLivePos}
                          onWaypointsUpdated={() => {}}
                          onEnd={endLiveJourney}
                        />
                      </>
                    ) : (
                      <>
                        <RouteMap
                          key="nav-main"
                          navMode
                          routeCoords={routeCoords}
                          routeSegments={routeSegments}
                          waypoints={result.waypoints || []}
                          activeSeg={activeSeg}
                          setActiveSeg={setActiveSeg}
                          openSegmentPopup={openSegmentPopup}
                          livePos={livePos}
                          fog={routeFog}
                        />
                        <LiveJourneyPanel
                          key="main"
                          apiBase={API_BASE}
                          routeCoords={routeCoords}
                          waypoints={result.waypoints || []}
                          plannedSpeedKmh={liveSpeedRef.current}
                          steps={routeSteps}
                          fog={routeFog}
                          onLivePos={setLivePos}
                          onWaypointsUpdated={applyLivePrediction}
                          onEnd={endLiveJourney}
                        />
                      </>
                    )}
                  </Suspense>
                </div>,
                document.body,
              )}

              {/* Rain-stop detail: nowcast + forecast radar at the tapped stop */}
              {journeyStop && !result._pending && (
                <JourneyStopCard
                  stop={journeyStop}
                  onClose={() => setJourneyStop(null)}
                />
              )}

              {/* Legend */}
              <div className="legend">
                <span className="legend__title">{t('Route colors', 'रास्ते के रंग')}</span>
                <div className="legend__chips">
                  {[
                    { cls: 'veryheavy', label: t('Very Heavy', 'बहुत तेज़') },
                    { cls: 'heavy',     label: t('Heavy', 'तेज़') },
                    { cls: 'moderate',  label: t('Moderate', 'मध्यम') },
                    { cls: 'light',     label: t('Light', 'हल्की') },
                    { cls: 'verylight', label: t('Very Light', 'बहुत हल्की') },
                    { cls: 'norain',    label: t('No Rain', 'बारिश नहीं') },
                    { cls: 'unknown',   label: t('Out of radar', 'रडार के बाहर') },
                  ].map(({ cls, label }) => (
                    <div key={cls} className="legend__chip">
                      <span className={`legend__swatch legend__swatch--${cls}`} aria-hidden />
                      <span className="legend__text">{label}</span>
                    </div>
                  ))}
                  <div className="legend__chip">
                    <span className="legend__swatch legend__swatch--fog" aria-hidden />
                    <span className="legend__text">{t('Fog (< 1 km visibility)', 'कोहरा (< 1 किमी दृश्यता)')}</span>
                  </div>
                </div>
              </div>
            </div>
          )}
      </div>
    </div>
  )
}
