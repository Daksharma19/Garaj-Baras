# RainRoute — Presentation Content (TATA InnoVent template order)
Copy each section into your PPT. Keep slides visual: route screenshots, radar images, big numbers.

---

## Slide 1 — Title
**RainRoute — Weather-Aware Route Intelligence for ADAS**
*Live rain, fog & destination safety on the car screen — powered by IMD radar + nowcast fusion, at the edge.*
Team name | College | TATA InnoVent 2026 | Category: Edge AI for ADAS & Autonomous Systems

---

## Slide 2 — Problem Statement (+ market size & potential)
**Drivers get navigation, but zero weather intelligence along their route.**
- **15,115 deaths in 2024** in fog-related road accidents alone (MoRTH).
- **1.72 lakh road deaths/year — 20 every hour**; wet roads double stopping distances.
- **428 killed, 70,000+ tourists evacuated** in the 2023 Himachal monsoon — most drove into danger with no warning.

The data to prevent this already exists with IMD — it never reaches the driver's screen. Weather apps answer "is it raining *here, now*?" Nobody answers: *"Will it rain/fog where I'll be 40 minutes from now — and is my destination safe?"*

**Market:** India's connected-car market is among the fastest growing globally; 100% of new connected vehicles (Tata's own fleet included) already ship the hardware this needs — screen, GPS, connectivity. Extensible to fleets/logistics, two-wheelers, public transport.

---

## Slide 3 — Objective & Approach
**Objective:** Put a live, colour-coded "weather along my route" verdict on the car's screen, and feed the same signal to ADAS.

**Approach — route–waypoint–time logic (our core innovation):**
1. Route is split into waypoints (every 5–10 km).
2. Vehicle's current speed + traffic → **ETA at each waypoint**.
3. Our nowcast engine predicts rain/fog **at that place, at that time** by fusing 2–3 systems: IMD Doppler radar (rain-cell motion tracking), IMD nowcast APIs (cross-validation), visibility/humidity models (fog).
4. Screen shows the route coloured green / yellow / red / fog-hatched: *"Heavy rain near Ambala in ~40 min."*

---

## Slide 4 — Solution Overview (can be 2 slides)
**A. Live Rain Route (built & working):** radar frames → rain cells segmented → motion vectors tracked → patches projected +15/+30/+60 min → intersected with waypoint ETAs → route verdict on screen.

**B. Fog prediction along route:** temperature–dew-point spread + humidity + terrain → fog-risk flags on route stretches (winter highways, hill sections).

**C. Destination Safety Briefing (the hills use-case):** one-glance card — past 48-h rainfall at destination, active IMD/govt alerts, live route rain. *"Nainital: 190 mm rain in 2 days, IMD orange alert — consider delaying."*

**D. AI Route Assistant:** voice/chat copilot with tool-calling into our live engine — *"Leaving for Mussoorie at 4 pm with family, is it safe?"* → plain-language answer, safer departure times/routes.

**E. ADAS integration:** predictions exposed as a forward-looking signal — pre-adaptive speed advisories, following distance, sensor-degradation readiness *before* entering rain/fog.

---

## Slide 5 — Challenges Faced (and how we overcame them)
- **Radar nowcast accuracy:** naive extrapolation over-predicted rain; we anchored intensity decay to actual pixel-level dBZ values instead of cell-centroid averages — projections now track real decay.
- **Stale/refreshing radar data:** IMD imagery updates irregularly; we built a caching + background-refresh pipeline so users always get the freshest frame without blocking, and fixed race conditions between refresh and serving.
- **Rain cells that split/merge:** tracked via intensity-threshold segmentation + frame-to-frame correlation rather than assuming rigid cells.
- **Making it readable at a glance:** iterated from raw radar overlays to a simple colour-coded route verdict — drivers need answers, not meteorology.

---

## Slide 6 — Technical Implementation
- **Nowcast engine:** Python, OpenCV, NumPy — radar frame segmentation, optical-flow/cross-correlation cell tracking, advection-based extrapolation with dBZ-anchored decay.
- **Data fusion:** IMD Doppler radar mosaics + IMD nowcast/district-warning APIs + humidity/visibility feeds.
- **Backend:** Python (FastAPI), geospatial matching of (waypoint, ETA) → projected weather grid; processed once per radar refresh, serves any number of vehicles.
- **Frontend/HMI:** React dashboard (working prototype) → portable to Android Automotive IVI.
- **Edge layer (roadmap):** lightweight in-vehicle client fusing cloud predictions with CAN speed, GPS, rain sensor + on-device camera visibility model; route predictions pre-fetched so it works through connectivity drops.
- **AI assistant (roadmap):** LLM with tool-calling into our engine — it queries live data, never guesses.

---

## Slide 7 — Results & Achievements
- **Working web POC:** live IMD radar ingestion → +15 to +60 min rain projections → route-waypoint colouring, end to end. (Demo video in this folder.)
- Nowcast decay anchored to real radar intensities after validation against successive actual frames.
- Robust serving pipeline (caching, background refresh) — production-pattern engineering, not a script.
- Success metric defined for pilots: **>85% of heavy-rain/fog encounters warned ≥15 min in advance**.

---

## Slide 8 — Demonstration
(Embed/link the recorded video: app loads → radar overlay → set route → route colours by projected weather at each waypoint's ETA → time-slider +15/+30/+60 min.)

---

## Slide 9 — Future Enhancements
- Deep-learning nowcasting (ConvLSTM/U-Net) trained on Indian radar archives for longer horizons.
- On-device fog/visibility inference from front camera (true edge AI), OTA-deployable.
- ADAS signal integration: speed advisory, AEB sensitivity profiles keyed to predicted conditions.
- Landslide-risk layer: 48-h rainfall + slope data for hill corridors.
- Fleet/logistics dashboard; two-wheeler alerts via cluster/companion app.

---

## Slide 10 — Project Plan (functional prototype roadmap)
- **Phase 1 (done):** Web POC — radar nowcast engine + route-waypoint weather colouring.
- **Phase 2 (months 1–2):** Fog-risk module + Destination Safety Briefing (IMD warnings + 48-h rainfall).
- **Phase 3 (months 3–4):** AI Route Assistant (LLM + tool-calling); accuracy validation vs. actual radar outcomes.
- **Phase 4 (months 5–6):** Android Automotive edge client; offline pre-fetch; demo ADAS signal (speed advisory) on a test bench/simulator.
- **Milestone:** in-vehicle pilot with success metric ≥85% advance-warning rate.

---

## Drive folder checklist (rules 4.2–4.3)
- [ ] Folder named exactly = team name
- [ ] This presentation (PPT/PDF, template section order above)
- [ ] POC demo video (2–3 min screen recording) + 4–6 screenshots
- [ ] College ID scans — ALL team members
- [ ] Share → Anyone with the link (test in incognito!)
- [ ] Form closes **5 July 2026** and cannot be saved mid-way — have all answers ready before starting.
