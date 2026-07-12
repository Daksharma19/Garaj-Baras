import { useEffect, useMemo, useRef, useState } from 'react'
import axios from 'axios'

// ── Live Journey ──────────────────────────────────────────────────────────────
// Two clocks:
//  · Client clock (1 s tick): GPS position snapped to the route + dead-reckoned
//    between fixes drives a live "rain in ~N min" countdown. Pure arithmetic,
//    no network.
//  · Server clock (every REPREDICT_MS): recompute remaining-waypoint ETAs from
//    the observed rolling speed, force a radar frame check, and re-POST
//    /predict_waypoints so the whole picture (segments, timeline, countdown)
//    re-anchors to the newest radar data.

const REPREDICT_MS = 5 * 60 * 1000   // radar sync cadence (IMD publishes ~10 min)
const TICK_MS = 1000
const SPEED_EMA_ALPHA = 0.25         // rolling-speed smoothing
const MOVING_MIN_KMH = 3             // below this = standing still (no dead-reckoning)
const OFF_ROUTE_KM = 1.0             // farther than this from the route = off-route
const SHIFT_NOTE_MIN = 2             // countdown jump (min) that earns an "updated" note
const SIM_SPEEDUP = 12               // dev simulator: 12× planned speed

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = window.atob(base64)
  const arr = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i)
  return arr
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371
  const dLat = ((lat2 - lat1) * Math.PI) / 180
  const dLon = ((lon2 - lon1) * Math.PI) / 180
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLon / 2) ** 2
  return R * 2 * Math.asin(Math.sqrt(a))
}

function istClock(offsetMins = 0) {
  const ist = new Date(Date.now() + offsetMins * 60 * 1000 + 5.5 * 60 * 60 * 1000)
  return `${String(ist.getUTCHours()).padStart(2, '0')}:${String(ist.getUTCMinutes()).padStart(2, '0')}`
}

function fmtMins(mins) {
  const m = Math.max(1, Math.round(mins))
  if (m < 60) return `~${m} min`
  const h = Math.floor(m / 60)
  const r = m % 60
  return r === 0 ? `~${h} h` : `~${h} h ${r} min`
}

/** cumKm[i] = distance along routeCoords ([lat,lon]) up to vertex i */
function buildCumKm(routeCoords) {
  const cum = [0]
  let acc = 0
  for (let i = 1; i < routeCoords.length; i++) {
    acc += haversineKm(routeCoords[i - 1][0], routeCoords[i - 1][1], routeCoords[i][0], routeCoords[i][1])
    cum.push(acc)
  }
  return cum
}

/** Nearest point on the route polyline → { km along route, offKm from route } */
function snapToRoute(routeCoords, cumKm, lat, lon) {
  let bestOff = Infinity
  let bestKm = 0
  // Equirectangular projection around the fix — accurate enough at city scale
  // and ~30× cheaper than per-segment haversine projection.
  const cosLat = Math.cos((lat * Math.PI) / 180)
  const KM_LAT = 111.32
  const px = lon * cosLat * KM_LAT
  const py = lat * KM_LAT
  for (let i = 1; i < routeCoords.length; i++) {
    const ax = routeCoords[i - 1][1] * cosLat * KM_LAT
    const ay = routeCoords[i - 1][0] * KM_LAT
    const bx = routeCoords[i][1] * cosLat * KM_LAT
    const by = routeCoords[i][0] * KM_LAT
    const dx = bx - ax, dy = by - ay
    const len2 = dx * dx + dy * dy
    const t = len2 > 0 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2)) : 0
    const qx = ax + t * dx, qy = ay + t * dy
    const off = Math.hypot(px - qx, py - qy)
    if (off < bestOff) {
      bestOff = off
      bestKm = cumKm[i - 1] + (cumKm[i] - cumKm[i - 1]) * t
    }
  }
  return { km: bestKm, offKm: bestOff }
}

/** [lat, lon] on the route at km along it */
function pointAtKm(routeCoords, cumKm, km) {
  const target = Math.max(0, Math.min(cumKm[cumKm.length - 1], km))
  let lo = 0, hi = cumKm.length - 1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (cumKm[mid] < target) lo = mid + 1
    else hi = mid
  }
  const i = Math.max(1, lo)
  const span = cumKm[i] - cumKm[i - 1] || 1e-9
  const u = Math.max(0, Math.min(1, (target - cumKm[i - 1]) / span))
  return [
    routeCoords[i - 1][0] + u * (routeCoords[i][0] - routeCoords[i - 1][0]),
    routeCoords[i - 1][1] + u * (routeCoords[i][1] - routeCoords[i - 1][1]),
  ]
}

/**
 * The live countdown, from current progress + speed + predicted waypoints.
 * Returns one of:
 *  { kind: 'in_rain', label, endsMins|null }   endsMins null = rain to destination
 *  { kind: 'ahead',   label, mins, decay }
 *  { kind: 'clear' }
 */
function computeCountdown(waypoints, progressKm, speedKmh) {
  const wps = (waypoints || [])
    .filter((w) => Number.isFinite(w?._cumKm))
    .sort((a, b) => a._cumKm - b._cumKm)
  if (!wps.length || !(speedKmh > 0)) return null

  // Waypoint zone the user is currently inside = last wp at/behind them
  let curIdx = 0
  for (let i = 0; i < wps.length; i++) {
    if (wps[i]._cumKm <= progressKm + 0.05) curIdx = i
    else break
  }
  const toMins = (km) => ((km - progressKm) / speedKmh) * 60

  if (wps[curIdx].rain_expected) {
    let end = null
    for (let i = curIdx + 1; i < wps.length; i++) {
      if (!wps[i].rain_expected) { end = wps[i]; break }
    }
    return {
      kind: 'in_rain',
      label: wps[curIdx].label || 'Rain',
      endsMins: end ? toMins(end._cumKm) : null,
    }
  }
  for (let i = curIdx + 1; i < wps.length; i++) {
    if (wps[i].rain_expected) {
      return {
        kind: 'ahead',
        label: wps[i].label || 'Rain',
        mins: toMins(wps[i]._cumKm),
        decay: wps[i].decay_status || null,
      }
    }
  }
  return { kind: 'clear' }
}

export default function LiveJourneyPanel({
  apiBase,
  routeCoords,        // [[lat,lon], ...]
  waypoints,          // merged predictions incl. _cumKm (updated by App on re-predict)
  plannedSpeedKmh,
  onLivePos,          // ({lat, lon}) → App → RouteMap marker
  onWaypointsUpdated, // (rawResponseData) → App merges + recolors segments
  onEnd,
}) {
  const cumKm = useMemo(() => buildCumKm(routeCoords), [routeCoords])
  const totalKm = cumKm[cumKm.length - 1] || 0

  const [phase, setPhase] = useState('starting') // starting | live | geo_error
  const [geoMsg, setGeoMsg] = useState(null)
  const [countdown, setCountdown] = useState(null)
  const [speedKmh, setSpeedKmh] = useState(null)
  const [remainKm, setRemainKm] = useState(totalKm)
  const [offRoute, setOffRoute] = useState(false)
  const [arrived, setArrived] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [syncLeftSec, setSyncLeftSec] = useState(Math.round(REPREDICT_MS / 1000))
  const [updateNote, setUpdateNote] = useState(null)
  const [simOn, setSimOn] = useState(false)

  // refs = the live model (mutated by tick/fix handlers without re-renders)
  const fixRef = useRef(null)          // { snapKm, at(ms), lat, lon }
  const progressRef = useRef(0)
  const speedRef = useRef(null)        // rolling km/h (EMA), null until first estimate
  const prevRawFixRef = useRef(null)   // for derived speed when coords.speed is null
  const waypointsRef = useRef(waypoints)
  waypointsRef.current = waypoints
  const simRef = useRef(false)
  const nextSyncAtRef = useRef(Date.now() + REPREDICT_MS)
  const repredictingRef = useRef(false)
  const wakeLockRef = useRef(null)
  const aliveRef = useRef(true)
  const journeyIdRef = useRef(null)    // server-side journey (screen-off guardian)
  const endedRef = useRef(false)
  const [guardian, setGuardian] = useState('starting') // starting | on | off | denied

  const speedForEta = () =>
    speedRef.current != null && speedRef.current >= MOVING_MIN_KMH
      ? speedRef.current
      : Number(plannedSpeedKmh) > 0 ? Number(plannedSpeedKmh) : 40

  // ── GPS ────────────────────────────────────────────────────────────────
  useEffect(() => {
    aliveRef.current = true
    if (!('geolocation' in navigator)) {
      setPhase('geo_error')
      setGeoMsg('Location is not supported in this browser.')
      return
    }
    const watchId = navigator.geolocation.watchPosition(
      (pos) => {
        if (!aliveRef.current || simRef.current) return
        const lat = Number(pos.coords.latitude)
        const lon = Number(pos.coords.longitude)
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return
        const now = Date.now()

        // rolling speed: device speed if present, else derived from the last fix
        let kmhSample = null
        if (Number.isFinite(pos.coords.speed) && pos.coords.speed >= 0) {
          kmhSample = pos.coords.speed * 3.6
        } else if (prevRawFixRef.current) {
          const p = prevRawFixRef.current
          const dtH = (now - p.at) / 3600000
          if (dtH > 3 / 3600) kmhSample = haversineKm(p.lat, p.lon, lat, lon) / dtH
        }
        prevRawFixRef.current = { lat, lon, at: now }
        if (kmhSample != null && kmhSample < 160) {
          speedRef.current = speedRef.current == null
            ? kmhSample
            : speedRef.current + SPEED_EMA_ALPHA * (kmhSample - speedRef.current)
        }

        const snap = snapToRoute(routeCoords, cumKm, lat, lon)
        const off = snap.offKm > OFF_ROUTE_KM
        setOffRoute(off)
        if (!off) {
          // never snap backwards more than 300 m (GPS jitter at a standstill)
          const km = Math.max(snap.km, progressRef.current - 0.3)
          fixRef.current = { snapKm: km, at: now, lat, lon }
          progressRef.current = Math.max(progressRef.current, km)
        }
        setPhase('live')
      },
      (err) => {
        if (!aliveRef.current || fixRef.current) return
        setPhase('geo_error')
        setGeoMsg(err?.code === 1
          ? 'Location permission denied — allow it to track your journey.'
          : 'Could not get a GPS fix. Move somewhere with better signal.')
      },
      { enableHighAccuracy: true, maximumAge: 3000, timeout: 20000 },
    )
    return () => {
      aliveRef.current = false
      navigator.geolocation.clearWatch(watchId)
    }
  }, [routeCoords, cumKm])

  // ── Screen wake lock (best-effort) ─────────────────────────────────────
  useEffect(() => {
    let released = false
    const acquire = async () => {
      try {
        if ('wakeLock' in navigator && !released) {
          wakeLockRef.current = await navigator.wakeLock.request('screen')
        }
      } catch { /* low battery / unsupported — fine */ }
    }
    acquire()
    const onVis = () => {
      if (document.visibilityState === 'visible') {
        acquire()
        // catch up if a sync was missed while backgrounded
        if (Date.now() >= nextSyncAtRef.current) repredict()
      }
    }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      released = true
      document.removeEventListener('visibilitychange', onVis)
      try { wakeLockRef.current?.release() } catch {}
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Journey guardian: server-side rain watch while the screen is off ───
  // Registers the route + speed + a push subscription with the backend; its
  // scheduled sweep dead-reckons the position and pushes "rain ahead on your
  // route" notifications even when the phone is locked (where web GPS can't
  // run). Every radar sync while the app IS open re-anchors the server's
  // estimate with the real GPS progress.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
          setGuardian('off')
          return
        }
        await navigator.serviceWorker.register('/sw.js')
        const reg = await navigator.serviceWorker.ready
        const perm = await Notification.requestPermission()
        if (perm !== 'granted') { if (!cancelled) setGuardian('denied'); return }
        let sub = await reg.pushManager.getSubscription()
        if (!sub) {
          const { data } = await axios.get(`${apiBase}/alerts/vapid_public_key`)
          sub = await reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(data.public_key),
          })
        }
        const body = {
          subscription: sub.toJSON(),
          speed_kmh: Number(plannedSpeedKmh) > 0 ? Number(plannedSpeedKmh) : 40,
          waypoints: waypointsRef.current
            .filter((w) => Number.isFinite(w?._cumKm))
            .map((w) => ({ lat: w.lat, lon: w.lon, cum_km: w._cumKm })),
        }
        const res = await axios.post(`${apiBase}/journey/start`, body, { timeout: 90000 })
        if (cancelled) {
          // unmounted before the server answered — don't leave an orphan journey
          if (res.data?.journey_id) {
            axios.post(`${apiBase}/journey/end`, { journey_id: res.data.journey_id }).catch(() => {})
          }
          return
        }
        journeyIdRef.current = res.data?.journey_id ?? null
        setGuardian(journeyIdRef.current != null ? 'on' : 'off')
      } catch {
        if (!cancelled) setGuardian('off')
      }
    })()
    return () => { cancelled = true }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  function endServerJourney() {
    if (endedRef.current || journeyIdRef.current == null) return
    endedRef.current = true
    axios.post(`${apiBase}/journey/end`, { journey_id: journeyIdRef.current }).catch(() => {})
  }

  // ── Radar sync: refresh frame + re-predict remaining route ─────────────
  async function repredict() {
    if (repredictingRef.current || !aliveRef.current) return
    repredictingRef.current = true
    setSyncing(true)
    const beforeCd = computeCountdown(waypointsRef.current, progressRef.current, speedForEta())
    try {
      const pos = fixRef.current
        ? { lat: fixRef.current.lat, lon: fixRef.current.lon }
        : { lat: routeCoords[0][0], lon: routeCoords[0][1] }

      // Ask the backend to check IMD for a newer frame (bounded by its bg-lock)
      let newFrame = false
      try {
        const r = await axios.post(`${apiBase}/radar/refresh`, pos, { timeout: 90000 })
        newFrame = !!r.data?.new_frame
      } catch { /* stale-while-revalidate: prediction below still works */ }

      const spd = speedForEta()
      const progress = progressRef.current
      const body = {
        waypoints: waypointsRef.current
          .filter((w) => Number.isFinite(w?._cumKm))
          .map((w) => ({
            lat: w.lat,
            lon: w.lon,
            eta_mins: Math.max(0, ((w._cumKm - progress) / spd) * 60),
          })),
      }
      const res = await axios.post(`${apiBase}/predict_waypoints`, body, { timeout: 90000 })
      if (!aliveRef.current) return
      onWaypointsUpdated(res.data)

      // re-anchor the server-side guardian with the real progress
      if (journeyIdRef.current != null && !endedRef.current) {
        axios.post(`${apiBase}/journey/update`, {
          journey_id: journeyIdRef.current,
          progress_km: progress,
          speed_kmh: spd,
        }).catch(() => {})
      }

      // honest correction note when the picture meaningfully shifted
      const rawWps = Array.isArray(res.data?.waypoints) ? res.data.waypoints : []
      const probe = rawWps.map((w, i) => ({
        ...w,
        _cumKm: waypointsRef.current[i]?._cumKm ?? null,
      }))
      const afterCd = computeCountdown(probe, progress, spd)
      const beforeMins = beforeCd?.kind === 'ahead' ? beforeCd.mins : null
      const afterMins = afterCd?.kind === 'ahead' ? afterCd.mins : null
      const shifted =
        (beforeCd?.kind !== afterCd?.kind) ||
        (beforeMins != null && afterMins != null && Math.abs(beforeMins - afterMins) > SHIFT_NOTE_MIN)
      if (newFrame || shifted) {
        setUpdateNote(`${newFrame ? 'New radar frame' : 'Radar re-check'} · updated ${istClock()} IST`)
      }
    } catch {
      if (aliveRef.current) setUpdateNote(`Radar sync failed — retrying in 5 min`)
    } finally {
      repredictingRef.current = false
      nextSyncAtRef.current = Date.now() + REPREDICT_MS
      if (aliveRef.current) setSyncing(false)
    }
  }

  // ── 1 s tick: dead-reckon + countdown + sync timer ─────────────────────
  useEffect(() => {
    const id = setInterval(() => {
      const now = Date.now()

      if (simRef.current) {
        const spd = (Number(plannedSpeedKmh) > 0 ? Number(plannedSpeedKmh) : 40) * SIM_SPEEDUP
        progressRef.current = Math.min(totalKm, progressRef.current + (spd / 3600) * (TICK_MS / 1000))
        speedRef.current = Number(plannedSpeedKmh) > 0 ? Number(plannedSpeedKmh) : 40
        fixRef.current = { snapKm: progressRef.current, at: now, lat: 0, lon: 0 }
        setPhase('live')
      } else if (fixRef.current) {
        // glide between fixes at the rolling speed; frozen when standing still
        const spd = speedRef.current
        if (spd != null && spd >= MOVING_MIN_KMH) {
          const reckoned = fixRef.current.snapKm + (spd / 3600) * ((now - fixRef.current.at) / 1000)
          progressRef.current = Math.min(totalKm, Math.max(progressRef.current, reckoned))
        }
      }

      // Countdown + stats are always computed from the best progress estimate.
      // Before a usable on-route GPS fix arrives (still locating, or the user
      // is off-route / not at the start yet), progress is 0 = the route's
      // start point — so the countdown matches the route prediction instead
      // of silently claiming "clear ahead".
      const progress = progressRef.current
      setRemainKm(Math.max(0, totalKm - progress))
      setSpeedKmh(speedRef.current)
      setCountdown(computeCountdown(waypointsRef.current, progress, speedForEta()))
      if (fixRef.current || simRef.current) {
        if (progress >= totalKm - 0.05) { setArrived(true); endServerJourney() }
        const [lat, lon] = pointAtKm(routeCoords, cumKm, progress)
        onLivePos({ lat, lon })
      }

      const left = Math.max(0, Math.round((nextSyncAtRef.current - now) / 1000))
      setSyncLeftSec(left)
      if (left === 0) repredict()
    }, TICK_MS)
    return () => clearInterval(id)
  }, [routeCoords, cumKm, totalKm, plannedSpeedKmh]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── render ──────────────────────────────────────────────────────────────
  const eta = speedForEta() > 0 ? (remainKm / speedForEta()) * 60 : null
  const decayChip =
    countdown?.kind === 'ahead' && countdown.decay && countdown.decay !== 'stable'
      ? countdown.decay === 'dying' || countdown.decay === 'dead' ? 'Fading fast'
        : countdown.decay === 'weakening' ? 'Weakening'
        : countdown.decay === 'growing' ? 'Intensifying'
        : null
      : null

  let hero
  if (arrived) {
    hero = (
      <div className="live-hero live-hero--clear">
        <span className="live-hero__big">You've arrived 🏁</span>
        <span className="live-hero__sub">Journey complete.</span>
      </div>
    )
  } else if (phase === 'geo_error') {
    hero = (
      <div className="live-hero live-hero--warn">
        <span className="live-hero__big">No GPS</span>
        <span className="live-hero__sub">{geoMsg}</span>
      </div>
    )
  } else if (phase === 'starting' && !countdown) {
    hero = (
      <div className="live-hero">
        <span className="live-hero__big live-hero__big--dim">Locating…</span>
        <span className="live-hero__sub">Locking onto your GPS position.</span>
      </div>
    )
  } else if (countdown?.kind === 'ahead') {
    const now = countdown.mins < 1.5
    hero = (
      <div className="live-hero live-hero--rain">
        <span className="live-hero__label">
          {countdown.label} ahead
          {decayChip && <span className={`decay-chip decay-chip--${countdown.decay}`}>{decayChip}</span>}
        </span>
        <span className="live-hero__big">{now ? 'Reaching you now' : fmtMins(countdown.mins)}</span>
        <span className="live-hero__sub">
          {now ? 'You are entering the rain stretch.' : 'until you reach the rain, at your current pace.'}
        </span>
      </div>
    )
  } else if (countdown?.kind === 'in_rain') {
    hero = (
      <div className="live-hero live-hero--rain">
        <span className="live-hero__label">{countdown.label} — you're in it</span>
        <span className="live-hero__big">
          {countdown.endsMins != null ? `Ends ${fmtMins(countdown.endsMins)}` : 'Rain to destination'}
        </span>
        <span className="live-hero__sub">
          {countdown.endsMins != null
            ? 'until you drive out of this rain stretch.'
            : 'Radar shows rain along the rest of your route.'}
        </span>
      </div>
    )
  } else {
    hero = (
      <div className="live-hero live-hero--clear">
        <span className="live-hero__big">Clear ahead</span>
        <span className="live-hero__sub">No rain predicted on the rest of your route.</span>
      </div>
    )
  }

  return (
    <div className="live-panel">
      <div className="live-panel__head">
        <span className="live-badge">
          <span className="live-badge__dot" aria-hidden />
          LIVE JOURNEY
        </span>
        {offRoute && <span className="live-offroute">Off route</span>}
        <button type="button" className="live-end-btn" onClick={() => { endServerJourney(); onEnd() }}>End</button>
      </div>

      {hero}

      {!arrived && !simOn && (offRoute || phase === 'starting') && countdown && (
        <p className="live-anchor-note">
          📍 You're not on the route yet — predictions count from the route's
          start point and will lock onto your GPS once you're on the way.
        </p>
      )}

      <div className="live-stats">
        <div className="live-stat">
          <span className="live-stat__label">Speed</span>
          <span className="live-stat__value">
            {speedKmh != null ? `${Math.round(speedKmh)} km/h` : '—'}
          </span>
        </div>
        <div className="live-stat">
          <span className="live-stat__label">Remaining</span>
          <span className="live-stat__value">{remainKm.toFixed(1)} km</span>
        </div>
        <div className="live-stat">
          <span className="live-stat__label">Arrive</span>
          <span className="live-stat__value">{eta != null && !arrived ? `${istClock(eta)} IST` : '—'}</span>
        </div>
      </div>

      {updateNote && <p className="live-note">{updateNote}</p>}

      {!arrived && (guardian === 'on' || guardian === 'denied') && (
        <p className={`live-guardian${guardian === 'on' ? ' live-guardian--on' : ''}`}>
          {guardian === 'on'
            ? '🛡 Screen-off watch on — you\'ll get a notification if rain nears your route, even with the phone locked.'
            : '🔕 Notifications blocked — with the screen off you won\'t get rain warnings. Allow notifications to enable.'}
        </p>
      )}

      <div className="live-sync">
        {syncing ? (
          <><span className="spinner spinner--sm" aria-hidden /> Syncing with radar…</>
        ) : (
          <>Next radar sync in {Math.floor(syncLeftSec / 60)}:{String(syncLeftSec % 60).padStart(2, '0')}</>
        )}
      </div>

      {import.meta.env.DEV && !arrived && (
        <button
          type="button"
          className="live-sim-btn"
          onClick={() => {
            simRef.current = !simRef.current
            setSimOn(simRef.current)
            if (simRef.current && !fixRef.current) {
              progressRef.current = 0
              fixRef.current = { snapKm: 0, at: Date.now(), lat: routeCoords[0][0], lon: routeCoords[0][1] }
            }
          }}
        >
          {simOn ? '⏸ Stop simulated drive' : '▶ Simulate drive (dev only)'}
        </button>
      )}
    </div>
  )
}
