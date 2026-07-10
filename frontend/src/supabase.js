// Supabase client (Auth only — the app's data still lives behind our own
// FastAPI backend, which verifies the Supabase JWT). Null when the env vars
// aren't set, so the app runs fine without auth configured (route + nowcast
// don't need it).
//
// frontend/.env:
//   VITE_SUPABASE_URL=https://<project-ref>.supabase.co
//   VITE_SUPABASE_ANON_KEY=<anon public key>
import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

export const supabase = url && anonKey ? createClient(url, anonKey) : null
export const authConfigured = Boolean(supabase)
