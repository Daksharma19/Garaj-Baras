# Garaj Baras — Project Architecture

> **Purpose of this file:** Complete architecture reference for the whole codebase.
> Read this INSTEAD of exploring the repo. It covers what the project does, every
> module's role, the data pipeline, all API endpoints, the frontend structure,
> deployment, and operational constraints. Only open source files when you need
> the exact implementation of something specific.
>
> Last updated: 2026-09-29.

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
refreshes are lazily triggered by user requests against a TTL cache, with
GitHub Actions providing an external wake-and-sweep cadence for alerts on the
free-tier deployment.

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
│   ├── radar_lucknow.py / radar_patna.py / radar_bhopal.py / radar_jaipur.py / radar_paradip.py / radar_patiala.py / radar_nagpur.py ← per-radar wrappers over radar.py helpers
│   ├── georef.py                ← Delhi pixel↔latlon quadratic GCP model
│   ├── georef_lucknow.py / georef_patna.py / georef_bhopal.py / georef_jaipur.py / georef_paradip.py / georef_patiala.py / georef_nagpur.py ← per-radar georef models
│   ├── optical_flow.py          ← rain mask isolation + global movement vector (Farneback)
│   ├── patches.py               ← per-storm-cell (blob) motion + route intercept scoring
│   ├── decay.py                 ← per-cell dBZ trend tracks (stable/weakening/dying/dead)
│   ├── fuzzy.py                 ← RGB → dBZ → label; enrich_results() for route waypoints
│   ├── prediction.py            ← OSRM route building + rain-mask-shift route check
│   ├── nowcast.py               ← 8-slot point forecast engine (compute_nowcast_slots)
│   ├── forecast_gif.py          ← renders the forecast animation (GIF + frame payloads)
│   ├── db.py                    ← DB backend switch: Postgres when DATABASE_URL is set (prod), else SQLite
│   ├── auth.py                  ← Supabase Auth JWT verification (get_current_user/get_optional_user deps)
│   ├── accounts.py              ← users + saved_locations tables (accounts.db in dev)
│   ├── bbox_mask.py             ← BBoxMask: patch masks as tight crops (~1/1000 memory)
│   ├── timestamp_match.py       ← digit template-matching timestamp reader (prod, no Tesseract)
│   ├── alerts.py                ← web-push rain alerts, SQLite alerts.db, state machine
│   ├── journeys.py              ← "journey guardian": server-side dead-reckoned rain watch for live journeys (push warnings while the phone screen is off)
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
│   ├── src/RouteMap.jsx         ← lazy-loaded Leaflet map: colored route segments + animated journey car + rain/fog layer toggles; `navMode` = full-screen heading-up nav map (leaflet-rotate)
│   ├── src/LiveJourney.jsx      ← navigation UI: GPS tracking, ORS turn-by-turn card, speed/ETA bar, rain + fog chips, 5-min radar re-sync, journey-guardian registration
│   ├── src/fog.js               ← Open-Meteo hourly visibility along route waypoints (browser-side, keyless) → fog zones
│   ├── src/mapTiles.js          ← base-map tiles: Mapbox (dark-v11 / navigation-night-v1 / light-v11) when MAPBOX_ACCESS_TOKEN is set (root .env, injected by vite.config.js), else CARTO/OSM fallback
│   ├── src/leafletSetup.js      ← exposes window.L before `leaflet-rotate` loads (the plugin patches the global)
│   ├── src/NetworkLayers.jsx    ← UNRELATED OSI-layers demo component; not imported anywhere
│   ├── public/sw.js             ← service worker: push notifications + offline fallback
│   ├── public/manifest.webmanifest, icon-*.png, apple-touch-icon.png, offline.html ← PWA assets
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

**Eight radars, each with its own `radar_*.py` + `georef_*.py` pair:**

| Radar | Center | Notes |
|---|---|---|
| Delhi (Palam) | 28.556 N, 77.100 E | Primary/default. Current image: `caz_delhi.gif`. |
| Lucknow | 26.847 N, 80.946 E | GIF is 704×594 (not 880×720); own OCR crop **and own radar-circle crop** (392×392 at box `(0,176,392,568)` — the circle sits at a different offset than Delhi's, so it cannot reuse Delhi's crop box). Current image: `caz_lkn.gif`. |
| Patna | 25.591 N, 85.096 E | Current image: `caz_ptn.gif`. |
| Bhopal | 23.288 N, 77.337 E | **Re-enabled** (older docs say disabled). BBoxMask compression + LRU state eviction (max 2 radars in RAM) made it fit in 512 MB. Current image: `caz_bhp.gif`. |
| Jaipur | 26.824 N, 75.812 E | Raw GIF is 880×720 but the map panel is at the bottom-left, so it has its own crop box `(0,200,520,720)` → 520×520 (Delhi's OCR box works). **250 km radar** (not 300): disc radius 257 px → 0.973 km/px true scale. GCPs generated from an azimuthal-equidistant fit around the disc center (= Jaipur airport), validated against the frame's airport markers. Current image: `caz_jpr.gif`. |
| Paradip | 20.264 N, 86.611 E | **Coastal** radar (Odisha). Same bottom-left panel layout/crop box as Jaipur → 520×520, Delhi OCR box. **250 km radar**: disc center + scale (0.971 km/px) confirmed by fitting the range rings over the open sea, then GCPs from a north-up azimuthal-equidistant fit about the site, validated against Odisha city diamonds. Half the disc is ocean (grey). Current image: `caz_pdp.gif` (URL code `PDP`). |
| Patiala | 30.354 N, 76.454 E | Punjab/Haryana. Same bottom-left panel layout/crop box as Jaipur/Paradip → 520×520, Delhi OCR box. **300 km radar**, but uses an **exact ring-derived AEQD model** (like Lucknow): station crosshair/ring-center at crop (260, 259), scale 0.867 px/km from the 100/200/300 km rings (radii 87/173/260 px). Center WGS84 fixed by the graticule (30/31/32 N at py 293/197/101; 75–78 E at px 138/223/305/389), which agrees with the rings and is IMD's own accurate grid — so the center is set to (30.354, 76.454), ~a few km off nominal "Patiala city". Current image: `caz_ptl.gif`; animation `PTL_MAXZ.gif` (URL code `PTL`). |
| Nagpur | 21.15 N, 79.05 E | Vidarbha / central India (Maharashtra, MP, Chhattisgarh, Telangana). Same bottom-left panel layout/crop box as Jaipur/Paradip/Patiala → 520×520, Delhi OCR box. **250 km radar** using an **AEQD model** (like Lucknow/Patiala): station crosshair/disc-center at crop (260, 259), scale 1.028 px/km (250 km disc edge ≈ 257 px, identical rendering to Jaipur/Paradip). Nagpur's graticule is **unlabeled**, so the center is anchored by a least-squares fit of the AEQD projection to surrounding real-city labels (Betul/Wardha/Seoni/Chandrapur/Akola/Yavatmal/Bhandara/Gadchiroli/Jabalpur) → (21.15, 79.05) at the Sonegaon site, cross-checked against the NGP crosshair. Current image: `caz_ngp.gif`; animation `NGP_MAXZ.gif` (URL code `NGP`). |

**Current-image augmentation (all radars):** IMD's animation GIF rebuilds
lazily and can lag 50–60+ min behind its single "current radar" image
(`caz_*.gif`, same layout as that radar's GIF frames). After extracting GIF
frames, each radar's refresh fetches the current image and appends it as the
newest frame when its OCR timestamp is strictly newer (never duplicated;
unreadable timestamp → skipped). Implemented in `radar.py
augment_with_current_image()`; Delhi calls it directly in `_do_delhi_refresh`,
the other radars via their `augment_current()` wrappers (which pass their own
OCR/crop boxes).

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
      dx_10, dy_10, speed, direction, max_dbz, mean_dbz}; masks stored as BBoxMask.
      mean_dbz is a texture feature: the peak/mean dBZ ratio (spikiness) feeds
      the convective-vs-stratiform score in nowcast.py.
  → decay.py: compute_decay_tracks(): link the same blob across all 6 frames
      (predict-next-position via global flow, nearest blob ≤35 px) → mean dBZ vs
      REAL minutes between frames (gaps vary) → Theil–Sen fit (median of pairwise
      slopes, outlier-immune) → decay_rate (dBZ per 10 real minutes) →
      project_dbz(eta) via dbz_change(): rate is clamped (growth +1.5, decay −8)
      and exponentially damped with lead time (τ = 45 min), so the projected
      change saturates at rate×4.5 dBZ instead of extrapolating linearly for
      hours. **Area trend (survivor-bias fix):** mean dBZ over a thresholded
      mask misses shrinking storms (weak edges leave the mask first, so the
      mean stays flat while the blob dies), so each track also fits a clamped
      Theil–Sen trend on ln(area); project_area_fraction() (same τ damping,
      growth capped at 1.0) feeds _classify alongside projected dBZ.
      Status: dead (<8 dBZ or <15% area left) / dying (<18 or <35%) /
      weakening (<32 & declining, or <65%) / growing (measured rate ≥ +1
      dBZ/10 min and footprint not shrinking — pre-peak cell) / stable.
      All consumers (nowcast, forecast_gif) use dbz_change() +
      project_area_fraction(); radar_scene exports the clamped dBZ rate only
      (its frontend player fades linearly and doesn't model area shrink).
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

**Timestamp date inference + glitched-frame rejection:** the IMD panel carries
only a time, no date. Both readers (`timestamp_match.py`, `radar.py
parse_radar_timestamp_text`) assume "today" in the panel's timezone but **roll
back a day when the built time lands >2 h in the future**. Separately, IMD
intermittently wedges a frame from a **completely different day** into the
Delhi feed — observed: a `4 JUL` frame (panel `16:12:24Z`) stuck in a `13 JUL`
feed, in **both** the animation GIF and the `caz_delhi.gif` current image,
while the rest of the animation is current. `extract_frames` therefore **drops
glitched frames outright** (removes them, never nulls — nulling would let the
gap-fill revive the frame with a fabricated current timestamp on stale
imagery): a frame >5 min in the future, or >6 h older than the freshest read
(a MAXZ GIF spans ≤~4.5 h, a stray is a day+ off; the relative window leaves a
genuinely-down radar's uniformly-old-but-clustered frames intact).
`augment_with_current_image` applies the same future guard so a glitched
current image is never appended. Net effect: when IMD serves a stray/no fresh
frame, we honestly report the freshest *valid* frame (`down`/large lag) instead
of a fabricated future/night timestamp. Without this, one stray future-dated
frame became the monotonic-ordering baseline, rejected every real frame after
it, and back-filled the whole history with bogus future timestamps (symptom:
newest frame showing ~night IST like `22:02` + lag falling back to the flat
25-min estimate).

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
- Intensity: `projected_dbz = raw_pixel_dbz + dbz_change(decay_rate, eff_mins)`
  (damped, saturating trend — see decay.py above);
  dBZ → probability via a motion-ensemble mapping (~44 dBZ → ~92%, 20 → ~35%).
- **New-cell lifecycle (texture-conditioned, "Tier 1"):** a blob with no
  measured trend (single observation / untracked) no longer gets one fixed
  prior. Two mechanisms replace it:
  - **Motion:** a fresh pop-up (|v| below FRESH_POPUP_VELOCITY_THRESH) is
    advected with the **global flow vector** instead of being skipped/parked —
    so dwell time at a point (blob extent ÷ steering speed) falls out of the
    projection geometry. Mirrored in forecast_gif.py and radar_scene.py so
    the animations agree with the numbers.
  - **Lifetime:** `new_cell_params(area, peak_dbz, mean_dbz, widespread_frac)`
    computes a `convective_score` (0 = stratiform, 1 = convective) from blob
    area (log-scaled 150→3000 px), peak dBZ (30→45), peak/mean spikiness, and
    the widespread-shield fraction (≥50% coverage in a 60-px circle pulls hard
    toward stratiform). The score interpolates the synthetic decay between
    −1 (stratiform) and −4 dBZ/10 min (convective) and the max-assert horizon
    between 105 and 30 min. Cells still die below 10 dBZ. The old fixed
    −2.5/30-min constants remain only as the neutral fallback when no texture
    features are available. This makes a small spiky heat-storm a ~30-min
    firecracker and a broad moderate blanket an all-horizon soaker.

### Radar scene v2 (radar_scene.py) — data-driven animation
`/nowcast/radar_scene?lat=&lon=` returns a compact JSON scene (~50–70 KB) the
frontend canvas player (`RadarScenePlayer` in App.jsx) renders client-side:
- **history**: each cached frame (deduped by timestamp, ~10-min cadence) as a
  base64 dBZ grid (crop around the user, max-pooled to `CELL_PX = 2` px cells
  (~1.75 km/cell)), with real OCR'd IST timestamps. The crop is `VIEW_RADIUS_PX`
  = half the 120-px prediction search radius (a tighter, more zoomed-in
  viewport + ~4× lighter payload); predictions still use the full radius.
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
with the global vector, cropped to `VIEW_RADIUS_PX` (half the 120-px search
radius) around the user.
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
covered saved location gets the full 8-slot nowcast (0-105 min, same horizon
as the Nowcast tab):
  - `clear→approaching`: "Rain approaching (~N min)" heads-up — can fire for
    rain anywhere in the 0-105 min horizon, not just the near slots. Near slots
    (now, +15, +30 min) only count when probability > 70%; all other remaining
    slots only count when probability > 80% (strict user-defined caps implemented in
    `_slot_is_rain`, applied to both the trigger and the ease/resume scan).
- **Peak-intensity naming:** when a heavier category is due within the next
  ~45 min (`PEAK_LOOKAHEAD_SLOTS` = 3 slots past the first rainy one), the
  alert names THAT intensity, not the light rain at the leading edge — a
  "Very Light → Heavy" ramp fires "Heavy Rain approaching … building to Heavy
  Rain by ~30 min" (`_peak_rain_slot` returns the earliest slot at the peak
  category; `_intensity_rank` orders the `fuzzy.dbz_to_label` categories).
  Only the message copy changes; the clear/approaching/raining state
  transition is still driven by the leading-edge slot.
- `→raining`: "Rain right now" arrival ping (fires even after a heads-up);
  appends "Expected to ease in ~N min" (and, if rain resumes afterward, "may
  pick up again around ~M min") via `_ease_and_resume_note`, scanning the same
  8-slot horizon.
- Separate 45-min cooldowns for heads-up vs arrival; dead subscriptions
  (HTTP 404/410) are pruned. VAPID keys from env
  (`VAPID_PRIVATE_KEY_PEM`/`VAPID_PUBLIC_KEY`, paste-mistake-tolerant) or
  `vapid_keys.json` in dev. Push TTL 3600 (WNS rejects 0).

**Two triggers keep alerts timely (beyond the browse-driven refresh):**
- **Instant check on subscribe:** `/alerts/subscribe` spawns a background
  `_instant_alert_check` (main.py) — detect radar, ensure fresh state, then
  `process_alerts(only_endpoint=...)` for just the new subscription. If it's
  already raining, the user is notified within seconds.
- **Scheduled sweep:** `GET /tasks/sweep_alerts?token=` (`_sweep_alerts` in
  main.py) reads every subscription's coords (`alerts.all_subscription_coords`),
  maps them to distinct radars via `_detect_radar`, and asynchronously refreshes each.
  To prevent HTTP timeouts by external cron providers, the endpoint returns immediately
  (`{"ok": true, "status": "sweep_queued"}`) and runs the sweep in a background thread.
  A global non-blocking lock (`_sweep_bg_lock`) ensures that at most one sweep runs at
  a time, protecting the 512 MB memory limit. A stale-cache refresh runs `process_alerts`
  internally on completion; if the cache was already fresh (someone browsed that city
  recently), the refresh no-ops so `_sweep_alerts` calls `process_alerts` explicitly
  itself — every sweep tick checks alerts regardless of recent browsing traffic. The
  `keepalive.yml` GitHub Action uses a **staggered** cadence: `/health` every
  5 min to keep Render warm, then `/tasks/sweep_alerts` every 10 min at
  minute `2,12,22,32,42,52` so the sweep is less likely to absorb the cold
  start itself. This keeps alerts firing on schedule instead of only when
  someone browses that city. Optional
  `SWEEP_TOKEN` env gates the endpoint (matching repo secret sent by the
  workflow). Both paths use the `_RADAR_REGISTRY` table (name → refresh fn /
  cache / ready event / georef).
  **Caveat:** GitHub Actions `schedule:` cron is best-effort, not exact — GitHub
  queues scheduled runs and can delay them, especially at :00/:05/:10 minute
  marks when load is high; delays of several minutes (occasionally more) are
  normal, and workflows are auto-disabled after 60 days with no repo commits.
  For tighter, more reliable timing, an external pinger (cron-job.org,
  UptimeRobot, etc.) hitting `/tasks/sweep_alerts` is more dependable than
  relying on GitHub's scheduler alone.

### Journey guardian (journeys.py)
Server-side rain watch for in-progress live journeys — solves "web pages can't
run GPS with the screen off". `/journey/start` registers the route samples
(lat, lon, cum_km), planned/observed speed, and a push subscription (one
active journey per endpoint; re-start replaces). The server **dead-reckons**
the user's position (anchor progress + speed × elapsed); whenever the app is
open, each 5-min live sync re-anchors via `/journey/update` with real GPS
progress, so drift only accumulates while the screen is off. Each scheduled
sweep (`_sweep_alerts`) includes journeys' estimated positions in the radar
refresh set, then `process_journeys()` checks the route waypoints ahead of
each estimate (≤45 min lookahead, same engine as `/predict_waypoints`:
`check_route_rain` + `enrich_results`) and pushes "🌧 {label} ahead on your
journey (~N min)" when rain falls within 30 min of travel (25-min per-journey
cooldown; ≤5 min ETA uses "right ahead" copy). Journeys auto-expire at
estimated arrival or 1.5× planned duration + 30 min; dead push subscriptions
deactivate their journey. Table `journeys` lives in the alerts DB
(Postgres in prod via db.py, alerts.db SQLite in dev).

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

**Exception — Lucknow, Patiala and Nagpur use an exact azimuthal-equidistant
(AEQD) model, not a quadratic GCP fit.** (Patiala's ring-derived center is also
cross-checked against the graticule; Nagpur's graticule is unlabeled so its
center is a least-squares fit to surrounding real-city labels; see the radar
table in §4.) The IMD image's 50–250 km range rings are concentric
circles, so echoes are plotted in true range/azimuth space; the ring center
(196.547, 195.576 px) and scale (0.78693 px/km) were measured directly from
the rings (joint fit across frames, <1 px residual), and forward/inverse are
closed-form spherical geodesics. The old quadratic fit used GCPs read off
IMD's basemap town dots, which are drawn sloppily (Kannauj's dot is ~20 km
off); the quadratic bent to absorb those errors and extrapolated badly east
of Jaunpur (82.68E), displaying Gorakhpur-area echoes ~8 km south of reality.
When any other radar shows a similar edge-of-frame offset, re-derive it from
its range rings the same way rather than adding more town-dot GCPs.

**A georef's GCP fit is only valid for the exact crop box its own `radar_*.py`
produces.** `extract_frames()` (radar.py) takes a `crop_box` param — Delhi,
Patna, and Bhopal share its default (their raw GIFs are all 880×720 with the
same panel layout), but Lucknow's raw GIF is 704×594 with the circle at a
different offset, so `radar_lucknow.py` defines and always applies its own
`CROP_BOX`. (This was previously a silent bug: Lucknow reused Delhi's crop
box, which pulled in the RHI/legend panel and black-padded the missing rows —
every Lucknow pixel was desynced from `georef_lucknow.py`'s fit. Fixed by
giving Lucknow its own crop box and re-deriving the fit for that crop.) Adding
a new radar whose source GIF isn't 880×720-with-Delhi's-layout must follow the
same pattern: measure that radar's own crop box from a live frame before
reusing any GCP numbers.

## 9. API endpoints (main.py)

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness (also the keep-alive target) |
| `/debug/cache` | GET | Per-radar cache freshness/ready diagnostics |
| `/movement` | GET | Global rain movement over Delhi NCR |
| `/radar/gif?radar=` | GET | Latest downloaded radar GIF (delhi/lucknow/patna/bhopal/jaipur/paradip/patiala/nagpur) |
| `/frames/latest?n=&force=` | GET | Latest frame URLs + timestamps + lag info |
| `/india-radar/metadata` | GET | Station locations, operational ranges, and India mosaic bounds for the separate national map UI. |
| `/india-radar/mosaic.png` | GET | Transparent India-wide reflectivity composite. It sequentially uses existing station states and caches only the rendered PNG for 5 minutes, preserving the two-radar heavy-state LRU. |
| `/radar/refresh` | POST | User-triggered "check for a new frame". Body `{lat,lon}` → detects radar, forces a **blocking** re-download + reprocess (bypasses TTL), returns `new_frame` (did IMD publish a newer frame than cached), plus latest/previous timestamps + lag. Bounded by a bg-lock (no double-refresh) and a frontend 60 s cooldown. |
| `/radar/frames*` | static | Extracted PNGs per radar (`frames_lucknow` etc.) |
| `/predict_waypoints` | POST | **Main route endpoint.** Body: `{waypoints:[{lat,lon,eta_mins}]}`; auto-selects radar from route midpoint; returns enriched waypoints + patch_analysis + summary |
| `/predict` | POST | Legacy: start/end coords → server builds waypoints (Delhi only) |
| `/nowcast` | POST | `{lat,lon}` → 8 slots + summary, auto radar selection |
| `/nowcast/radar_scene?lat=&lon=` | GET | v2 animation scene JSON (dBZ history grids + patch motion/decay params) |
| `/nowcast/forecast_gif?lat=&lon=` | GET | Forecast animation GIF |
| `/nowcast/forecast_frames?lat=&lon=` | GET | Same as base64 PNG frames for the scrubber |
| `/journey/start` | POST | Register a live journey for the server-side guardian: `{subscription, waypoints:[{lat,lon,cum_km}], speed_kmh}` → `{journey_id}` |
| `/journey/update` | POST | Re-anchor the guardian's dead-reckoning with real GPS progress: `{journey_id, progress_km, speed_kmh?}` |
| `/journey/end` | POST | Deactivate a journey (explicit end or arrival) |
| `/alerts/vapid_public_key` | GET | Push public key |
| `/alerts/subscribe` / `/alerts/unsubscribe` / `/alerts/test` | POST | Push subscription management |
| `/stats/accuracy?days=` | GET | Verified POD/FAR/CSI |
| `/chat` | POST | Chatbot, SSE stream (frontend gates the tab behind sign-in) |
| `/me` | GET | **Auth.** Upsert + return the signed-in user |
| `/locations` | GET/POST | **Auth.** List / add saved locations (cap 10/user) |
| `/locations/{id}` | PATCH/DELETE | **Auth.** Rename/toggle-alerts / delete a saved location |

CORS: allow all. Validation: coordinates must be inside rough India bounds
(6–38 N, 68–98 E).

**Auth (auth.py + accounts.py):** Supabase Auth (Google OAuth + email OTP) on
the frontend via `@supabase/supabase-js`; the backend verifies the Supabase
JWT locally (pyjwt) — no per-request network call. Verification auto-picks by
the token's `alg`: **HS256** (shared `SUPABASE_JWT_SECRET`) or **ES256/RS256**
(asymmetric signing keys, verified via the project JWKS at
`SUPABASE_URL/auth/v1/.well-known/jwks.json`, cached). **Policy: route +
nowcast are fully public; sign-in gates only the
extras** — Ask AI tab, rain alerts, saved places. `get_current_user` (401
without token) / `get_optional_user` FastAPI deps. `accounts.py` owns `users`
(id = Supabase uid) and `saved_locations` tables (Postgres in prod,
`accounts.db` SQLite in dev, same `db.py` switch). `alerts.subscriptions`
gained a nullable `user_id` column: subscriptions from signed-in users are
tied to the account, anonymous ones keep working (`user_id NULL`). Dev
fallback: with NEITHER `SUPABASE_JWT_SECRET` nor `SUPABASE_URL` set, tokens are
decoded WITHOUT signature verification (loud warning) so localhost works before
Supabase is wired; never run prod like that.

## 10. Frontend (frontend/src)

Single-page React app, all UI in **App.jsx** (~1900 lines), three tabs
(`route` / `nowcast` / `chat`) via `TabBar`:

- **First-run onboarding (`Onboarding.jsx`):** 3-card stepped intro (route
  colors = live radar not a forecast / Nowcast ~2 h horizon / alerts + live
  journeys), shown once (`gb_onboarded` in localStorage) and re-openable via
  the "How it works" link under the planner hero. A `ServerWakeNote` banner
  (App.jsx) pings `/health` on load and warns about the Render cold boot if
  it takes >3 s; `postWithWarmup` shows phased warmup copy instead of a
  seconds counter.

- **Route tab (default):** Nominatim place search with autocomplete
  (viewbox-biased), route fetched via OSRM/ORS (needs `VITE_ORS_API_KEY` for
  OpenRouteService), waypoints sampled every ~5 driving minutes and POSTed to
  `/predict_waypoints`. Results render as: `RainTimelineBar` (colored journey
  timeline), colored route polyline segments, and **RouteMap.jsx** — a
  lazy-loaded Leaflet map that animates a car driving the route (~3.2 s),
  pausing at each rainy stop; clicking a stop opens `JourneyStopCard` with an
  on-demand `/nowcast` for that point. Merged waypoints carry `_cumKm` (their
  real distance along the route) so segment coloring survives live-journey
  re-predictions where passed waypoints clamp to eta 0.
- **Fog layer (`fog.js`):** IMD radar can't see fog, so after each route
  prediction the frontend fetches Open-Meteo `hourly=visibility` for all
  waypoints in one multi-location GET (no backend involvement — keeps the
  512 MB server untouched), picking each waypoint's forecast hour at its ETA.
  <1 km = fog, <500 m = thick, <200 m = dense. Contiguous foggy waypoints
  become fog zones, drawn as a dashed haze on the route (toggleable, like the
  rain forecast coloring), summarized in a `fog-banner` on the results page,
  and shown as a "Fog in ~N min · visibility X" chip while navigating.
  Refreshed every 20 min during navigation. Failures are silent (fog optional).
- **Navigation mode (`LiveJourney.jsx` + `RouteMap navMode`):** "Start
  navigation" on the results screen opens a full-screen view (portal to
  `document.body`, `.nav-screen`): a second RouteMap instance with
  `rotate: true` (leaflet-rotate) that follows the puck heading-up (bearing =
  360 − route heading, puck framed in the lower third; compass button toggles
  north-up), a green top card with the next maneuver + distance + street and a
  "Then" hint, a speed bubble, and a bottom bar (Exit · time left · km left ·
  arrival clock · expand). Rain/fog chips sit above the bar; the expanded
  sheet holds the full rain hero, guardian/sync status and the upcoming
  directions list. Turn-by-turn comes from the ORS response's
  `segments[].steps` (type/name/exit_number/way_points[0] kept in
  `routeSteps`); each step is located by the cumulative km of its geometry
  index and the next one is the first step ahead of current progress. Heading
  is derived from the route geometry at the current progress (steadier than
  GPS course). The inline results map is not rendered with a live position
  (only the nav map is). Two clocks: a 1 s client tick (GPS `watchPosition` snapped
  to the route + dead-reckoned between fixes at an EMA rolling speed) drives
  a live "rain in ~N min" countdown, remaining km, and arrival time; every
  5 min a radar sync POSTs `/radar/refresh` then re-sends all waypoints to
  `/predict_waypoints` with ETAs recomputed from observed speed, recoloring
  the map and re-anchoring the countdown (honest "updated from radar" note
  when it shifts >2 min or a new frame landed). Extras: screen wake-lock,
  missed-sync catch-up on `visibilitychange`, off-route chip (>1 km), a
  dev-only simulated drive (`import.meta.env.DEV`). On start it also registers
  the server-side **journey guardian** (push subscription + route + speed →
  `/journey/start`; each sync re-anchors via `/journey/update`; End/arrival →
  `/journey/end`) so rain warnings arrive as push notifications while the
  screen is off. The nav map shows a direction arrow with follow mode (pan
  pauses on drag; Re-center button). Note: RouteMap gets its Leaflet map via
  `ref={setMapRef}` — react-leaflet v5 has no `whenCreated`.
- **Nowcast tab:** location search or geolocation → `NowcastSlots` (8 slot
  cards with probability bars), `RadarScenePlayer` (canvas player over
  `/nowcast/radar_scene`; `ForecastRadarPlayer` remains as unused fallback),
  then a `SignInGate` wrapping `RainAlertsCard` (registers `public/sw.js`,
  subscribes to push via `/alerts/subscribe` with the auth header) and
  `SavedPlaces.jsx` (CRUD on `/locations`; tapping a place loads it into the
  nowcast picker).
- **India Radar tab:** separate lazy-loaded `IndiaRadarMap.jsx` Leaflet view
  using OpenStreetMap tiles, transparent `/india-radar/mosaic.png` echoes,
  station range rings, and an even-odd no-data mask outside coverage circles.
  It does not reuse or alter the route/nowcast radar players.
- **Chat tab (`ChatPage`):** streams `/chat` SSE, shows tool-call status.
  Gated behind sign-in (`SignInGate`).
- **Auth (`auth.jsx` + `supabase.js`):** `AuthProvider` context (session,
  `openLogin`, `authHeaders`), `AccountButton` in every nav, `LoginModal`
  (Google OAuth + 6-digit email OTP), `SignInGate` prompt card. When
  `VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY` are unset the client is null
  and gated features show a "not configured" note; route + nowcast unaffected.

**PWA:** the app is installable. `public/manifest.webmanifest` (name, purple
theme, standalone display) + PNG icons (192/512/maskable + apple-touch-icon,
generated from the brand SVG). `public/sw.js` is registered on app load in
`main.jsx` (not just on alert subscribe); besides push it precaches
`offline.html` and serves it as a fallback for failed page navigations —
API calls, radar frames, and dev modules are deliberately never cached (live
radar data must stay fresh). An `InstallPrompt` banner component in App.jsx
listens for `beforeinstallprompt` and offers Install / dismiss (dismissal
persisted in localStorage; hidden when already running standalone).

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
  `SUPABASE_URL` + / or `SUPABASE_JWT_SECRET` (auth; neither set = dev no-verify mode),
  `VAPID_PRIVATE_KEY_PEM`, `VAPID_PUBLIC_KEY`, `GEMINI_API_KEY*`,
  `GEMINI_API_KEYS`, `GROQ_API_KEY`; frontend: `VITE_API_BASE`, `MAPBOX_ACCESS_TOKEN` (root .env or host env; public pk token),
  `VITE_ORS_API_KEY`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`.
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
