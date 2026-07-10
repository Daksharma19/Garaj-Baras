# Garaj Baras — Project Architecture

> **Purpose of this file:** Complete architecture reference for the whole codebase.
> Read this INSTEAD of exploring the repo. It covers what the project does, every
> module's role, the data pipeline, all API endpoints, the frontend structure,
> deployment, and operational constraints. Only open source files when you need
> the exact implementation of something specific.
>
> Last updated: 2026-07-10.

---

## 1. What the project is

**Garaj Baras** (Hindi: *Garaj* = thunder, *Baras* = to rain) is a real-time,
radar-driven rain **nowcasting** system for India. It reads live Doppler weather
radar GIFs published by **IMD (India Meteorological Department)**, detects rain
and its motion via computer vision, and answers two questions:

1. **Route prediction** — "Will it rain on my drive from A to B, and on which
   exact stretch, at the time I'll be there?" (waypoint-by-waypoint, 2-min ETA
   resolution)
2. **Point nowcast** — "Will it rain at this location in the next ~2 hours?"
   (8 slots: now, +15 … +105 min, with probability + intensity + decay trend)

Plus: web-push **rain alerts** for saved locations, an **AI chatbot** (Gemini)
that calls the prediction engine via function calling, a **forecast animation**
that visualizes the prediction simulation, and **automated accuracy
verification** (every prediction is graded against later radar frames).

There is **no ML model** — the whole engine is classical CV (OpenCV optical
flow, connected components, color matching) + geometry, driven by IMD's public
radar imagery. There is also **no scheduler/cron in the backend** — all
refreshes are lazily triggered by user requests against a TTL cache (GitHub
Actions keep-alive pings substitute for a scheduler).

## 2. Tech stack

| Layer | Tech |
|---|---|
| Backend | Python 3.11, FastAPI + uvicorn, NumPy, OpenCV, Pillow, pytesseract (dev only), pywebpush, httpx/requests, Postgres (Supabase, prod) / SQLite (dev fallback) via `db.py` |
| Frontend | React 19 + Vite 8, react-leaflet/Leaflet (maps), axios. Single-page app, 3 tabs. |
| External services | IMD radar GIFs (data source), OSRM public demo (routing), Nominatim (geocoding), Open-Meteo (cloud-cover cross-check, currently not wired in), Gemini 2.5 Flash / Groq (chatbot), Web Push (VAPID) |
| Hosting | Backend: **Render free tier (512 MB RAM — the central constraint)** at `https://garaj-baras-api.onrender.com`. Frontend: Vercel/Netlify (static Vite build). GitHub Actions ping `/health` every 5–10 min to keep Render awake. |

## 3. Repository layout

```
Garaj Baras/
├── ARCHITECTURE.md              ← this file
├── README.md
├── Garaj Baras — Complete Project Summ.txt   ← prose project summary (partly stale)
├── Garaj Baras — Complete System Flow.txt    ← PPT-style pipeline walkthrough (partly stale)
├── .github/workflows/
│   ├── keep_alive.yml           ← cron */5: curl /health (keeps Render awake)
│   └── keepalive.yml            ← cron */10: same (duplicate, both active)
├── backend/                     ← FastAPI app (run from inside this dir)
│   ├── main.py                  ← API endpoints + per-radar state cache + LRU eviction (THE hub)
│   ├── radar.py                 ← Delhi GIF download/frame-extraction/OCR-timestamp + shared helpers
│   ├── radar_lucknow.py / radar_patna.py / radar_bhopal.py  ← per-radar wrappers over radar.py helpers
│   ├── georef.py                ← Delhi pixel↔latlon quadratic GCP model
│   ├── georef_lucknow.py / georef_patna.py / georef_bhopal.py ← per-radar georef models
│   ├── optical_flow.py          ← rain mask isolation + global movement vector (Farneback)
│   ├── patches.py               ← per-storm-cell (blob) motion + route intercept scoring
│   ├── decay.py                 ← per-cell dBZ trend tracks (stable/weakening/dying/dead)
│   ├── fuzzy.py                 ← RGB → dBZ → label; enrich_results() for route waypoints
│   ├── prediction.py            ← OSRM route building + rain-mask-shift route check
│   ├── nowcast.py               ← 8-slot point forecast engine (compute_nowcast_slots)
│   ├── forecast_gif.py          ← renders the forecast animation (GIF + frame payloads)
│   ├── db.py                    ← DB backend switch: Postgres when DATABASE_URL is set (prod), else SQLite
│   ├── bbox_mask.py             ← BBoxMask: patch masks as tight crops (~1/1000 memory)
│   ├── timestamp_match.py       ← digit template-matching timestamp reader (prod, no Tesseract)
│   ├── alerts.py                ← web-push rain alerts, SQLite alerts.db, state machine
│   ├── verification.py          ← prediction logging + auto-grading, SQLite verification.db, POD/FAR/CSI
│   ├── chatbot.py               ← Gemini/Groq chatbot with function calling, SSE streaming
│   ├── cloud_cover.py           ← Open-Meteo cloud-cover cross-check (utility; NOT currently imported by the pipeline)
│   ├── find_timestamp.py        ← one-off debug script (locating the timestamp panel)
│   ├── requirements.txt, runtime.txt (python-3.11.9), vapid_keys.json (dev VAPID keys)
│   ├── frames/, frames_lucknow/ … ← extracted PNG frames per radar (served statically)
│   ├── *.gif                    ← downloaded radar GIFs (delhi_radar.gif etc.)
│   ├── alerts.db, verification.db ← SQLite (dev only; prod uses Supabase Postgres via db.py)
│   ├── ts_templates/            ← digit glyph templates for timestamp_match.py
│   └── debug_*.png, *_verify*.png ← throwaway debug images (ignore)
├── frontend/
│   ├── src/App.jsx              ← ~1900 lines; entire app UI: tabs, route page, nowcast page, chat page
│   ├── src/RouteMap.jsx         ← lazy-loaded Leaflet map: colored route segments + animated journey car
│   ├── src/NetworkLayers.jsx    ← UNRELATED OSI-layers demo component; not imported anywhere
│   ├── public/sw.js             ← service worker: displays push notifications
│   ├── dist/                    ← committed production build
│   └── package.json, vite.config.js
└── misc/                        ← pitch decks, debug images, logs (non-code)
```

## 4. Data source & radars

IMD publishes an animated GIF per radar station (~every 10 min), e.g.
`https://mausam.imd.gov.in/Radar/animation/Converted/DELHI_MAXZ.gif`.
Each GIF ≈ 18 frames ≈ last 3 hours. The Delhi frame is 880×720 but only a
**527×525 center crop** is the radar map; the border holds legend + timestamp
text. Rain intensity (reflectivity, **dBZ**) is encoded as 10 legend colors
(dark blue ≈ 20 dBZ light drizzle → yellow 44 heavy → red 55 → white 60 extreme).

**Four radars, each with its own `radar_*.py` + `georef_*.py` pair:**

| Radar | Center | Notes |
|---|---|---|
| Delhi (Palam) | 28.556 N, 77.100 E | Primary/default. Also fetches IMD's separate "current image" (`caz_delhi.gif`) and appends it as newest frame when its timestamp is strictly newer than the GIF (GIF rebuilds lazily, can lag 60+ min). |
| Lucknow | 26.847 N, 80.946 E | GIF is 704×594, own OCR crop coordinates |
| Patna | 25.591 N, 85.096 E | |
| Bhopal | 23.288 N, 77.337 E | **Re-enabled** (older docs say disabled). BBoxMask compression + LRU state eviction (max 2 radars in RAM) made it fit in 512 MB. |

Radar selection (`_detect_radar` in main.py): check which radars' coverage
circles contain the point; if several, pick the closest center; if none,
fall back to Delhi. Out-of-coverage points get an explicit
`in_radar_bounds: false`, never a silent guess.

## 5. Processing pipeline (runs once per radar refresh, cached)

```
IMD GIF (every ~10 min)
  → radar.py: download → split frames → drop byte-identical duplicates
      → per-frame timestamp: Tesseract OCR (dev) / digit template match (prod, timestamp_match.py)
      → list of (frame_png_path, ist_datetime), oldest→newest
  → keep last 6 frames only (memory)
  → optical_flow.py: isolate_rain() per frame (RGB ≤65-dist match to 10 legend colors → binary mask)
      get_movement_vector(): Farneback dense flow on 5 consecutive pairs, averaged
      over rain pixels only, recent pairs weighted higher → global (dx, dy) px per 10 min
      + direction strings + speed km/h (0.877 km/px)
  → patches.py: compute_patch_motion(): connectedComponents on latest rain mask →
      per-blob optical flow using only that blob's pixels, only frames where the
      blob existed (12-px centroid search) → per-blob {centroid px+latlon, area,
      dx_10, dy_10, speed, direction, max_dbz}; masks stored as BBoxMask
  → decay.py: compute_decay_tracks(): link the same blob across all 6 frames
      (predict-next-position via global flow, nearest blob ≤35 px) → mean dBZ per
      frame → linear fit → decay_rate (dBZ per 10-min frame) → project_dbz(eta),
      status: stable / weakening (<32 & declining) / dying (<18) / dead (<8)
  → verification.verify_pending(): grade past predictions whose target time now
      has a real frame (±5 min) → outcome hit/false_alarm/miss/correct_clear
  → alerts.process_alerts(): nowcast each saved location, push notifications
  → new state dict atomically swapped into the per-radar cache
```

**Radar lag correction:** the frame timestamp is OCR'd; `get_radar_lag_mins()`
computes image age vs now (fallback default 25 min). Every prediction uses
`effective_eta = eta + lag_mins` because rain has already moved since the image
was taken. Lag is **recomputed per request** (`_fresh_lag_info`), not frozen at
refresh time.

**Clutter mask is intentionally disabled** (`clutter_mask=None` everywhere) —
it misflagged persistent monsoon rain as ground clutter.

## 6. Radar state cache (main.py) — the concurrency core

Per radar there is: a `*_cache` dict (the served state), a `_*_state_lock`
(guards writes), a `_*_bg_lock` (non-blocking acquire → at most one refresh at
a time), and a `_*_ready` threading.Event (cold-start gate).

Request flow (`_load_radar_state` and the 3 per-radar clones):
- **Cold start** (ready not set): spawn background refresh thread, block up to
  60 s on the event, return whatever's in the cache.
- **Fresh** (loaded < `RADAR_TTL_SEC` = 10 min ago, has frames+movement): return
  instantly.
- **Stale**: return current (stale) data immediately, kick off a background
  refresh thread — users never wait for a refresh after first load.

**LRU eviction** (`_touch_radar_and_evict`): at most `MAX_RADARS_IN_MEMORY = 2`
radars keep heavy state (frames, patches, tracks, masks). Least-recently-used
radars beyond the cap get heavy keys nulled, `ready` cleared (so next request
rebuilds from the on-disk GIF), and `gc.collect()` runs. This is what keeps
peak RSS flat on Render's 512 MB regardless of radar count.

State dict keys: `frame_data` (list of (path, ts)), `movement` (dx, dy,
dir_from, dir_to, speed), `latest_frame`, `latest_ts`, `lag_info`, `patches`,
`roi_mask`, `decay_tracks`, `clutter_mask` (always None), `last_loaded`,
`last_used`, `gif_mtime`.

## 7. Prediction algorithms

### Route rain check (prediction.py `check_route_rain` + fuzzy.py `enrich_results`)
1. Frontend (or `/predict`'s `generate_waypoints`) supplies waypoints
   (lat, lon, eta_mins). `/predict` builds them via **OSRM** (real road geometry
   + real per-segment driving time, resampled to one waypoint per ~2 driving
   minutes); fallback = straight line every 2 km at 40 km/h.
2. Per waypoint: latlon → pixel (georef), `effective_eta = eta + radar_lag`,
   shift the rain mask by `(dx, dy) × effective_eta/10`, check the pixel at
   3 time offsets (eta, +5, +10 min) → 3 hits = high confidence, 2 = medium,
   1 = low, 0 = clear; ETA > 60 min caps confidence at low. Per-patch motion is
   also consulted (patch back-projection provides `src_px/src_py`).
3. `enrich_results` reads the RGB at the (back-projected) source pixel → dBZ →
   label (Very Light/Light/Moderate/Heavy/Very Heavy Rain) + color + message.
4. Decay lookup at the source pixel attaches `decay_status` / `projected_dbz`.
5. `patches.score_patches_for_route` finds which storm cells will intercept the
   route and when (`patch_analysis` in the response).

### Point nowcast (nowcast.py `compute_nowcast_slots`)
8 slots (0, +15 … +105 min; alerts use 4 slots). Per slot, two passes:
- **Pass 1 — patch forward projection (primary):** every blob within 120 px of
  the user is advected by its OWN velocity to slot time; if the projected blob
  covers the user (+5 px tolerance) → hit; use that blob's dBZ + decay track.
- **Pass 2 — global-flow fallback:** back-project the user pixel by the global
  (dx, dy) over `slot + lag` minutes; if that source pixel is rain in the
  latest mask → rain is coming.
- Intensity: `projected_dbz = raw_pixel_dbz + decay_rate × frames_ahead`;
  dBZ → probability via a motion-ensemble mapping (~44 dBZ → ~92%, 20 → ~35%).
- **New-cell lifecycle:** a blob observed in only 1 frame (no measured motion/
  trend) gets synthetic decay −2.5 dBZ/10 min, may only assert rain for
  ≤30 min ahead, and dies below 10 dBZ — unless rain is a widespread shield
  (≥50% coverage in a 60-px circle), where the kill doesn't apply.

### Radar scene v2 (radar_scene.py) — data-driven animation
`/nowcast/radar_scene?lat=&lon=` returns a compact JSON scene (~50–70 KB) the
frontend canvas player (`RadarScenePlayer` in App.jsx) renders client-side:
- **history**: each cached frame (deduped by timestamp, ~10-min cadence) as a
  base64 dBZ grid (crop around the user, max-pooled to `CELL_PX = 3` px cells,
  i.e. 80×80 for the 240×240 crop), with real OCR'd IST timestamps.
- **owner grid**: which patch claims each rain cell of the newest frame
  (0 = unclaimed → global drift).
- **patches**: per-patch velocity (px/10 min) + decay params mirroring
  nowcast's `_patch_fade` rules (`track` mode with measured decay_rate, or
  `new`-cell synthetic lifecycle).
- **places**: named cities inside the crop (curated per-radar `PLACES` list in
  radar_scene.py, georeferenced via that radar's `latlon_to_pixel`), drawn as
  labels on the canvas like IMD's city abbreviations.
The player places history frames lag-corrected (latest frame at t = −lag),
cross-fades between observed frames, and for t > −lag advects each cell by its
owner's velocity with decay fade — continuous interpolated motion from ~−60
to +60 min. Rain is drawn as a smoothed heatmap (two-pass canvas blur), with a
dBZ color legend (Drizzle → Extreme) under the player. dBZ classification is vectorized (`_classify_dbz`, same color
table/tolerance as fuzzy.py). This supersedes the base64-PNG player below,
which is kept as fallback.

### Forecast animation (forecast_gif.py)
Renders the exact same simulation as frames at now/+15/+30/+45/+60: each patch
advected by its own vector and faded by its decay track, unclaimed rain drifts
with the global vector, cropped to the 120-px search circle around the user.
Served as GIF (`/nowcast/forecast_gif`) or base64-PNG frame list for a
scrubbable player (`/nowcast/forecast_frames`).

### Verification (verification.py)
Every `/predict_waypoints` and `/nowcast` claim is logged into
`verification.db` (pending). On each radar refresh, pending predictions whose
target time matches a new frame (±5 min) are graded by checking real rain in a
2-px neighborhood → hit / false_alarm / miss / correct_clear; >200 min
unmatched → expired. `/stats/accuracy` aggregates POD, FAR, CSI.

### Alerts (alerts.py)
Web-push (VAPID) subscriptions stored in `alerts.db` with a per-subscription
state machine `clear → approaching → raining`. After each radar refresh, each
covered saved location gets a 4-slot nowcast:
- `clear→approaching`: "Rain approaching (~N min)" heads-up.
- `→raining`: "Rain right now" arrival ping (fires even after a heads-up);
  adds "likely to ease within ~15 min" if the +15 slot is clear.
- Separate 45-min cooldowns for heads-up vs arrival; dead subscriptions
  (HTTP 404/410) are pruned. VAPID keys from env
  (`VAPID_PRIVATE_KEY_PEM`/`VAPID_PUBLIC_KEY`, paste-mistake-tolerant) or
  `vapid_keys.json` in dev. Push TTL 3600 (WNS rejects 0).

### Chatbot (chatbot.py)
Gemini 2.5 Flash with function calling; tools are the **in-process** endpoint
functions registered by main.py (`get_nowcast`, `get_route_rain`,
`get_rain_movement`, `get_accuracy_stats`) — no HTTP self-calls. Multiple
Gemini keys rotate round-robin (`GEMINI_API_KEY[,2,3…]` or `GEMINI_API_KEYS`
comma list); Groq llama-3.3-70b is the quota fallback (`GROQ_API_KEY`).
Streams SSE: `{"type":"text"|"tool"|"done"|"error"}`. System prompt enforces:
never judge coverage from a place name — always call get_nowcast and trust
`in_radar_bounds`.

## 8. Georeferencing (georef*.py)

Per radar, a **quadratic** fit (`px = c0 + c1·lat + c2·lon + c3·lat·lon +
c4·lat² + c5·lon²`, same for py) least-squares fitted to 7 manually measured
Ground Control Points (cities with known lat/lon and pixel positions; ≤4 px
residual for Delhi). Exposes `latlon_to_pixel`, `pixel_to_latlon`,
`is_within_radar`, `IMAGE_WIDTH/HEIGHT`, `CENTER_LAT/LON`. Scale ≈ 0.877 km/px.

## 9. API endpoints (main.py)

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness (also the keep-alive target) |
| `/debug/cache` | GET | Per-radar cache freshness/ready diagnostics |
| `/movement` | GET | Global rain movement over Delhi NCR |
| `/radar/gif?radar=` | GET | Latest downloaded radar GIF (delhi/lucknow/patna/bhopal) |
| `/frames/latest?n=&force=` | GET | Latest frame URLs + timestamps + lag info |
| `/radar/frames*` | static | Extracted PNGs per radar (`frames_lucknow` etc.) |
| `/predict_waypoints` | POST | **Main route endpoint.** Body: `{waypoints:[{lat,lon,eta_mins}]}`; auto-selects radar from route midpoint; returns enriched waypoints + patch_analysis + summary |
| `/predict` | POST | Legacy: start/end coords → server builds waypoints (Delhi only) |
| `/nowcast` | POST | `{lat,lon}` → 8 slots + summary, auto radar selection |
| `/nowcast/radar_scene?lat=&lon=` | GET | v2 animation scene JSON (dBZ history grids + patch motion/decay params) |
| `/nowcast/forecast_gif?lat=&lon=` | GET | Forecast animation GIF |
| `/nowcast/forecast_frames?lat=&lon=` | GET | Same as base64 PNG frames for the scrubber |
| `/alerts/vapid_public_key` | GET | Push public key |
| `/alerts/subscribe` / `/alerts/unsubscribe` / `/alerts/test` | POST | Push subscription management |
| `/stats/accuracy?days=` | GET | Verified POD/FAR/CSI |
| `/chat` | POST | Chatbot, SSE stream |

CORS: allow all. Validation: coordinates must be inside rough India bounds
(6–38 N, 68–98 E).

## 10. Frontend (frontend/src)

Single-page React app, all UI in **App.jsx** (~1900 lines), three tabs
(`route` / `nowcast` / `chat`) via `TabBar`:

- **Route tab (default):** Nominatim place search with autocomplete
  (viewbox-biased), route fetched via OSRM/ORS (needs `VITE_ORS_API_KEY` for
  OpenRouteService), waypoints sampled every ~5 driving minutes and POSTed to
  `/predict_waypoints`. Results render as: `RainTimelineBar` (colored journey
  timeline), colored route polyline segments, and **RouteMap.jsx** — a
  lazy-loaded Leaflet map that animates a car driving the route (~3.2 s),
  pausing at each rainy stop; clicking a stop opens `JourneyStopCard` with an
  on-demand `/nowcast` for that point.
- **Nowcast tab:** location search or geolocation → `NowcastSlots` (8 slot
  cards with probability bars), `RadarScenePlayer` (canvas player over
  `/nowcast/radar_scene`; `ForecastRadarPlayer` remains as unused fallback),
  and `RainAlertsCard` (registers `public/sw.js`,
  subscribes to push via `/alerts/subscribe`).
- **Chat tab (`ChatPage`):** streams `/chat` SSE, shows tool-call status.

Cold-start handling: `postWithWarmup` retries for up to 3 min with a
"server warming up" status (Render free tier sleeps); `warmBackend()` pings
`/health` on app load. `API_BASE`: localhost:8000 in dev, else
`VITE_API_BASE` or the Render URL.

`NetworkLayers.jsx` is an unrelated OSI-model demo component — **not imported
anywhere**; ignore it. `dist/` is a committed production build.

## 11. Operational constraints & conventions

- **512 MB RAM (Render free tier) drives most design decisions:** last-6-frames
  only, explicit `del` + `gc.collect()`, BBoxMask patch storage, LRU cap of
  2 in-memory radars, clutter mask disabled, no startup warmup (lazy load on
  first request).
- **No Tesseract in production** — `timestamp_match.py` (digit template
  matching against `ts_templates/`) is the prod timestamp reader; Tesseract is
  dev-only ground truth.
- **Best-effort philosophy:** alerts/verification/patch/decay failures are
  caught and printed, never fail a request. External fetches fail-open.
- **Database (`db.py`):** when `DATABASE_URL` is set (Render → Supabase Postgres,
  Session pooler URL), alerts + verification use Postgres — this is what makes
  alert subscriptions survive deploys/restarts (Render's disk is ephemeral, so
  the old SQLite files were wiped on every deploy). Without `DATABASE_URL`
  (local dev) they fall back to the SQLite files, zero setup. `db.py` keeps the
  sqlite3 call style and translates `?` placeholders to `%s`; schemas use
  `db.AUTOINC_PK` for the dialect difference. `/health` reports which backend
  is active (`"db": "postgres"|"sqlite"`).
- SQLite (dev) uses WAL; both backends are guarded by a module-level `threading.Lock`.
- All timestamps IST-aware (`UTC+5:30`); DB rows store UTC ISO.
- Deploy version = `RENDER_GIT_COMMIT` env (verification's `engine_version`).
- Env vars: `DATABASE_URL` (Supabase Postgres; unset = SQLite dev mode),
  `VAPID_PRIVATE_KEY_PEM`, `VAPID_PUBLIC_KEY`, `GEMINI_API_KEY*`,
  `GEMINI_API_KEYS`, `GROQ_API_KEY`; frontend: `VITE_API_BASE`,
  `VITE_ORS_API_KEY`.
- A Flutter client (`garaj_baras_flutter/`) existed but is **deleted** (staged
  deletions in git); the React app is the only client.
- The two root `.txt` files are older prose/PPT summaries — useful narrative
  but stale in places (they say Bhopal is disabled and describe blocking
  refreshes; both superseded by LRU eviction + background-refresh design
  described above).

## 12. Running locally

```powershell
# Backend (Windows)
cd backend
.\venv\Scripts\activate          # venv is committed in backend/venv
uvicorn main:app --reload --port 8000

# Frontend
cd frontend
npm install
npm run dev                      # talks to http://127.0.0.1:8000 automatically
```

First request per radar triggers a full GIF download + processing (~5–8 s);
subsequent requests are served from cache (<50 ms) for 10 minutes.
