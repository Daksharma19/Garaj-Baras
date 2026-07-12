# Garaj Baras — journeys.py
#
# "Journey guardian": server-side rain watch for in-progress journeys, so a
# user whose phone is locked (web pages can't run GPS in the background) still
# gets warned before driving into rain.
#
# How it works without live GPS: the frontend registers the journey once —
# route polyline samples (lat, lon, cum_km), the planned/observed speed, and a
# push subscription. The server then DEAD-RECKONS the user's position at any
# moment ("started 2:00 PM at 40 km/h → ~20 km along at 2:30"). Whenever the
# app is open it re-anchors the estimate with the real GPS progress
# (/journey/update), so the drift only grows while the screen is off.
#
# Every scheduled sweep (~10 min, same cron that drives rain alerts) each
# active journey gets the waypoints AHEAD of the estimated position checked
# with the exact same engine as /predict_waypoints (check_route_rain +
# enrich_results). Rain inside the lookahead window → push notification.
#
# Journeys auto-expire (reaching the destination estimate, or 1.5× the planned
# duration + slack) so the table cleans itself; a dead push subscription
# deactivates its journey.

import json
import threading
from datetime import datetime, timedelta, timezone
import os

import db
from alerts import _send_push
from prediction import check_route_rain
from fuzzy import enrich_results

DB_PATH = os.path.join(os.path.dirname(__file__), "alerts.db")  # shares the alerts DB file in dev

LOOKAHEAD_MINS = 45.0        # how far ahead along the route we scan for rain
NOTIFY_WINDOW_MINS = 30.0    # only notify for rain the user reaches within this
RENOTIFY_MINS = 25.0         # per-journey notification cooldown
EXPIRY_SLACK = 1.5           # journey lives for planned duration × this + 30 min

_db_lock = threading.Lock()
_db_ready = False


def _conn():
    return db.connect(DB_PATH)


def init_db():
    global _db_ready
    if _db_ready:
        return
    with _db_lock, _conn() as c:
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS journeys (
                id {db.AUTOINC_PK},
                endpoint TEXT NOT NULL,
                sub_json TEXT NOT NULL,
                route_json TEXT NOT NULL,       -- [[lat, lon, cum_km], ...]
                total_km REAL NOT NULL,
                speed_kmh REAL NOT NULL,
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                anchor_km REAL NOT NULL DEFAULT 0,   -- last REAL progress (from the app)
                anchor_at TEXT NOT NULL,             -- when that progress was true
                active INTEGER NOT NULL DEFAULT 1,
                last_notified_at TEXT
            )
        """)
    _db_ready = True


def _now():
    return datetime.now(timezone.utc)


def _mins_since(iso: str) -> float:
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 60.0
    except Exception:
        return 1e9


def start_journey(sub: dict, waypoints: list, speed_kmh: float) -> int:
    """Register a journey. `waypoints`: [{lat, lon, cum_km}], ordered.
    Returns the journey id."""
    init_db()
    endpoint = (sub or {}).get("endpoint")
    if not endpoint:
        raise ValueError("push subscription with endpoint required")
    route = [[float(w["lat"]), float(w["lon"]), float(w["cum_km"])] for w in waypoints]
    if len(route) < 2:
        raise ValueError("need at least 2 waypoints")
    total_km = route[-1][2]
    speed = max(5.0, float(speed_kmh))
    duration_mins = (total_km / speed) * 60.0
    now = _now()
    expires = now + timedelta(minutes=duration_mins * EXPIRY_SLACK + 30)
    with _db_lock, _conn() as c:
        # One active journey per device: replace any previous one
        c.execute("UPDATE journeys SET active=0 WHERE endpoint=? AND active=1", (endpoint,))
        cur = c.execute("""
            INSERT INTO journeys (endpoint, sub_json, route_json, total_km, speed_kmh,
                                  started_at, expires_at, anchor_km, anchor_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """ + (" RETURNING id" if db.IS_POSTGRES else ""),
            (endpoint, json.dumps(sub), json.dumps(route), total_km, speed,
             now.isoformat(), expires.isoformat(), 0.0, now.isoformat()))
        if db.IS_POSTGRES:
            return int(cur.fetchone()[0])
        return int(cur.lastrowid)


def update_journey(journey_id: int, progress_km: float, speed_kmh: float = None) -> bool:
    """Re-anchor the dead-reckoning with the app's real GPS progress."""
    init_db()
    with _db_lock, _conn() as c:
        if speed_kmh is not None and float(speed_kmh) > 0:
            cur = c.execute(
                "UPDATE journeys SET anchor_km=?, anchor_at=?, speed_kmh=? WHERE id=? AND active=1",
                (max(0.0, float(progress_km)), _now().isoformat(), float(speed_kmh), int(journey_id)))
        else:
            cur = c.execute(
                "UPDATE journeys SET anchor_km=?, anchor_at=? WHERE id=? AND active=1",
                (max(0.0, float(progress_km)), _now().isoformat(), int(journey_id)))
        return cur.rowcount > 0


def end_journey(journey_id: int) -> bool:
    init_db()
    with _db_lock, _conn() as c:
        cur = c.execute("UPDATE journeys SET active=0 WHERE id=?", (int(journey_id),))
        return cur.rowcount > 0


def _estimate_km(row) -> float:
    """Dead-reckoned progress: last real anchor + speed × time since."""
    _id, anchor_km, anchor_at, speed, total_km = row
    est = float(anchor_km) + float(speed) * (_mins_since(anchor_at) / 60.0)
    return min(float(total_km), est)


def _point_at_km(route, km):
    """(lat, lon) interpolated at km along the stored route samples."""
    if km <= route[0][2]:
        return route[0][0], route[0][1]
    for i in range(1, len(route)):
        if route[i][2] >= km:
            a, b = route[i - 1], route[i]
            span = b[2] - a[2] or 1e-9
            u = max(0.0, min(1.0, (km - a[2]) / span))
            return a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])
    return route[-1][0], route[-1][1]


def _load_active():
    init_db()
    with _db_lock, _conn() as c:
        return c.execute(
            "SELECT id, endpoint, sub_json, route_json, total_km, speed_kmh, "
            "started_at, expires_at, anchor_km, anchor_at, last_notified_at "
            "FROM journeys WHERE active=1").fetchall()


def estimated_positions():
    """[(lat, lon)] estimated CURRENT position per active journey — used by the
    sweep to decide which radars to refresh (same idea as alert coords)."""
    try:
        out = []
        for (jid, _ep, _sj, route_json, total_km, speed, _st, expires_at,
             anchor_km, anchor_at, _ln) in _load_active():
            if _mins_since(expires_at) > 0:  # already past expiry; process() will clean it
                continue
            route = json.loads(route_json)
            est = _estimate_km((jid, anchor_km, anchor_at, speed, total_km))
            out.append(_point_at_km(route, est))
        return out
    except Exception as e:
        print(f"journeys: estimated_positions failed: {e}")
        return []


def process_journeys(detect_radar, get_bundle):
    """Run once per sweep, after radar refreshes.
    `detect_radar(lat, lon) -> radar name`
    `get_bundle(name) -> {state, georef, lag_info} | None` (fresh cached state)
    Best-effort: never raises."""
    try:
        rows = _load_active()
        if not rows:
            return {"active": 0}

        import numpy as np
        from PIL import Image
        from optical_flow import isolate_rain

        # Cache per-radar heavy work across journeys in this sweep
        radar_work = {}
        notified = 0

        for (jid, endpoint, sub_json, route_json, total_km, speed, started_at,
             expires_at, anchor_km, anchor_at, last_notified_at) in rows:
            try:
                route = json.loads(route_json)
                est_km = _estimate_km((jid, anchor_km, anchor_at, speed, total_km))

                # Auto-expire: arrived (by estimate) or way past schedule
                if est_km >= float(total_km) - 0.05 or _mins_since(expires_at) > 0:
                    with _db_lock, _conn() as c:
                        c.execute("UPDATE journeys SET active=0 WHERE id=?", (jid,))
                    continue

                est_lat, est_lon = _point_at_km(route, est_km)
                radar = detect_radar(est_lat, est_lon)
                bundle = get_bundle(radar)
                if not bundle:
                    continue
                state, georef, lag_info = bundle["state"], bundle["georef"], bundle["lag_info"]
                latest_frame = state.get("latest_frame")
                if not latest_frame or not state.get("movement"):
                    continue

                if radar not in radar_work:
                    radar_work[radar] = {
                        "rgb": np.array(Image.open(latest_frame).convert("RGB")),
                        "mask": isolate_rain(latest_frame, clutter_mask=None),
                    }
                work = radar_work[radar]
                dx, dy = state["movement"][0], state["movement"][1]

                # Waypoints ahead of the estimated position, within the lookahead
                spd = max(5.0, float(speed))
                wps_px, wps_ll = [], []
                for lat, lon, cum in route:
                    eta = ((cum - est_km) / spd) * 60.0
                    if eta < 0 or eta > LOOKAHEAD_MINS:
                        continue
                    if not georef.is_within_radar(lat, lon):
                        continue
                    px, py = georef.latlon_to_pixel(lat, lon)
                    wps_px.append((px, py, eta))
                    wps_ll.append((lat, lon, eta))
                if not wps_px:
                    continue

                results = check_route_rain(
                    wps_px, dx, dy, latest_frame,
                    eta_minutes=LOOKAHEAD_MINS,
                    clutter_mask=None,
                    lag_mins=lag_info["lag_mins"],
                    frame_rgb=work["rgb"],
                    base_rain_mask=work["mask"],
                    patches=state.get("patches") or [],
                )
                enriched = enrich_results(
                    results, wps_ll, latest_frame, dx, dy,
                    lag_info=lag_info, frame_rgb=work["rgb"],
                    latlon_to_pixel_fn=georef.latlon_to_pixel,
                )

                first = next(
                    (e for e in enriched
                     if e.get("rain_expected") and e["eta_mins"] <= NOTIFY_WINDOW_MINS),
                    None)
                if first is None:
                    continue
                if last_notified_at and _mins_since(last_notified_at) < RENOTIFY_MINS:
                    continue

                eta = int(round(first["eta_mins"]))
                label = first.get("label") or "Rain"
                if eta <= 5:
                    title = f"⛈ {label} on your route — right ahead"
                    body = ("You're about to reach a rain stretch. "
                            "Open the app for the live countdown.")
                else:
                    title = f"🌧 {label} ahead on your journey"
                    body = (f"Radar shows {label.lower()} roughly {eta} min ahead "
                            f"on your route (estimate based on your speed).")

                alive = _send_push(sub_json, title, body)
                with _db_lock, _conn() as c:
                    if not alive:
                        c.execute("UPDATE journeys SET active=0 WHERE id=?", (jid,))
                    else:
                        c.execute("UPDATE journeys SET last_notified_at=? WHERE id=?",
                                  (_now().isoformat(), jid))
                        notified += 1
                try:
                    print(f"journeys: notified journey {jid}: {title}")
                except Exception:
                    pass
            except Exception as e:
                print(f"journeys: check failed for journey {jid}: {e}")
        return {"active": len(rows), "notified": notified}
    except Exception as e:
        print(f"journeys: process_journeys failed: {e}")
        return {"error": str(e)}


try:
    init_db()
except Exception as _e:
    print(f"journeys: init_db failed at import (will retry lazily): {_e}")
