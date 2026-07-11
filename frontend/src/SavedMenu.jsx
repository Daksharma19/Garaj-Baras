// Garaj Baras — SavedMenu.jsx
//
// Kebab (⋮) button in the nav → slide-out panel with tabs "Places" and
// "Routes", each searchable. Signed-in only. Tapping a saved place calls
// onPick (App switches to the Nowcast tab and loads it). Saved routes are
// a Phase-5 placeholder for now.
//
// `currentLoc` (optional {label, lat, lon}) enables a "Save this location"
// action — passed only from the Nowcast nav, where a current scan exists.

import { useEffect, useState, useCallback } from 'react'
import axios from 'axios'
import { useAuth } from './auth'

export default function SavedMenu({ apiBase, currentLoc, onPick }) {
  const { user, authHeaders } = useAuth()
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState('places')     // places | routes
  const [locations, setLocations] = useState(null)
  const [query, setQuery] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

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

  if (!user) return null

  async function saveCurrent() {
    if (!currentLoc) return
    setBusy(true)
    try {
      const label = (currentLoc.label ||
        `${currentLoc.lat.toFixed(3)}, ${currentLoc.lon.toFixed(3)}`).slice(0, 60)
      await axios.post(`${apiBase}/locations`,
        { label, lat: currentLoc.lat, lon: currentLoc.lon },
        { headers: authHeaders() })
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

  const filtered = (locations || []).filter((l) =>
    l.label.toLowerCase().includes(query.trim().toLowerCase()))

  const alreadySaved = currentLoc && (locations || []).some((l) =>
    Math.abs(l.lat - currentLoc.lat) < 1e-4 && Math.abs(l.lon - currentLoc.lon) < 1e-4)

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
                    onClick={saveCurrent}>
                    + Save “{(currentLoc.label || 'current location').slice(0, 28)}”
                  </button>
                )}

                <input className="sm-search" placeholder="Search saved places…"
                  value={query} onChange={(e) => setQuery(e.target.value)} />

                {locations === null && <div className="sm-empty">Loading…</div>}
                {error && <div className="sm-error">{error}</div>}
                {locations !== null && locations.length === 0 && !error && (
                  <div className="sm-empty">
                    No saved places yet. Scan a location in Nowcast, then tap “Save”.
                  </div>
                )}
                {locations !== null && locations.length > 0 && filtered.length === 0 && (
                  <div className="sm-empty">No places match “{query}”.</div>
                )}

                {filtered.map((l) => (
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
