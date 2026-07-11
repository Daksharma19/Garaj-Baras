// Garaj Baras — SavedMenu.jsx
//
// Kebab (⋮) button in the nav → slide-out panel with tabs "Places" and
// "Routes", each searchable. Signed-in only. Tapping a saved place calls
// onPick (App switches to the Nowcast tab and loads it). Saved routes are
// a Phase-5 placeholder for now.
//
// The Places search box does double duty: it filters your saved places AND
// geocodes new places (Nominatim). Any geocoded result you haven't saved yet
// shows a "+" to save it on the spot.
//
// `currentLoc` (optional {label, lat, lon}) enables a "Save this location"
// action — passed only from the Nowcast nav, where a current scan exists.

import { useEffect, useState, useCallback, useRef } from 'react'
import axios from 'axios'
import { useAuth } from './auth'

const NOMINATIM_SEARCH_URL = 'https://nominatim.openstreetmap.org/search'

function shortName(name) {
  return String(name ?? '').split(',')[0].trim()
}

export default function SavedMenu({ apiBase, currentLoc, onPick }) {
  const { user, authHeaders } = useAuth()
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState('places')     // places | routes
  const [locations, setLocations] = useState(null)
  const [query, setQuery] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  // Geocoder results for the current query (places you could add).
  const [geoResults, setGeoResults] = useState([])
  const [geoLoading, setGeoLoading] = useState(false)
  const debounceRef = useRef(null)
  const abortRef = useRef(null)

  const load = useCallback(async () => {
    try {
      const { data } = await axios.get(`${apiBase}/locations`, { headers: authHeaders() })
      setLocations(data.locations || [])
      setError(null)
    } catch (e) {
      setError(e?.response?.status === 404
        ? 'Server is updating — try again in a minute.'
        : (e?.response?.data?.detail || 'Could not load saved places.'))
      setLocations([])
    }
  }, [apiBase, authHeaders])

  useEffect(() => { if (open && user) load() }, [open, user, load])

  // Close on Escape
  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // Debounced geocoder search as you type.
  useEffect(() => {
    const q = query.trim()
    if (abortRef.current) abortRef.current.abort()
    if (debounceRef.current) clearTimeout(debounceRef.current)
    if (q.length < 3) { setGeoResults([]); setGeoLoading(false); return }
    setGeoLoading(true)
    debounceRef.current = setTimeout(async () => {
      const ac = new AbortController()
      abortRef.current = ac
      try {
        const res = await axios.get(NOMINATIM_SEARCH_URL, {
          timeout: 15000, signal: ac.signal,
          params: { q, format: 'jsonv2', limit: 6, addressdetails: 1, countrycodes: 'in' },
          headers: { Accept: 'application/json' },
        })
        const arr = (Array.isArray(res.data) ? res.data : [])
          .filter((x) => x?.lat && x?.lon && x?.display_name)
          .map((x) => ({
            id: String(x.place_id ?? x.osm_id ?? x.display_name),
            display_name: String(x.display_name),
            lat: Number(x.lat), lon: Number(x.lon),
          }))
          .filter((x) => Number.isFinite(x.lat) && Number.isFinite(x.lon))
        setGeoResults(arr)
      } catch (e) {
        if (e?.name !== 'CanceledError' && e?.name !== 'AbortError') setGeoResults([])
      } finally {
        setGeoLoading(false)
      }
    }, 400)
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current) }
  }, [query])

  if (!user) return null

  const isSaved = (lat, lon) => (locations || []).some((l) =>
    Math.abs(l.lat - lat) < 1e-3 && Math.abs(l.lon - lon) < 1e-3)

  async function savePlace(label, lat, lon) {
    setBusy(true)
    try {
      const lbl = (label || `${lat.toFixed(3)}, ${lon.toFixed(3)}`).slice(0, 60)
      await axios.post(`${apiBase}/locations`,
        { label: lbl, lat, lon }, { headers: authHeaders() })
      await load()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Could not save this place.')
    } finally {
      setBusy(false)
    }
  }

  async function remove(id) {
    setBusy(true)
    try {
      await axios.delete(`${apiBase}/locations/${id}`, { headers: authHeaders() })
      setLocations((prev) => (prev || []).filter((l) => l.id !== id))
    } catch {
      setError('Could not delete — try again.')
    } finally {
      setBusy(false)
    }
  }

  const q = query.trim().toLowerCase()
  const savedMatches = (locations || []).filter((l) =>
    !q || l.label.toLowerCase().includes(q))
  // Geocoded places not already in the saved list.
  const addable = q.length >= 3
    ? geoResults.filter((g) => !isSaved(g.lat, g.lon))
    : []

  const alreadySaved = currentLoc && isSaved(currentLoc.lat, currentLoc.lon)

  return (
    <>
      <button type="button" className="kebab-btn" aria-label="Saved places & routes"
        onClick={() => setOpen(true)}>
        <svg width="18" height="18" viewBox="0 0 20 20" aria-hidden>
          <circle cx="10" cy="4" r="1.6" fill="currentColor" />
          <circle cx="10" cy="10" r="1.6" fill="currentColor" />
          <circle cx="10" cy="16" r="1.6" fill="currentColor" />
        </svg>
      </button>

      {open && (
        <div className="sm-overlay" onClick={() => setOpen(false)}>
          <aside className="sm-panel" onClick={(e) => e.stopPropagation()}>
            <div className="sm-panel__head">
              <span className="sm-panel__title">Your library</span>
              <button type="button" className="sm-panel__close" aria-label="Close"
                onClick={() => setOpen(false)}>×</button>
            </div>

            <div className="sm-tabs">
              <button type="button"
                className={`sm-tab${tab === 'places' ? ' sm-tab--active' : ''}`}
                onClick={() => setTab('places')}>📍 Places</button>
              <button type="button"
                className={`sm-tab${tab === 'routes' ? ' sm-tab--active' : ''}`}
                onClick={() => setTab('routes')}>🛣 Routes</button>
            </div>

            {tab === 'places' && (
              <div className="sm-body">
                {currentLoc && !alreadySaved && (
                  <button type="button" className="sm-save-current" disabled={busy}
                    onClick={() => savePlace(currentLoc.label, currentLoc.lat, currentLoc.lon)}>
                    + Save “{(currentLoc.label || 'current location').slice(0, 28)}”
                  </button>
                )}

                <input className="sm-search" placeholder="Search a place to save, or filter saved…"
                  value={query} onChange={(e) => setQuery(e.target.value)} />

                {/* Add new (geocoded) results not yet saved */}
                {addable.length > 0 && (
                  <>
                    <div className="sm-grouphdr">Add a new place</div>
                    {addable.map((g) => (
                      <div key={g.id} className="sm-row">
                        <div className="sm-row__pick sm-row__pick--static">
                          <span className="sm-row__label">{shortName(g.display_name)}</span>
                          <span className="sm-row__coords">{g.display_name}</span>
                        </div>
                        <button type="button" className="sm-row__add"
                          aria-label={`Save ${shortName(g.display_name)}`} disabled={busy}
                          onClick={() => savePlace(shortName(g.display_name), g.lat, g.lon)}>+</button>
                      </div>
                    ))}
                  </>
                )}
                {geoLoading && q.length >= 3 && <div className="sm-empty">Searching…</div>}

                {/* Saved places */}
                {(savedMatches.length > 0 || addable.length > 0) && (
                  <div className="sm-grouphdr">Saved places</div>
                )}
                {locations === null && <div className="sm-empty">Loading…</div>}
                {error && <div className="sm-error">{error}</div>}
                {locations !== null && locations.length === 0 && !error && addable.length === 0 && (
                  <div className="sm-empty">
                    No saved places yet. Search above and tap “+”, or scan a location
                    in Nowcast and tap “Save”.
                  </div>
                )}
                {savedMatches.map((l) => (
                  <div key={l.id} className="sm-row">
                    <button type="button" className="sm-row__pick"
                      onClick={() => { onPick && onPick(l); setOpen(false) }}>
                      <span className="sm-row__label">{l.label}</span>
                      <span className="sm-row__coords">
                        {Number(l.lat).toFixed(3)}°N, {Number(l.lon).toFixed(3)}°E
                      </span>
                    </button>
                    <button type="button" className="sm-row__del" aria-label={`Delete ${l.label}`}
                      disabled={busy} onClick={() => remove(l.id)}>✕</button>
                  </div>
                ))}
              </div>
            )}

            {tab === 'routes' && (
              <div className="sm-body">
                <div className="sm-soon">
                  <div className="sm-soon__icon">🛣</div>
                  <div className="sm-soon__title">Saved routes are coming soon</div>
                  <div className="sm-soon__sub">
                    You’ll be able to save your daily commute and get a one-tap
                    rain re-check — plus “alert me if rain’s expected on my 6pm drive”.
                  </div>
                </div>
              </div>
            )}
          </aside>
        </div>
      )}
    </>
  )
}
