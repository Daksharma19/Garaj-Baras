# Auth setup (Phase 3: Login + Saved Places)

Route + Nowcast never need login. Sign-in gates: **Ask AI tab, rain alerts,
saved places.**

## Try on localhost (5 minutes)

1. **Supabase dashboard** (same project as the Postgres DB):
   - *Authentication → Providers → Email*: ON (default). This gives email OTP.
   - *(Optional now)* *Providers → Google*: needs a Google Cloud OAuth client
     (Authorized redirect URI: `https://<project-ref>.supabase.co/auth/v1/callback`).
     Email OTP works without this — you can test with just email first.
   - *Authentication → URL Configuration*: add `http://localhost:5173` to
     Site URL / Redirect URLs.

2. **`frontend/.env`** (create/extend):
   ```
   VITE_SUPABASE_URL=https://<project-ref>.supabase.co
   VITE_SUPABASE_ANON_KEY=<anon public key>   # dashboard → Settings → API
   ```

3. **Backend** — nothing needed for localhost. Without `SUPABASE_JWT_SECRET`
   it runs in dev mode (accepts tokens without signature verification and
   prints a warning). The real Supabase token from the frontend "just works".

4. Run as usual: `uvicorn main:app --reload --port 8000` in `backend/`,
   `npm run dev` in `frontend/`. Click **Sign in** in the top nav →
   "Email me a code" → enter the 6-digit code from your inbox.

## Before deploying to Render (do NOT skip)

Backend token verification supports BOTH of Supabase's signing schemes and
auto-picks per token, so set whichever your project uses (setting both is fine):

- **`SUPABASE_URL`** = `https://<project-ref>.supabase.co`
  — covers projects using **asymmetric JWT signing keys** (ES256/RS256, the
  newer default; verified via the public JWKS endpoint). Safe to always set.
- **`SUPABASE_JWT_SECRET`** = Settings → JWT Keys → JWT/Legacy secret
  — covers projects still signing with **HS256** (shared secret).

If NEITHER is set, the backend trusts unsigned tokens (dev mode) — fine on
localhost, **unacceptable in production**. Recommended: set **`SUPABASE_URL`**
always, and add `SUPABASE_JWT_SECRET` too if unsure — the right one is used
automatically based on each token's `alg`.

Also:
- Add the production frontend URL to Supabase's Redirect URLs.
- Set `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` in the frontend build env
  (the **Publishable** key is the anon key on newer projects).
- `pyjwt` + `cryptography` install via requirements.txt on deploy.

## What was built (reference)

- `backend/auth.py` — verifies the Supabase JWT locally (HS256, pyjwt).
- `backend/accounts.py` — `users` + `saved_locations` (Postgres prod /
  `accounts.db` SQLite dev, same `db.py` switch as alerts).
- Endpoints: `GET /me`, `GET/POST /locations`, `PATCH/DELETE /locations/{id}`.
- `alerts.subscriptions.user_id` — subscriptions from signed-in users are tied
  to the account; anonymous subscriptions keep working.
- `frontend/src/auth.jsx` — AuthProvider, AccountButton (nav), LoginModal
  (Google + email OTP), SignInGate. `SavedPlaces.jsx` — saved-places card in
  the Nowcast tab.
