// Garaj Baras — SavedPlaces.jsx
//
// "Saved places" card for the Nowcast tab (signed-in users only — wrap in
// <SignInGate>). CRUD against the backend /locations endpoints; tapping a
// place feeds it back into the nowcast location picker via onPick.

import { useEffect, useState, useCallback } from 'react'
import axios from 'axios'
import { useAuth } from './auth'

export default function SavedPlaces({ apiBase, currentLat, currentLon, currentName, onPick }) {
  const { user, authHeaders } = useAuth()
  const [locations, setLocations] = useState(null)   // null = loading
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const { data } = await axios.get(`${apiBase}/locations`, { headers: authHeaders() })
      setLocations(data.locations || [])
      setError(null)
    } catch (e) {
      setError(e?.response?.data?.detail || 'Could not load saved places.')
      setLocations([])
    }
  }, [apiBase, authHeaders])

  useEffect(() => { if (user) load() }, [user, load])

  async function saveCurrent() {
    if (currentLat == null || currentLon == null) return
    setBusy(true)
    try {
      const label = (currentName || `${currentLat.toFixed(3)}, ${currentLon.toFixed(3)}`).slice(0, 60)
      await axios.post(`${apiBase}/locations`,
        { label, lat: currentLat, lon: currentLon },
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

  const canSaveCurrent = currentLat != null && currentLon != null &&
    !(locations || []).some((l) =>
      Math.abs(l.lat - currentLat) < 1e-4 && Math.abs(l.lon - currentLon) < 1e-4)

  return (
    <div className="places-card">
      <div className="places-card__head">
        <span className="places-card__title">📍 Saved places</span>
        {canSaveCurrent && (
          <button type="button" className="places-card__save" disabled={busy} onClick={saveCurrent}>
            + Save “{(currentName || 'this location').slice(0, 24)}”
          </button>
        )}
      </div>

      {locations === null && <div className="places-card__empty">Loading…</div>}
      {locations !== null && locations.length === 0 && !error && (
        <div className="places-card__empty">
          No saved places yet. Pick a location above and save it for one-tap checks.
        </div>
      )}
      {error && <div className="places-card__error">{error}</div>}

      {(locations || []).map((l) => (
        <div key={l.id} className="places-card__row">
          <button type="button" className="places-card__pick"
            onClick={() => onPick && onPick(l)}>
            <span className="places-card__label">{l.label}</span>
            <span className="places-card__coords">
              {Number(l.lat).toFixed(3)}°N, {Number(l.lon).toFixed(3)}°E
            </span>
          </button>
          <button type="button" className="places-card__del" aria-label={`Delete ${l.label}`}
            disabled={busy} onClick={() => remove(l.id)}>
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}
