import { tr } from './i18n'

// Shared turn-by-turn helpers (ORS step types) — used by the route card and navigation.

// ORS step types → [english, hindi, arrow rotation (deg) | special glyph]
export const MANEUVERS = {
  0: ['Turn left', 'बाएँ मुड़ें', -90],
  1: ['Turn right', 'दाएँ मुड़ें', 90],
  2: ['Turn sharp left', 'तेज़ बाएँ मुड़ें', -135],
  3: ['Turn sharp right', 'तेज़ दाएँ मुड़ें', 135],
  4: ['Bear left', 'हल्का बाएँ मुड़ें', -45],
  5: ['Bear right', 'हल्का दाएँ मुड़ें', 45],
  6: ['Continue straight', 'सीधे चलें', 0],
  7: ['Enter the roundabout', 'गोलचक्कर में जाएँ', 'round'],
  8: ['Exit the roundabout', 'गोलचक्कर से निकलें', 'round'],
  9: ['Make a U-turn', 'यू-टर्न लें', 'uturn'],
  10: ['Arrive at destination', 'मंज़िल पर पहुँचें', 'goal'],
  11: ['Head out', 'चलना शुरू करें', 0],
  12: ['Keep left', 'बाएँ रहें', -30],
  13: ['Keep right', 'दाएँ रहें', 30],
}

export function maneuverText(step) {
  const m = MANEUVERS[step?.type] || MANEUVERS[6]
  if (step?.type === 7 && Number(step.exit) > 0) {
    return tr(`Take exit ${step.exit} at the roundabout`, `गोलचक्कर से ${step.exit}वाँ निकास लें`)
  }
  return tr(m[0], m[1])
}

export function streetName(step) {
  const n = String(step?.name || '').trim()
  return n && n !== '-' ? n : ''
}

export function ManeuverIcon({ type, size = 44 }) {
  const glyph = (MANEUVERS[type] || MANEUVERS[6])[2]
  if (glyph === 'goal') return <span className="nav-mico nav-mico--glyph" style={{ fontSize: size * 0.7 }}>🏁</span>
  if (glyph === 'round') return <span className="nav-mico nav-mico--glyph" style={{ fontSize: size * 0.8 }}>⟳</span>
  if (glyph === 'uturn') {
    return (
      <svg className="nav-mico" width={size} height={size} viewBox="0 0 48 48" aria-hidden="true">
        <path d="M30 42 V18 a8 8 0 0 0 -16 0 V30" fill="none" stroke="currentColor" strokeWidth="5" strokeLinecap="round" />
        <path d="M6 26 L14 36 L22 26" fill="none" stroke="currentColor" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    )
  }
  return (
    <svg className="nav-mico" width={size} height={size} viewBox="0 0 48 48" aria-hidden="true"
      style={{ transform: `rotate(${glyph}deg)` }}>
      <path d="M24 42 V10" stroke="currentColor" strokeWidth="5" strokeLinecap="round" />
      <path d="M13 20 L24 8 L35 20" fill="none" stroke="currentColor" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}


export function fmtDist(km) {
  if (km < 0.1) return `${Math.max(10, Math.round((km * 1000) / 10) * 10)} ${tr('m', 'मी')}`
  if (km < 1) return `${Math.round((km * 1000) / 50) * 50} ${tr('m', 'मी')}`
  return `${km < 10 ? km.toFixed(1) : Math.round(km)} ${tr('km', 'किमी')}`
}
