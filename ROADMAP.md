# Garaj Baras — Production Roadmap

> Path from working demo → app real users can rely on.
> ✅ = done · 🟡 = partially done · ⬜ = not started
>
> Last updated: 2026-07-11

---

## Progress at a glance

| Phase | Item | Status |
|---|---|---|
| 1 | Postgres + persistent alert subscriptions | ✅ Done |
| — | Working push pipeline (VAPID fixed, delivery confirmed) | ✅ Done |
| 4 (early) | Reliable scheduled alerts (sweep + instant check) | 🟡 Mostly done (GitHub-Actions cron, not a real worker) |
| 2 | Login (Google/OTP) + saved locations | 🟡 Code done (Supabase project config + prod env pending) |
| 3 | PWA install + coverage/onboarding screen | ⬜ |
| 5 | Saved routes + commute alerts | ⬜ |
| 6 | More radars, Hindi UI, sharing, accuracy badge | ⬜ |

**Roughly the foundational ~30% is done** — the database and alert system now work reliably, which everything else builds on.

---

## 1. Accounts & data (the biggest gap)

- ✅ **Real database (Postgres).** Supabase Postgres is live; `backend/db.py` switches to it when `DATABASE_URL` is set, SQLite locally. Fixes the silent data loss — SQLite on Render's ephemeral disk was wiping `alerts.db` / `verification.db` on every deploy. Alerts + verification now persist.
- 🟡 **User accounts / login** — built: Supabase Auth (Google + email OTP) on the frontend, local JWT verification in `backend/auth.py`. Remaining: enable the providers in the Supabase dashboard + set env vars.
- 🟡 **Saved locations** — built: `saved_locations` table + `/locations` CRUD + `SavedPlaces` card in the Nowcast tab (tap to load, cap 10). Per-location alert toggle field exists but isn't wired to the sweep yet.
- ⬜ **Saved/favorite routes** — e.g. daily commute, one-tap re-check, optionally "alert me if rain expected on my 6pm commute".
- ⬜ **Prediction history** — "what did it tell me yesterday, and was it right" (accuracy is already graded internally — surface it per-user; builds trust).

## 2. Alerts (exists, but fragile for real users)

- ✅ **Subscriptions no longer die on deploy** — now stored in Postgres, not ephemeral SQLite.
- ✅ **Alerts fire without a user browsing** — `/tasks/sweep_alerts` + GitHub Action (every 10 min) refresh radars that have subscriptions; instant check fires the moment a user enables alerts.
- 🟡 **Tie subscriptions to accounts + multiple devices** — `subscriptions.user_id` now recorded when a signed-in user subscribes; per-account multi-device management UI still to do.
- ⬜ **Quiet hours / alert preferences** — don't ping at 3am unless opted in; intensity threshold ("only heavy rain").
- ⬜ **Commute-time alerts** — "check my route at 8:30am daily" (needs a real scheduler — see infra).
- ⬜ **Fallback channels** — email or WhatsApp/Telegram alerts for users who deny push.

## 3. Product / UX features users will expect

- ⬜ **Onboarding + coverage map** — only 4 radar cities covered; a first-run screen showing coverage circles prevents "it doesn't work in Mumbai" 1-star reviews. Out-of-coverage users get a graceful "notify me when your city is added" capture.
- ⬜ **More radars** — IMD publishes many more stations; the per-radar module pattern scales, LRU eviction handles memory. The #1 growth lever.
- ⬜ **PWA / installable app** — service worker already exists; add manifest + install prompt so it feels like an app (the Flutter client was deleted, so this is the cheap mobile story).
- ⬜ **Shareable results** — share a nowcast/route result as a link or image card (organic growth).
- ⬜ **Hindi/vernacular UI** — target audience strongly suggests it; the app name itself is Hindi.
- ⬜ **Confidence/accuracy badge** — surface POD/CSI stats to users ("87% accurate last 7 days near you").
- ⬜ **Rate-limit-safe geocoding** — Nominatim public API forbids production traffic at scale; needs own instance or a paid geocoder (same for OSRM demo server).

## 4. Infrastructure for "real deployment"

- 🟡 **Reliable alert firing** — the GitHub-Actions keep-alive + lazy-refresh now drives a real sweep every 10 min (was: alerts only fired when someone browsed). A dedicated always-on worker/cron would make it fully robust.
- ⬜ **Rate limiting + basic abuse protection** on the API (CORS is allow-all today); API keys per client if ever exposed.
- ⬜ **Monitoring/observability** — error tracking (Sentry), uptime alerts, IMD-source-down detection (if the GIF feed breaks, users see "radar data delayed", not stale predictions).
- ⬜ **Analytics** — know which tabs/cities people use.
- ⬜ **Legal basics** — privacy policy (storing locations = sensitive data), IMD data attribution, terms.

---

## Sequential order — what to do next, in order

1. ✅ **Postgres + persistent alert subscriptions** — *fixes silent data loss. DONE.*
2. ✅ **Reliable scheduled alerts (sweep + instant check)** — *done early, out of order, because alerts were the active feature. Real-worker upgrade optional later.*
3. 🟡 **Login (Google/OTP) + saved locations** ← **CODE DONE (2026-07-11); needs Supabase config**
   - Built: `backend/auth.py` (JWT verify), `backend/accounts.py` (`users` + `saved_locations`),
     `/me` + `/locations` CRUD endpoints, `subscriptions.user_id` column,
     frontend `auth.jsx` (Google + email OTP modal, `SignInGate`), `SavedPlaces.jsx`.
   - Policy shipped: route + nowcast free; **Ask AI, rain alerts, saved places gated**.
   - Remaining: enable Google provider + email OTP in the Supabase dashboard,
     set `SUPABASE_JWT_SECRET` (backend) and `VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY` (frontend).
4. ⬜ **PWA install + coverage/onboarding screen**
   - `manifest.json` + install prompt; first-run coverage map of the 4 radars; out-of-coverage email capture.
5. ⬜ **Saved routes + commute alerts**
   - Save favourite routes; scheduled "check my commute at 8:30am" (needs the real scheduler from infra).
6. ⬜ **Alert preferences** — quiet hours, intensity threshold, multi-device (all depend on login being in place).
7. ⬜ **Growth: more radar cities, Hindi UI, sharing, accuracy badge.**
8. ⬜ **Infra hardening** — rate limiting, Sentry monitoring, IMD-feed-down detection, analytics, privacy policy/terms, production geocoding. *(Do incrementally alongside 3–7; tighten before any real marketing push.)*

---

### Immediate next action
Start **Phase 3: Login + saved locations** using Supabase Auth. Keep nowcast/route free; gate only saved locations, saved routes, alert management, and history behind sign-in.
