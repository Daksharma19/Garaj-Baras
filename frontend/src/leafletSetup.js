// leaflet-rotate patches the *global* L, so expose it before that plugin loads.
// Import this module ahead of 'leaflet-rotate'.
import L from 'leaflet'

if (typeof window !== 'undefined') window.L = L

export default L
