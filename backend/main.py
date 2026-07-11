from fastapi import FastAPI, HTTPException, Depends  # type: ignore
from fastapi.middleware.cors import CORSMiddleware  # type: ignore
from fastapi.responses import FileResponse, Response, StreamingResponse  # type: ignore
from fastapi.staticfiles import StaticFiles  # type: ignore
from pydantic import BaseModel  # type: ignore
from typing import List
import gc
import os
import time
import threading
import traceback

import numpy as np  # type: ignore
from PIL import Image  # type: ignore

from radar import (
    get_recent_frames,
    get_all_frames,
    get_radar_lag_mins,
    refresh_frames_if_stale,
    GIF_SAVE_PATH,
    RADAR_TTL_SEC,
)  # type: ignore
from optical_flow import get_movement_vector, build_clutter_mask, isolate_rain  # type: ignore
from prediction import (generate_waypoints, check_route_rain,   # type: ignore
                        haversine_km)
from georef import latlon_to_pixel, is_within_radar  # type: ignore
from fuzzy import enrich_results  # type: ignore
from decay import compute_decay_tracks, get_decay_status_at_pixel  # type: ignore
import verification  # type: ignore
import alerts  # type: ignore
import accounts  # type: ignore
from auth import get_current_user, get_optional_user  # type: ignore
from patches import (  # type: ignore
    compute_patch_motion,
    score_patches_for_route,
    build_ncr_roi_mask,
    build_roi_mask,
)
import georef_lucknow  # type: ignore
import georef as _georef_delhi  # type: ignore
import georef_patna  # type: ignore
import georef_bhopal  # type: ignore
import radar_lucknow   # type: ignore
import radar_patna  # type: ignore
import radar_bhopal  # type: ignore


def _detect_radar(lat: float, lon: float) -> str:
    """Pick the radar whose coverage contains the point; break ties by proximity."""
    in_delhi  = _georef_delhi.is_within_radar(lat, lon)
    in_lck    = georef_lucknow.is_within_radar(lat, lon)
    in_patna  = georef_patna.is_within_radar(lat, lon)
    # Re-enabled: bbox-compressed patch masks + LRU state eviction keep peak
    # RSS flat regardless of radar count (was OOM on 512MB with full masks).
    in_bhopal = georef_bhopal.is_within_radar(lat, lon)

    candidates = []
    if in_delhi:  candidates.append(('delhi',   haversine_km(lat, lon, 28.5562, 77.1000)))
    if in_lck:    candidates.append(('lucknow', haversine_km(lat, lon, 26.8467, 80.9462)))
    if in_patna:  candidates.append(('patna',   haversine_km(lat, lon, 25.5913, 85.0956)))
    if in_bhopal: candidates.append(('bhopal',  haversine_km(lat, lon, 23.2875, 77.3374)))

    if not candidates:
        return 'delhi'   # fallback
    return min(candidates, key=lambda x: x[1])[0]
from datetime import timezone as _timezone

from datetime import datetime as _dt, timedelta as _td

# India Standard Time (UTC+5:30)
IST = _timezone(_td(hours=5, minutes=30))

app = FastAPI(
    title="Garaj Baras API",
    description="Rain prediction for Delhi NCR routes",
    version="1.0.0"
)



# Serve extracted radar PNGs (and allow clients to fetch them)
try:
    from radar import FRAMES_FOLDER  # type: ignore

    if os.path.isdir(FRAMES_FOLDER):
        app.mount("/radar/frames", StaticFiles(directory=FRAMES_FOLDER), name="radar_frames")
except Exception:
    # Best-effort: API still works without static mounting (e.g. missing folder on first boot)
    pass

# Serve Lucknow radar frame PNGs — create folder now so mount never fails
try:
    os.makedirs(radar_lucknow.FRAMES_FOLDER, exist_ok=True)
    app.mount("/radar/frames_lucknow", StaticFiles(directory=radar_lucknow.FRAMES_FOLDER), name="radar_frames_lucknow")
except Exception:
    pass

# Serve Patna radar frame PNGs
try:
    os.makedirs(radar_patna.FRAMES_FOLDER, exist_ok=True)
    app.mount("/radar/frames_patna", StaticFiles(directory=radar_patna.FRAMES_FOLDER), name="radar_frames_patna")
except Exception:
    pass

# Serve Bhopal radar frame PNGs
try:
    os.makedirs(radar_bhopal.FRAMES_FOLDER, exist_ok=True)
    app.mount("/radar/frames_bhopal", StaticFiles(directory=radar_bhopal.FRAMES_FOLDER), name="radar_frames_bhopal")
except Exception:
    pass

# Allow all origins for now (frontend will call this)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request/Response Models

class RouteRequest(BaseModel):
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    spacing_km: float = 2.0
    # OSRM routes: waypoint every N minutes of driving (ignored for straight-line fallback)
    eta_spacing_minutes: float = 2.0

class WaypointResult(BaseModel):
    lat: float
    lon: float
    eta_mins: float
    effective_eta: float
    rain_expected: bool
    confidence: str
    label: str
    color: str
    dbz: float
    message: str
    in_radar_bounds: bool

class PredictResponse(BaseModel):
    route_distance_km: float
    total_waypoints: int
    rain_waypoints: int
    clear_waypoints: int
    first_rain_eta: float | None
    first_rain_label: str | None
    rain_direction_from: str
    rain_direction_to: str
    rain_speed_kmh: float
    radar_lag_mins: float
    radar_freshness: str
    radar_message: str
    waypoints: List[dict]


class WaypointInput(BaseModel):
    lat: float
    lon: float
    eta_mins: float


class PredictWaypointsRequest(BaseModel):
    waypoints: List[WaypointInput]


def _patch_to_public(p: dict) -> dict:
    """Strip numpy masks / cast numpy types to a JSON-safe patch summary."""
    cx, cy = p.get("centroid_px", (None, None))
    latlon = p.get("centroid_latlon", (None, None))
    ipx = p.get("intercept_px")
    eta = p.get("intercept_eta")
    return {
        "id": int(p.get("id", -1)),
        "area_px": int(p.get("area_px", 0)),
        "centroid_px": [float(cx), float(cy)] if cx is not None else None,
        "centroid_latlon": [float(latlon[0]), float(latlon[1])] if latlon and latlon[0] is not None else None,
        "speed_kmh": round(float(p.get("speed_kmh", 0.0)), 1),
        "direction_from": p.get("direction_from", "Unknown"),
        "direction_to": p.get("direction_to", "Unknown"),
        "max_dbz": int(p.get("max_dbz", 0)),
        "in_ncr": bool(p.get("in_ncr", False)),
        "will_hit_route": bool(p.get("will_hit_route", False)),
        "intercept_eta": (None if eta is None else round(float(eta), 1)),
        "intercept_px": ([float(ipx[0]), float(ipx[1])] if ipx else None),
        "relevance": float(p.get("relevance", 0.0)),
    }


# ── Radar state management ────────────────────────────────────────────────────
#
# Each radar has:
#   _*_cache     : dict served to all requests — atomically updated at end of refresh
#   _*_state_lock: guards cache.update() writes
#   _*_bg_lock   : non-blocking acquire prevents two simultaneous refreshes
#   _*_ready     : Event set once the first data is available (cold-start gate)
#
# Request flow:
#   Cold start (no data yet)  → block on _*_ready.wait() while bg thread loads
#   Cache fresh               → return immediately (sub-ms)
#   Cache stale               → return current data now, kick off bg refresh
#
# Root bug fixed: old _cache_is_fresh() checked `clutter_mask is None` but
# clutter_mask is intentionally None (disabled). This made the cache appear
# stale on every single request, triggering a full reload each time.

radar_cache   = {"clutter_mask": None, "last_loaded": None}
lucknow_cache = {"clutter_mask": None, "last_loaded": None}
patna_cache   = {"clutter_mask": None, "last_loaded": None}
bhopal_cache  = {"clutter_mask": None, "last_loaded": None}

_radar_state_lock   = threading.Lock()
_lucknow_state_lock = threading.Lock()
_patna_state_lock   = threading.Lock()
_bhopal_state_lock  = threading.Lock()

_delhi_bg_lock   = threading.Lock()
_lucknow_bg_lock = threading.Lock()
_patna_bg_lock   = threading.Lock()
_bhopal_bg_lock  = threading.Lock()

_delhi_ready   = threading.Event()
_lucknow_ready = threading.Event()
_patna_ready   = threading.Event()
_bhopal_ready  = threading.Event()

RADAR_CACHE_TTL_SEC = RADAR_TTL_SEC


def _is_fresh(cache: dict, ttl_sec: float) -> bool:
    """True iff cache has been populated within ttl_sec and has the required fields."""
    last = cache.get("last_loaded")
    if not last:
        return False
    if (time.time() - float(last)) >= float(ttl_sec):
        return False
    return bool(cache.get("latest_frame") and cache.get("movement") and cache.get("frame_data"))


DEFAULT_RADAR_LAG_MINS = 25.0

# ── LRU radar-state eviction ──────────────────────────────────────────────────
# Keep at most this many radar states fully loaded. Evicted radars rebuild
# from the on-disk GIF on their next request (a few seconds of CPU), so peak
# RSS stays flat no matter how many radars the app grows to.
MAX_RADARS_IN_MEMORY = 2

_HEAVY_STATE_KEYS = (
    "patches", "decay_tracks", "roi_mask", "frame_data", "recent_frame_data",
    "movement", "latest_frame", "clutter_mask", "latest_ts", "lag_info",
)


def _touch_radar_and_evict(name: str) -> None:
    """Mark `name` most-recently-used; drop heavy state of radars beyond the cap."""
    entries = {
        "delhi":   (radar_cache,   _radar_state_lock,   _delhi_ready),
        "lucknow": (lucknow_cache, _lucknow_state_lock, _lucknow_ready),
        "patna":   (patna_cache,   _patna_state_lock,   _patna_ready),
        "bhopal":  (bhopal_cache,  _bhopal_state_lock,  _bhopal_ready),
    }
    cache, lock, _evt = entries.get(name, entries["delhi"])
    with lock:
        cache["last_used"] = time.time()

    loaded = [(n, c, l, e) for n, (c, l, e) in entries.items() if c.get("last_loaded")]
    loaded.sort(key=lambda t: t[1].get("last_used") or t[1].get("last_loaded") or 0,
                reverse=True)
    for n, c, l, e in loaded[MAX_RADARS_IN_MEMORY:]:
        with l:
            for k in _HEAVY_STATE_KEYS:
                c[k] = None
            c["last_loaded"] = None
        # Back to cold-start semantics: the next request for this radar must
        # BLOCK on a rebuild instead of being served the gutted state via the
        # stale-cache fast path.
        e.clear()
        gc.collect()
        print(f"{n} radar: state evicted (LRU, cap={MAX_RADARS_IN_MEMORY})")

def _fresh_lag_info(state: dict) -> dict:
    """
    Radar lag recomputed at REQUEST time.

    The lag_info stored in the radar cache is computed once per refresh and
    then frozen for the cache TTL, so predictions under-shift rain as minutes
    pass. Recompute from the OCR'd frame timestamp against now; if OCR never
    produced a timestamp, use a conservative 25-min default.
    """
    ts = state.get("latest_ts")
    if ts is not None:
        # get_radar_lag_mins computes against now() and falls through to its
        # own 25-min default when the timestamp yields an impossible lag.
        return get_radar_lag_mins(ts)
    cached = state.get("lag_info") or {}
    if cached.get("lag_mins") is not None:
        return cached
    return {
        "lag_mins": DEFAULT_RADAR_LAG_MINS,
        "freshness": "stale",
        "method": "fallback_estimate",
        "message": "Radar ~25 mins old (estimate)",
        "radar_time": "Unknown",
    }


# ── Delhi ─────────────────────────────────────────────────────────────────────

def _do_delhi_refresh(ttl_sec: float, force: bool = False) -> None:
    """Full Delhi radar refresh. Non-blocking acquire — silently no-ops if already running."""
    if not _delhi_bg_lock.acquire(blocking=False):
        return
    try:
        print("Delhi radar: refresh started")
        now = time.time()
        try:
            gif_fresh = os.path.exists(GIF_SAVE_PATH) and (now - os.path.getmtime(GIF_SAVE_PATH) < ttl_sec)
        except Exception:
            gif_fresh = False

        frame_data, did_refresh = refresh_frames_if_stale(ttl_sec=ttl_sec, force=force, clear_pngs=False)
        if did_refresh:
            all_frame_data = frame_data
        else:
            from radar import extract_frames, FRAMES_FOLDER as _FF  # type: ignore
            all_frame_data = extract_frames(GIF_SAVE_PATH, _FF) if gif_fresh else get_all_frames()

        # IMD's single "current radar" image often updates before the
        # animation GIF (observed 60 min newer). Append it as the newest
        # frame when its OCR timestamp is strictly newer — never duplicated.
        try:
            from radar import augment_with_current_image, FRAMES_FOLDER as _FF2  # type: ignore
            all_frame_data = augment_with_current_image(all_frame_data, _FF2)
        except Exception as _ae:
            print(f"Delhi current-image augmentation failed: {_ae}")

        recent_frame_data = all_frame_data[-6:] if len(all_frame_data) > 6 else all_frame_data
        verification.verify_pending("delhi", all_frame_data, isolate_rain, clutter_mask=None)
        del all_frame_data
        clutter_mask = None
        gc.collect()

        dx, dy, dir_from, dir_to, speed = get_movement_vector(recent_frame_data, clutter_mask=clutter_mask)
        latest_frame = recent_frame_data[-1][0] if recent_frame_data else None
        latest_ts    = recent_frame_data[-1][1] if recent_frame_data else None
        lag_info     = get_radar_lag_mins(latest_ts)

        patches_motion, roi_mask = [], None
        try:
            roi_mask = build_ncr_roi_mask(radius_km=150.0)
            motion_frames = [p for p, _ in recent_frame_data[-4:]]
            ts_prev = recent_frame_data[-2][1] if len(recent_frame_data) >= 2 else None
            ts_last = recent_frame_data[-1][1] if recent_frame_data else None
            gap = 10.0
            if ts_prev and ts_last:
                gap = max(1.0, (ts_last - ts_prev).total_seconds() / 60.0)
            if len(motion_frames) >= 2:
                patches_motion = compute_patch_motion(
                    motion_frames, gap_mins=gap, clutter_mask=clutter_mask,
                    roi_mask=roi_mask, min_area_px=4,
                )
                print(f"  Delhi per-patch: {len(patches_motion)} patch(es) (gap={gap:.0f}m)")
        except Exception as _pe:
            print(f"  Delhi per-patch failed: {_pe}")

        decay_tracks = []
        try:
            decay_tracks = compute_decay_tracks(recent_frame_data, dx, dy, clutter_mask=clutter_mask)
            print(f"  Delhi decay tracks: {len(decay_tracks)} patch(es)")
        except Exception as _de:
            print(f"  Delhi decay tracking failed: {_de}")

        new_state = {
            "frame_data": recent_frame_data,
            "recent_frame_data": recent_frame_data,
            "clutter_mask": clutter_mask,
            "movement": (dx, dy, dir_from, dir_to, speed),
            "latest_frame": latest_frame,
            "latest_ts": latest_ts,
            "lag_info": lag_info,
            "patches": patches_motion,
            "roi_mask": roi_mask,
            "decay_tracks": decay_tracks,
            "last_loaded": time.time(),
            "gif_mtime": os.path.getmtime(GIF_SAVE_PATH) if os.path.exists(GIF_SAVE_PATH) else None,
        }
        with _radar_state_lock:
            radar_cache.update(new_state)
        print("Delhi radar: refresh complete")
        try:
            alerts.process_alerts("delhi", new_state, _georef_delhi.is_within_radar, _georef_delhi.latlon_to_pixel)
        except Exception as _al:
            print(f"alerts hook failed: {_al}")
    except Exception as e:
        print(f"Delhi radar: refresh failed: {e}\n{traceback.format_exc()}")
    finally:
        _delhi_ready.set()   # always unblock cold-start waiters
        _delhi_bg_lock.release()


def _load_radar_state(ttl_sec: float = RADAR_CACHE_TTL_SEC, *, force: bool = False) -> dict:
    # Cold start: no data yet — block until background thread delivers first load
    if not _delhi_ready.is_set():
        threading.Thread(target=_do_delhi_refresh, args=(ttl_sec, True), daemon=True).start()
        _delhi_ready.wait(timeout=60)
        return radar_cache

    # Fresh: return instantly — no I/O, no computation
    if not force and _is_fresh(radar_cache, ttl_sec):
        return radar_cache

    # Stale or forced: return current data immediately, refresh silently in background
    threading.Thread(target=_do_delhi_refresh, args=(ttl_sec, force), daemon=True).start()
    return radar_cache


# ── Lucknow ───────────────────────────────────────────────────────────────────

def _do_lucknow_refresh(ttl_sec: float, force: bool = False) -> None:
    if not _lucknow_bg_lock.acquire(blocking=False):
        return
    try:
        print("Lucknow radar: refresh started")
        lk_gif = radar_lucknow.GIF_SAVE_PATH
        now = time.time()
        try:
            gif_fresh = os.path.exists(lk_gif) and (now - os.path.getmtime(lk_gif) < ttl_sec)
        except Exception:
            gif_fresh = False

        frame_data, did_refresh = radar_lucknow.refresh_frames_if_stale(ttl_sec=ttl_sec, force=force, clear_pngs=False)
        if did_refresh:
            all_frame_data = frame_data
        else:
            all_frame_data = radar_lucknow.extract_frames(lk_gif, radar_lucknow.FRAMES_FOLDER) if gif_fresh else radar_lucknow.get_all_frames()

        recent_frame_data = all_frame_data[-6:] if len(all_frame_data) > 6 else all_frame_data
        verification.verify_pending("lucknow", all_frame_data, isolate_rain, clutter_mask=None)
        del all_frame_data
        clutter_mask = None
        gc.collect()

        dx, dy, dir_from, dir_to, speed = get_movement_vector(recent_frame_data, clutter_mask=clutter_mask)
        latest_frame = recent_frame_data[-1][0] if recent_frame_data else None
        latest_ts    = recent_frame_data[-1][1] if recent_frame_data else None
        lag_info     = radar_lucknow.get_radar_lag_mins(latest_ts)

        patches_motion, roi_mask = [], None
        try:
            roi_mask = build_roi_mask(
                georef_lucknow.latlon_to_pixel, georef_lucknow.IMAGE_WIDTH,
                georef_lucknow.IMAGE_HEIGHT, georef_lucknow.CENTER_LAT,
                georef_lucknow.CENTER_LON, radius_km=150.0,
            )
            motion_frames = [p for p, _ in recent_frame_data[-4:]]
            ts_prev = recent_frame_data[-2][1] if len(recent_frame_data) >= 2 else None
            ts_last = recent_frame_data[-1][1] if recent_frame_data else None
            gap = 10.0
            if ts_prev and ts_last:
                gap = max(1.0, (ts_last - ts_prev).total_seconds() / 60.0)
            if len(motion_frames) >= 2:
                patches_motion = compute_patch_motion(
                    motion_frames, gap_mins=gap, clutter_mask=clutter_mask,
                    roi_mask=roi_mask, min_area_px=4,
                    pixel_to_latlon_fn=georef_lucknow.pixel_to_latlon,
                )
                print(f"  Lucknow per-patch: {len(patches_motion)} patch(es) (gap={gap:.0f}m)")
        except Exception as _pe:
            print(f"  Lucknow per-patch failed: {_pe}")

        decay_tracks = []
        try:
            decay_tracks = compute_decay_tracks(recent_frame_data, dx, dy, clutter_mask=clutter_mask)
            print(f"  Lucknow decay tracks: {len(decay_tracks)} patch(es)")
        except Exception as _de:
            print(f"  Lucknow decay tracking failed: {_de}")

        new_state = {
            "frame_data": recent_frame_data,
            "recent_frame_data": recent_frame_data,
            "clutter_mask": clutter_mask,
            "movement": (dx, dy, dir_from, dir_to, speed),
            "latest_frame": latest_frame,
            "latest_ts": latest_ts,
            "lag_info": lag_info,
            "patches": patches_motion,
            "roi_mask": roi_mask,
            "decay_tracks": decay_tracks,
            "last_loaded": time.time(),
            "gif_mtime": os.path.getmtime(lk_gif) if os.path.exists(lk_gif) else None,
        }
        with _lucknow_state_lock:
            lucknow_cache.update(new_state)
        print("Lucknow radar: refresh complete")
        try:
            alerts.process_alerts("lucknow", new_state, georef_lucknow.is_within_radar, georef_lucknow.latlon_to_pixel)
        except Exception as _al:
            print(f"alerts hook failed: {_al}")
    except Exception as e:
        print(f"Lucknow radar: refresh failed: {e}\n{traceback.format_exc()}")
    finally:
        _lucknow_ready.set()
        _lucknow_bg_lock.release()


def _load_lucknow_radar_state(ttl_sec: float = RADAR_CACHE_TTL_SEC, *, force: bool = False) -> dict:
    if not _lucknow_ready.is_set():
        threading.Thread(target=_do_lucknow_refresh, args=(ttl_sec, True), daemon=True).start()
        _lucknow_ready.wait(timeout=60)
        return lucknow_cache
    if not force and _is_fresh(lucknow_cache, ttl_sec):
        return lucknow_cache
    threading.Thread(target=_do_lucknow_refresh, args=(ttl_sec, force), daemon=True).start()
    return lucknow_cache


# ── Patna ─────────────────────────────────────────────────────────────────────

def _do_patna_refresh(ttl_sec: float, force: bool = False) -> None:
    if not _patna_bg_lock.acquire(blocking=False):
        return
    try:
        print("Patna radar: refresh started")
        ptn_gif = radar_patna.GIF_SAVE_PATH
        now = time.time()
        try:
            gif_fresh = os.path.exists(ptn_gif) and (now - os.path.getmtime(ptn_gif) < ttl_sec)
        except Exception:
            gif_fresh = False

        frame_data, did_refresh = radar_patna.refresh_frames_if_stale(ttl_sec=ttl_sec, force=force, clear_pngs=False)
        if did_refresh:
            all_frame_data = frame_data
        else:
            all_frame_data = radar_patna.extract_frames(ptn_gif, radar_patna.FRAMES_FOLDER) if gif_fresh else radar_patna.get_all_frames()

        recent_frame_data = all_frame_data[-6:] if len(all_frame_data) > 6 else all_frame_data
        verification.verify_pending("patna", all_frame_data, isolate_rain, clutter_mask=None)
        del all_frame_data
        clutter_mask = None
        gc.collect()

        dx, dy, dir_from, dir_to, speed = get_movement_vector(recent_frame_data, clutter_mask=clutter_mask)
        latest_frame = recent_frame_data[-1][0] if recent_frame_data else None
        latest_ts    = recent_frame_data[-1][1] if recent_frame_data else None
        lag_info     = radar_patna.get_radar_lag_mins(latest_ts)

        patches_motion, roi_mask = [], None
        try:
            roi_mask = build_roi_mask(
                georef_patna.latlon_to_pixel, georef_patna.IMAGE_WIDTH,
                georef_patna.IMAGE_HEIGHT, georef_patna.CENTER_LAT,
                georef_patna.CENTER_LON, radius_km=150.0,
            )
            motion_frames = [p for p, _ in recent_frame_data[-4:]]
            ts_prev = recent_frame_data[-2][1] if len(recent_frame_data) >= 2 else None
            ts_last = recent_frame_data[-1][1] if recent_frame_data else None
            gap = 10.0
            if ts_prev and ts_last:
                gap = max(1.0, (ts_last - ts_prev).total_seconds() / 60.0)
            if len(motion_frames) >= 2:
                patches_motion = compute_patch_motion(
                    motion_frames, gap_mins=gap, clutter_mask=clutter_mask,
                    roi_mask=roi_mask, min_area_px=4,
                    pixel_to_latlon_fn=georef_patna.pixel_to_latlon,
                )
                print(f"  Patna per-patch: {len(patches_motion)} patch(es) (gap={gap:.0f}m)")
        except Exception as _pe:
            print(f"  Patna per-patch failed: {_pe}")

        decay_tracks = []
        try:
            decay_tracks = compute_decay_tracks(recent_frame_data, dx, dy, clutter_mask=clutter_mask)
            print(f"  Patna decay tracks: {len(decay_tracks)} patch(es)")
        except Exception as _de:
            print(f"  Patna decay tracking failed: {_de}")

        new_state = {
            "frame_data": recent_frame_data,
            "recent_frame_data": recent_frame_data,
            "clutter_mask": clutter_mask,
            "movement": (dx, dy, dir_from, dir_to, speed),
            "latest_frame": latest_frame,
            "latest_ts": latest_ts,
            "lag_info": lag_info,
            "patches": patches_motion,
            "roi_mask": roi_mask,
            "decay_tracks": decay_tracks,
            "last_loaded": time.time(),
            "gif_mtime": os.path.getmtime(ptn_gif) if os.path.exists(ptn_gif) else None,
        }
        with _patna_state_lock:
            patna_cache.update(new_state)
        print("Patna radar: refresh complete")
        try:
            alerts.process_alerts("patna", new_state, georef_patna.is_within_radar, georef_patna.latlon_to_pixel)
        except Exception as _al:
            print(f"alerts hook failed: {_al}")
    except Exception as e:
        print(f"Patna radar: refresh failed: {e}\n{traceback.format_exc()}")
    finally:
        _patna_ready.set()
        _patna_bg_lock.release()


def _load_patna_radar_state(ttl_sec: float = RADAR_CACHE_TTL_SEC, *, force: bool = False) -> dict:
    if not _patna_ready.is_set():
        threading.Thread(target=_do_patna_refresh, args=(ttl_sec, True), daemon=True).start()
        _patna_ready.wait(timeout=60)
        return patna_cache
    if not force and _is_fresh(patna_cache, ttl_sec):
        return patna_cache
    threading.Thread(target=_do_patna_refresh, args=(ttl_sec, force), daemon=True).start()
    return patna_cache


# ── Bhopal ────────────────────────────────────────────────────────────────────

def _do_bhopal_refresh(ttl_sec: float, force: bool = False) -> None:
    if not _bhopal_bg_lock.acquire(blocking=False):
        return
    try:
        print("Bhopal radar: refresh started")
        bhp_gif = radar_bhopal.GIF_SAVE_PATH
        now = time.time()
        try:
            gif_fresh = os.path.exists(bhp_gif) and (now - os.path.getmtime(bhp_gif) < ttl_sec)
        except Exception:
            gif_fresh = False

        frame_data, did_refresh = radar_bhopal.refresh_frames_if_stale(ttl_sec=ttl_sec, force=force, clear_pngs=False)
        if did_refresh:
            all_frame_data = frame_data
        else:
            all_frame_data = radar_bhopal.extract_frames(bhp_gif, radar_bhopal.FRAMES_FOLDER) if gif_fresh else radar_bhopal.get_all_frames()

        recent_frame_data = all_frame_data[-6:] if len(all_frame_data) > 6 else all_frame_data
        verification.verify_pending("bhopal", all_frame_data, isolate_rain, clutter_mask=None)
        del all_frame_data
        clutter_mask = None
        gc.collect()

        dx, dy, dir_from, dir_to, speed = get_movement_vector(recent_frame_data, clutter_mask=clutter_mask)
        latest_frame = recent_frame_data[-1][0] if recent_frame_data else None
        latest_ts    = recent_frame_data[-1][1] if recent_frame_data else None
        lag_info     = radar_bhopal.get_radar_lag_mins(latest_ts)

        patches_motion, roi_mask = [], None
        try:
            roi_mask = build_roi_mask(
                georef_bhopal.latlon_to_pixel, georef_bhopal.IMAGE_WIDTH,
                georef_bhopal.IMAGE_HEIGHT, georef_bhopal.CENTER_LAT,
                georef_bhopal.CENTER_LON, radius_km=150.0,
            )
            motion_frames = [p for p, _ in recent_frame_data[-4:]]
            ts_prev = recent_frame_data[-2][1] if len(recent_frame_data) >= 2 else None
            ts_last = recent_frame_data[-1][1] if recent_frame_data else None
            gap = 10.0
            if ts_prev and ts_last:
                gap = max(1.0, (ts_last - ts_prev).total_seconds() / 60.0)
            if len(motion_frames) >= 2:
                patches_motion = compute_patch_motion(
                    motion_frames, gap_mins=gap, clutter_mask=clutter_mask,
                    roi_mask=roi_mask, min_area_px=4,
                    pixel_to_latlon_fn=georef_bhopal.pixel_to_latlon,
                )
                print(f"  Bhopal per-patch: {len(patches_motion)} patch(es) (gap={gap:.0f}m)")
        except Exception as _pe:
            print(f"  Bhopal per-patch failed: {_pe}")

        decay_tracks = []
        try:
            decay_tracks = compute_decay_tracks(recent_frame_data, dx, dy, clutter_mask=clutter_mask)
            print(f"  Bhopal decay tracks: {len(decay_tracks)} patch(es)")
        except Exception as _de:
            print(f"  Bhopal decay tracking failed: {_de}")

        new_state = {
            "frame_data": recent_frame_data,
            "recent_frame_data": recent_frame_data,
            "clutter_mask": clutter_mask,
            "movement": (dx, dy, dir_from, dir_to, speed),
            "latest_frame": latest_frame,
            "latest_ts": latest_ts,
            "lag_info": lag_info,
            "patches": patches_motion,
            "roi_mask": roi_mask,
            "decay_tracks": decay_tracks,
            "last_loaded": time.time(),
            "gif_mtime": os.path.getmtime(bhp_gif) if os.path.exists(bhp_gif) else None,
        }
        with _bhopal_state_lock:
            bhopal_cache.update(new_state)
        print("Bhopal radar: refresh complete")
        try:
            alerts.process_alerts("bhopal", new_state, georef_bhopal.is_within_radar, georef_bhopal.latlon_to_pixel)
        except Exception as _al:
            print(f"alerts hook failed: {_al}")
    except Exception as e:
        print(f"Bhopal radar: refresh failed: {e}\n{traceback.format_exc()}")
    finally:
        _bhopal_ready.set()
        _bhopal_bg_lock.release()


def _load_bhopal_radar_state(ttl_sec: float = RADAR_CACHE_TTL_SEC, *, force: bool = False) -> dict:
    if not _bhopal_ready.is_set():
        threading.Thread(target=_do_bhopal_refresh, args=(ttl_sec, True), daemon=True).start()
        _bhopal_ready.wait(timeout=60)
        return bhopal_cache
    if not force and _is_fresh(bhopal_cache, ttl_sec):
        return bhopal_cache
    threading.Thread(target=_do_bhopal_refresh, args=(ttl_sec, force), daemon=True).start()
    return bhopal_cache


# ── Radar registry + alert plumbing ───────────────────────────────────────────
# One table mapping a radar name to everything the alert paths need: its
# blocking refresh fn (which also runs process_alerts on completion), its cache,
# its cold-start event, and its georef module.

_RADAR_REGISTRY = {
    "delhi":   {"refresh": _do_delhi_refresh,   "cache": radar_cache,   "ready": _delhi_ready,   "georef": _georef_delhi},
    "lucknow": {"refresh": _do_lucknow_refresh, "cache": lucknow_cache, "ready": _lucknow_ready, "georef": georef_lucknow},
    "patna":   {"refresh": _do_patna_refresh,   "cache": patna_cache,   "ready": _patna_ready,   "georef": georef_patna},
    "bhopal":  {"refresh": _do_bhopal_refresh,  "cache": bhopal_cache,  "ready": _bhopal_ready,  "georef": georef_bhopal},
}


def _ensure_radar_fresh_blocking(name: str) -> dict:
    """Return a usable state for `name`, refreshing synchronously if stale.
    The refresh fn no-ops if another refresh already holds the bg lock, so this
    is best-effort: it never double-refreshes and never raises."""
    reg = _RADAR_REGISTRY.get(name)
    if not reg:
        return None
    cache = reg["cache"]
    if not (reg["ready"].is_set() and _is_fresh(cache, RADAR_CACHE_TTL_SEC)):
        try:
            reg["refresh"](RADAR_CACHE_TTL_SEC, False)
        except Exception as e:
            print(f"alert sweep: refresh {name} failed: {e}")
    return cache


def _instant_alert_check(endpoint: str, lat: float, lon: float) -> None:
    """Fired in a thread the moment a user enables alerts: if it's already
    raining (or rain is imminent) at their spot, notify within seconds instead
    of waiting for the next radar refresh."""
    try:
        name = _detect_radar(lat, lon)
        reg = _RADAR_REGISTRY.get(name)
        if not reg:
            return
        state = _ensure_radar_fresh_blocking(name)
        if not (state and state.get("latest_frame")):
            return
        alerts.process_alerts(name, state, reg["georef"].is_within_radar,
                              reg["georef"].latlon_to_pixel, only_endpoint=endpoint)
    except Exception as e:
        print(f"instant alert check failed: {e}")


def _sweep_alerts() -> dict:
    """Refresh every radar that has at least one saved subscription and run its
    alert checks. Driven by the scheduled keep-alive so alerts fire on time even
    when no one is actively using that radar city."""
    try:
        coords = alerts.all_subscription_coords()
    except Exception as e:
        print(f"alert sweep: could not read subscriptions: {e}")
        return {"ok": False, "error": str(e)}
    radars = sorted({_detect_radar(lat, lon) for lat, lon in coords})
    refreshed = []
    for name in radars:
        reg = _RADAR_REGISTRY.get(name)
        was_fresh = bool(reg and reg["ready"].is_set() and _is_fresh(reg["cache"], RADAR_CACHE_TTL_SEC))
        state = _ensure_radar_fresh_blocking(name)
        # A stale-cache refresh already runs process_alerts internally on
        # completion. But if the cache was already fresh (e.g. a user browsed
        # this radar's city a few minutes ago), the refresh above no-ops and
        # alerts would otherwise be silently skipped for this sweep — so call
        # it explicitly in that case.
        if was_fresh and state and reg:
            try:
                alerts.process_alerts(name, state, reg["georef"].is_within_radar,
                                      reg["georef"].latlon_to_pixel)
            except Exception as e:
                print(f"alert sweep: process_alerts {name} failed: {e}")
        refreshed.append(name)
    return {"ok": True, "subscriptions": len(coords), "radars_refreshed": refreshed}


# ENDPOINT 1: Health Check

@app.get("/health")
def health():
    import db as _db
    return {
        "status": "ok",
        "service": "Garaj Baras API",
        "version": "1.0.0",
        "db": "postgres" if _db.IS_POSTGRES else "sqlite",
    }


@app.get("/debug/cache")
def debug_cache():
    """Inspect radar cache state. Used to diagnose slow-request issues."""
    def _summary(cache, ready_event, gif_path):
        last = cache.get("last_loaded")
        age = round(time.time() - float(last), 1) if last else None
        return {
            "ready": ready_event.is_set(),
            "fresh": _is_fresh(cache, RADAR_CACHE_TTL_SEC),
            "frame_count": len(cache.get("frame_data") or []),
            "has_movement": bool(cache.get("movement")),
            "has_latest_frame": bool(cache.get("latest_frame")),
            "cache_age_sec": age,
            "gif_exists": os.path.exists(gif_path) if gif_path else False,
        }
    return {
        "ttl_sec": RADAR_CACHE_TTL_SEC,
        "pid": os.getpid(),
        "delhi":   _summary(radar_cache,   _delhi_ready,   GIF_SAVE_PATH),
        "lucknow": _summary(lucknow_cache, _lucknow_ready, radar_lucknow.GIF_SAVE_PATH),
        "patna":   _summary(patna_cache,   _patna_ready,   radar_patna.GIF_SAVE_PATH),
        "bhopal":  _summary(bhopal_cache,  _bhopal_ready,  radar_bhopal.GIF_SAVE_PATH),
    }

# ENDPOINT 2: Current Rain Movement

@app.get("/movement")
def get_movement():
    """
    Returns current rain movement direction 
    and speed over Delhi NCR
    """
    try:
        all_frame_data = get_all_frames()
        recent_frame_data = get_recent_frames(n=6)
        all_paths = [p for (p, _ts) in all_frame_data]
        clutter_mask = build_clutter_mask(all_paths)
        
        dx, dy, dir_from, dir_to, speed = get_movement_vector(
            recent_frame_data, clutter_mask=clutter_mask
        )
        latest_ts = recent_frame_data[-1][1] if recent_frame_data else None
        lag_info = get_radar_lag_mins(latest_ts)
        
        return {
            "direction_from": dir_from,
            "direction_to": dir_to,
            "speed_kmh": round(float(speed), 1),  # type: ignore
            "dx": round(float(dx), 2),  # type: ignore
            "dy": round(float(dy), 2),  # type: ignore
            "radar_lag_mins": lag_info["lag_mins"],
            "radar_freshness": lag_info["freshness"],
            "message": lag_info["message"]
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Movement detection failed: {str(e)}"
        )

# ENDPOINT 3: Latest Radar Frames (PNG + GIF)

@app.get("/radar/gif")
def get_radar_gif(radar: str = "delhi"):
    """Returns the latest downloaded radar GIF. Pass ?radar=lucknow or ?radar=patna."""
    if radar == "lucknow":
        gif_path = radar_lucknow.GIF_SAVE_PATH
        filename  = "lucknow_radar.gif"
    elif radar == "patna":
        gif_path = radar_patna.GIF_SAVE_PATH
        filename  = "patna_radar.gif"
    elif radar == "bhopal":
        gif_path = radar_bhopal.GIF_SAVE_PATH
        filename  = "bhopal_radar.gif"
    else:
        gif_path = GIF_SAVE_PATH
        filename  = "delhi_radar.gif"
    if not os.path.exists(gif_path):
        raise HTTPException(status_code=404, detail=f"{radar.title()} radar GIF not found yet.")
    return FileResponse(gif_path, media_type="image/gif", filename=filename)


@app.get("/frames/latest")
def get_latest_frames(n: int = 6, force: bool = True):
    """
    Fetch latest radar frames.

    - If frames are stale (older than RADAR_TTL_SEC) it refreshes the GIF and re-extracts frames.
    - If `force=true`, it refreshes even if the GIF is still fresh.

    Returns frame URLs served from `/radar/frames/<filename>`.
    """
    if n <= 0:
        n = 6
    n = min(int(n), 60)

    state = _load_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=bool(force))
    frame_data = state.get("frame_data") or []
    latest_ts = state.get("latest_ts")
    lag_info = _fresh_lag_info(state)

    last_n = frame_data[-n:] if len(frame_data) > n else frame_data
    frames = []
    for fp, ts in last_n:
        frames.append({
            "filename": os.path.basename(fp),
            "url": f"/radar/frames/{os.path.basename(fp)}",
            "timestamp_ist": ts.isoformat() if ts else None,
        })

    return {
        "count": len(frames),
        "requested": int(n),
        "force": bool(force),
        "gif_url": "/radar/gif",
        "latest_timestamp_ist": latest_ts.isoformat() if latest_ts else None,
        "radar_lag_mins": lag_info.get("lag_mins"),
        "radar_freshness": lag_info.get("freshness"),
        "radar_message": lag_info.get("message"),
        "frames": frames,
    }

# ENDPOINT 4: Predict Rain For Provided Waypoints

@app.post("/predict_waypoints")
def predict_waypoints(payload: PredictWaypointsRequest):
    """
    Predict rain for a frontend-provided set of waypoints with explicit ETAs.
    Radar is auto-selected based on which one covers the route midpoint.
    """
    try:
        if not payload.waypoints:
            raise HTTPException(status_code=400, detail="No waypoints provided.")

        # Validate coordinates are in India roughly (and ETAs are non-negative)
        for wp in payload.waypoints:
            if not (6 < wp.lat < 38 and 68 < wp.lon < 98):
                raise HTTPException(
                    status_code=400,
                    detail=f"Coordinates ({wp.lat},{wp.lon}) outside India bounds",
                )
            if wp.eta_mins < 0:
                raise HTTPException(status_code=400, detail="ETA minutes must be >= 0.")

        # Auto-detect radar from route midpoint
        mid = payload.waypoints[len(payload.waypoints) // 2]
        radar = _detect_radar(mid.lat, mid.lon)

        if radar == "lucknow":
            _georef = georef_lucknow
            state   = _load_lucknow_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
        elif radar == "patna":
            _georef = georef_patna
            state   = _load_patna_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
        elif radar == "bhopal":
            _georef = georef_bhopal
            state   = _load_bhopal_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
        else:
            _georef = _georef_delhi
            state   = _load_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)

        _latlon_to_pixel = _georef.latlon_to_pixel
        _is_within_radar = _georef.is_within_radar
        _touch_radar_and_evict(radar)

        clutter_mask = state["clutter_mask"]
        dx, dy, dir_from, dir_to, speed = state["movement"]
        latest_frame = state["latest_frame"]
        lag_info = _fresh_lag_info(state)
        decay_tracks = state.get("decay_tracks") or []

        # Per-request shared work: open the latest frame + compute base rain
        # mask exactly ONCE and pass them into both check_route_rain and
        # enrich_results. Previously both helpers re-opened the PIL image,
        # and predict_rain_position re-ran isolate_rain for every time offset.
        frame_rgb = None
        base_rain_mask = None
        try:
            if latest_frame is not None:
                frame_rgb = np.array(Image.open(latest_frame).convert('RGB'))
                base_rain_mask = isolate_rain(latest_frame, clutter_mask=clutter_mask)
        except Exception:
            frame_rgb = None
            base_rain_mask = None

        # Convert to pixels and build (lat,lon,eta) tuples for enrichment
        waypoints_pixels = []
        waypoints_latlon = []
        for wp in payload.waypoints:
            px, py = _latlon_to_pixel(wp.lat, wp.lon)
            waypoints_pixels.append((px, py, float(wp.eta_mins)))
            waypoints_latlon.append((float(wp.lat), float(wp.lon), float(wp.eta_mins)))

        max_eta = max((eta for (_la, _lo, eta) in waypoints_latlon), default=0.0)

        # --- Per-patch route intercept ---
        patch_analysis = None
        try:
            patches_motion = state.get("patches", []) or []
            if patches_motion:
                scored = score_patches_for_route(
                    patches_motion,
                    waypoints_pixels,
                    lag_mins=lag_info["lag_mins"],
                    horizon_mins=max_eta if max_eta > 0 else 120.0,
                )
                first = scored["first"]
                patch_analysis = {
                    "first_patch": _patch_to_public(first) if first else None,
                    "relevant_count": len(scored["relevant"]),
                    "relevant_patches": [_patch_to_public(p) for p in scored["relevant"]],
                    "all_patches": [_patch_to_public(p) for p in scored["patches"]],
                }
        except Exception as _pe:
            patch_analysis = {"error": str(_pe)}

        results = check_route_rain(
            waypoints_pixels,
            dx,
            dy,
            latest_frame,
            eta_minutes=max_eta,
            clutter_mask=clutter_mask,
            lag_mins=lag_info["lag_mins"],
            frame_rgb=frame_rgb,
            base_rain_mask=base_rain_mask,
            patches=state.get("patches") or [],
        )

        enriched = enrich_results(
            results,
            waypoints_latlon,
            latest_frame,
            dx,
            dy,
            lag_info=lag_info,
            frame_rgb=frame_rgb,
            latlon_to_pixel_fn=_latlon_to_pixel,
        )

        for e, (px, py, _eta) in zip(enriched, waypoints_pixels):
            e["in_radar_bounds"] = _is_within_radar(e["lat"], e["lon"])
            if e["rain_expected"] and decay_tracks:
                lag = lag_info["lag_mins"]
                effective_eta = e["eta_mins"] + lag
                # Source pixel in the latest frame: prefer the per-patch
                # back-projection from check_route_rain, else global vector.
                if e.get("src_px") is not None and e.get("src_py") is not None:
                    src_px = int(e["src_px"])
                    src_py = int(e["src_py"])
                else:
                    frames_ahead = effective_eta / 10.0
                    src_px = int(px - dx * frames_ahead)
                    src_py = int(py - dy * frames_ahead)
                decay_info = get_decay_status_at_pixel(src_px, src_py, effective_eta, decay_tracks)
                e["decay_status"] = decay_info["decay_status"]
                e["projected_dbz"] = decay_info["projected_dbz"]
                e["current_dbz_patch"] = decay_info["current_dbz"]
            else:
                e["decay_status"] = None
                e["projected_dbz"] = None
                e["current_dbz_patch"] = None

        # Log every waypoint claim for automated hit-rate verification
        try:
            verification.log_predictions(
                radar, "route",
                [{
                    "lat": e["lat"], "lon": e["lon"],
                    "px": pxy[0], "py": pxy[1],
                    "eta_mins": e["eta_mins"],
                    "rain_expected": e["rain_expected"],
                    "label": e["label"], "dbz": e["dbz"],
                    "confidence": e["confidence"],
                } for e, pxy in zip(enriched, waypoints_pixels)],
                lag_mins=lag_info.get("lag_mins"),
            )
        except Exception as _ve:
            print(f"verification logging failed: {_ve}")

        rain_wps = [e for e in enriched if e["rain_expected"]]
        clear_wps = [e for e in enriched if not e["rain_expected"]]
        first_rain = next((e for e in enriched if e["rain_expected"]), None)

        return {
            "total_waypoints": len(enriched),
            "rain_waypoints": len(rain_wps),
            "clear_waypoints": len(clear_wps),
            "first_rain_eta": first_rain["eta_mins"] if first_rain else None,
            "first_rain_label": first_rain["label"] if first_rain else None,
            "rain_direction_from": dir_from,
            "rain_direction_to": dir_to,
            "rain_speed_kmh": round(float(speed), 1),  # type: ignore
            "radar_lag_mins": lag_info["lag_mins"],
            "radar_freshness": lag_info["freshness"],
            "radar_message": lag_info["message"],
            "waypoints": enriched,
            "patch_analysis": patch_analysis,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Waypoint prediction failed: {str(e)}\n{traceback.format_exc()}",
        )


# ENDPOINT 5: Predict Rain On Route

@app.post("/predict")
def predict_rain(route: RouteRequest):
    """
    Main endpoint.
    Takes start + end coordinates.
    Returns rain prediction for every 
    waypoint along the route.
    """
    try:
        # Validate coordinates are in India roughly
        for lat, lon in [
            (route.start_lat, route.start_lon),
            (route.end_lat, route.end_lon)
        ]:
            if not (6 < lat < 38 and 68 < lon < 98):
                raise HTTPException(
                    status_code=400,
                    detail=f"Coordinates ({lat},{lon}) outside India bounds"
                )

        # Load radar state with TTL lazy-cache (no download/re-extract if GIF fresh)
        state = _load_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
        _touch_radar_and_evict("delhi")
        clutter_mask = state["clutter_mask"]
        dx, dy, dir_from, dir_to, speed = state["movement"]
        latest_frame = state["latest_frame"]
        lag_info = _fresh_lag_info(state)

        # Pre-load frame RGB + base rain mask once (see /predict_waypoints)
        frame_rgb = None
        base_rain_mask = None
        try:
            if latest_frame is not None:
                frame_rgb = np.array(Image.open(latest_frame).convert('RGB'))
                base_rain_mask = isolate_rain(latest_frame, clutter_mask=clutter_mask)
        except Exception:
            frame_rgb = None
            base_rain_mask = None

        # Generate waypoints
        waypoints_latlon = generate_waypoints(
            (route.start_lat, route.start_lon),
            (route.end_lat, route.end_lon),
            spacing_km=route.spacing_km,
            eta_spacing_minutes=route.eta_spacing_minutes,
        )

        total_dist = haversine_km(
            route.start_lat, route.start_lon,
            route.end_lat, route.end_lon
        )

        # Convert to pixels
        waypoints_pixels = []
        for lat, lon, eta in waypoints_latlon:
            px, py = latlon_to_pixel(lat, lon)
            waypoints_pixels.append((px, py, eta))

        max_eta = waypoints_latlon[-1][2]

        # Run prediction
        results = check_route_rain(
            waypoints_pixels, dx, dy,
            latest_frame,
            eta_minutes=max_eta,
            clutter_mask=clutter_mask,
            lag_mins=lag_info["lag_mins"],
            frame_rgb=frame_rgb,
            base_rain_mask=base_rain_mask,
            patches=state.get("patches") or [],
        )

        # Enrich with fuzzy intensity
        enriched = enrich_results(
            results, waypoints_latlon,
            latest_frame, dx, dy,
            lag_info=lag_info,
            frame_rgb=frame_rgb,
        )

        # Add bounds check to each waypoint
        for e in enriched:
            e["in_radar_bounds"] = is_within_radar(
                e["lat"], e["lon"]
            )

        # Build summary
        rain_wps = [e for e in enriched if e["rain_expected"]]
        clear_wps = [e for e in enriched if not e["rain_expected"]]

        first_rain = next(
            (e for e in enriched if e["rain_expected"]), None
        )

        return {
            "route_distance_km": round(float(total_dist), 1),  # type: ignore
            "total_waypoints": len(enriched),
            "rain_waypoints": len(rain_wps),
            "clear_waypoints": len(clear_wps),
            "first_rain_eta": first_rain["eta_mins"] if first_rain else None,
            "first_rain_label": first_rain["label"] if first_rain else None,
            "rain_direction_from": dir_from,
            "rain_direction_to": dir_to,
            "rain_speed_kmh": round(float(speed), 1),  # type: ignore
            "radar_lag_mins": lag_info["lag_mins"],
            "radar_freshness": lag_info["freshness"],
            "radar_message": lag_info["message"],
            "waypoints": enriched
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Prediction failed: {str(e)}\n{traceback.format_exc()}"
        )

# ENDPOINT: Point Nowcast

class NowcastRequest(BaseModel):
    lat: float
    lon: float


@app.post("/nowcast")
def nowcast_location(req: NowcastRequest):
    """
    Predict rain arrival at a fixed location within 120 minutes.
    Radar is auto-selected based on which one covers the point.
    """
    try:
        if not (6 < req.lat < 38 and 68 < req.lon < 98):
            raise HTTPException(status_code=400, detail="Coordinates outside India bounds.")

        radar = _detect_radar(req.lat, req.lon)

        if radar == "lucknow":
            _georef = georef_lucknow
            state   = _load_lucknow_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
        elif radar == "patna":
            _georef = georef_patna
            state   = _load_patna_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
        elif radar == "bhopal":
            _georef = georef_bhopal
            state   = _load_bhopal_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
        else:
            _georef = _georef_delhi
            state   = _load_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)

        _latlon_to_pixel = _georef.latlon_to_pixel
        _is_within_radar = _georef.is_within_radar
        _touch_radar_and_evict(radar)

        if not _is_within_radar(req.lat, req.lon):
            return {
                "in_radar_bounds": False,
                "events": [],
                "summary": "Location is outside radar coverage area.",
                "radar_as_of": None,
                "lag_mins": None,
                "total_events": 0,
            }

        dx, dy, _dir_from, _dir_to, _speed = state["movement"]
        latest_frame = state.get("latest_frame")
        lag_info = _fresh_lag_info(state)
        lag_mins = float(lag_info.get("lag_mins", DEFAULT_RADAR_LAG_MINS))
        patch_tracks = state.get("decay_tracks") or []
        patches_motion = state.get("patches") or []
        clutter_mask = state.get("clutter_mask")
        latest_ts = state.get("latest_ts")

        if not latest_frame:
            raise HTTPException(status_code=503, detail="Radar data not yet loaded. Try again in a moment.")

        rain_mask = isolate_rain(latest_frame, clutter_mask=clutter_mask)
        rgb_arr = np.array(Image.open(latest_frame).convert("RGB"))

        user_px, user_py = _latlon_to_pixel(req.lat, req.lon)

        from nowcast import compute_nowcast_slots  # type: ignore
        slots = compute_nowcast_slots(
            user_px=float(user_px),
            user_py=float(user_py),
            dx=float(dx),
            dy=float(dy),
            rain_mask=rain_mask,
            rgb_arr=rgb_arr,
            patch_tracks=patch_tracks,
            lag_mins=lag_mins,
            patches_motion=patches_motion,
        )

        as_of = latest_ts.strftime("%H:%M IST") if latest_ts else "unknown"

        # Log every slot claim for automated hit-rate verification
        try:
            verification.log_predictions(
                radar, "nowcast",
                [{
                    "lat": req.lat, "lon": req.lon,
                    "px": user_px, "py": user_py,
                    "eta_mins": s["slot_mins"],
                    "rain_expected": s["has_rain"],
                    "label": s.get("intensity"),
                    "dbz": s.get("projected_dbz"),
                    "confidence": s.get("arrival_confidence"),
                } for s in slots],
                lag_mins=lag_mins,
            )
        except Exception as _ve:
            print(f"verification logging failed: {_ve}")

        first_rain = next((s for s in slots if s["has_rain"]), None)
        if not first_rain:
            summary = "No rains for the next 2 hours"
        elif first_rain["slot_mins"] <= 0:
            summary = f"Rain right now · {first_rain['probability']}% probability"
        else:
            summary = f"Rain in ~{int(first_rain['slot_mins'])} min · {first_rain['probability']}% probability"

        rain_slot_count = sum(1 for s in slots if s["has_rain"])

        return {
            "in_radar_bounds": True,
            "slots": slots,
            "summary": summary,
            "radar_as_of": as_of,
            "lag_mins": round(lag_mins, 1),
            "rain_slots": rain_slot_count,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Nowcast failed: {str(e)}\n{traceback.format_exc()}"
        )


# ENDPOINT: 1-hour Forecast Radar Animation (visualizes the prediction engine)

def _forecast_render_args(lat: float, lon: float) -> dict:
    """Shared setup for the forecast animation endpoints."""
    if not (6 < lat < 38 and 68 < lon < 98):
        raise HTTPException(status_code=400, detail="Coordinates outside India bounds.")

    radar = _detect_radar(lat, lon)
    if radar == "lucknow":
        _georef = georef_lucknow
        state   = _load_lucknow_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
    elif radar == "patna":
        _georef = georef_patna
        state   = _load_patna_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
    elif radar == "bhopal":
        _georef = georef_bhopal
        state   = _load_bhopal_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
    else:
        _georef = _georef_delhi
        state   = _load_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)

    _touch_radar_and_evict(radar)

    if not _georef.is_within_radar(lat, lon):
        raise HTTPException(status_code=404, detail="Location outside radar coverage.")

    latest_frame = state.get("latest_frame")
    if not latest_frame:
        raise HTTPException(status_code=503, detail="Radar data not yet loaded. Try again in a moment.")

    dx, dy, _f, _t, _s = state["movement"]
    lag_info = _fresh_lag_info(state)
    user_px, user_py = _georef.latlon_to_pixel(lat, lon)
    return dict(
        frame_rgb=np.array(Image.open(latest_frame).convert("RGB")),
        rain_mask=isolate_rain(latest_frame, clutter_mask=state.get("clutter_mask")),
        patches=state.get("patches") or [],
        tracks=state.get("decay_tracks") or [],
        gdx=float(dx), gdy=float(dy),
        lag_mins=float(lag_info.get("lag_mins", DEFAULT_RADAR_LAG_MINS)),
        user_px=user_px, user_py=user_py,
        frame_data=state.get("frame_data") or [],
        radar_name=radar,
        latlon_to_pixel_fn=_georef.latlon_to_pixel,
    )


@app.get("/nowcast/forecast_gif")
def nowcast_forecast_gif(lat: float, lon: float):
    """
    Animated GIF: latest radar + forecast frames at +15/+30/+45/+60 min around
    (lat, lon), cropped to the nowcast patch-search circle. Each patch moves
    with its own measured velocity and fades with its decay trend — the exact
    simulation behind the nowcast slots.
    """
    try:
        args = _forecast_render_args(lat, lon)
        from forecast_gif import render_forecast_gif  # type: ignore
        gif_bytes = render_forecast_gif(
            args["frame_rgb"], args["rain_mask"], args["patches"], args["tracks"],
            args["gdx"], args["gdy"], args["lag_mins"], args["user_px"], args["user_py"],
        )
        return Response(
            content=gif_bytes,
            media_type="image/gif",
            headers={"Cache-Control": "no-store"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Forecast GIF failed: {str(e)}\n{traceback.format_exc()}",
        )


@app.get("/nowcast/forecast_frames")
def nowcast_forecast_frames(lat: float, lon: float):
    """
    Same forecast animation as /nowcast/forecast_gif but as individual
    base64-PNG frames with labels, so the frontend player can pause/play/scrub.
    """
    try:
        args = _forecast_render_args(lat, lon)
        from forecast_gif import render_forecast_frames_payload  # type: ignore
        payload = render_forecast_frames_payload(
            args["frame_rgb"], args["rain_mask"], args["patches"], args["tracks"],
            args["gdx"], args["gdy"], args["lag_mins"], args["user_px"], args["user_py"],
        )
        return payload
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Forecast frames failed: {str(e)}\n{traceback.format_exc()}",
        )


@app.get("/nowcast/radar_scene")
def nowcast_radar_scene(lat: float, lon: float):
    """
    v2 radar animation scene: compact JSON with dBZ history grids (real frame
    timestamps, ~10-min cadence) + per-patch motion/decay parameters. The
    frontend canvas player renders it and interpolates motion continuously
    from -60ish to +60 min. Replaces the base64-PNG forecast_frames payload.
    """
    try:
        args = _forecast_render_args(lat, lon)
        from radar_scene import build_radar_scene  # type: ignore
        return build_radar_scene(
            args["frame_data"], args["rain_mask"], args["patches"],
            args["tracks"], args["gdx"], args["gdy"], args["lag_mins"],
            args["user_px"], args["user_py"],
            radar_name=args["radar_name"],
            latlon_to_pixel_fn=args["latlon_to_pixel_fn"],
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Radar scene failed: {str(e)}\n{traceback.format_exc()}",
        )


# ENDPOINTS: Rain alerts (web push for saved locations)

class AlertSubscribeRequest(BaseModel):
    subscription: dict
    lat: float
    lon: float
    label: str | None = None


class AlertEndpointRequest(BaseModel):
    endpoint: str


@app.get("/alerts/vapid_public_key")
def alerts_vapid_public_key():
    key = alerts.public_key()
    if not key:
        raise HTTPException(status_code=503, detail="Push not configured (no VAPID keys).")
    return {"public_key": key}


@app.post("/alerts/subscribe")
def alerts_subscribe(req: AlertSubscribeRequest, user=Depends(get_optional_user)):
    if not (6 < req.lat < 38 and 68 < req.lon < 98):
        raise HTTPException(status_code=400, detail="Coordinates outside India bounds.")
    ok = alerts.subscribe(req.subscription, req.lat, req.lon, req.label,
                          user_id=(user or {}).get("id"))
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid subscription.")
    # Instant first check (background): if it's already raining at this spot,
    # the user hears about it in seconds rather than at the next radar refresh.
    endpoint = (req.subscription or {}).get("endpoint")
    if endpoint:
        threading.Thread(target=_instant_alert_check,
                         args=(endpoint, req.lat, req.lon), daemon=True).start()
    return {"ok": True, "message": "Rain alerts enabled for this location."}


@app.post("/alerts/unsubscribe")
def alerts_unsubscribe(req: AlertEndpointRequest):
    return {"ok": alerts.unsubscribe(req.endpoint)}


@app.get("/tasks/sweep_alerts")
def tasks_sweep_alerts(token: str = ""):
    """Scheduled alert sweep: refresh radars that have saved subscriptions and
    fire due notifications. Called by the keep-alive workflow every ~10-15 min so
    alerts don't depend on someone happening to browse. Protected by SWEEP_TOKEN
    when that env var is set (leave unset to allow open calls in dev)."""
    expected = (os.environ.get("SWEEP_TOKEN") or "").strip()
    if expected and token != expected:
        raise HTTPException(status_code=403, detail="Bad sweep token.")
    return _sweep_alerts()


@app.get("/alerts/debug")
def alerts_debug(send_test: int = 0, token: str = ""):
    """Diagnostic: list subscriptions (no keys) and optionally fire a test push
    to each. `?send_test=1` proves whether push delivery reaches the browser.
    Gated by SWEEP_TOKEN when that env var is set."""
    expected = (os.environ.get("SWEEP_TOKEN") or "").strip()
    if expected and token != expected:
        raise HTTPException(status_code=403, detail="Bad token.")
    return {
        "vapid_public_key": alerts.public_key_fingerprint(),
        "subscriptions": alerts.debug_list(send_test=bool(send_test)),
    }


@app.post("/alerts/test")
def alerts_test(req: AlertEndpointRequest):
    """Fire a test notification to one subscription (for setup verification)."""
    row = alerts.get_subscription(req.endpoint)
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found.")
    alive = alerts._send_push(row[0], "🔔 Garaj Baras test",
                              f"Rain alerts are working for {row[1] or 'your location'}.")
    return {"ok": bool(alive)}


# ENDPOINTS: Accounts + saved locations (Supabase Auth; auth.py verifies the
# JWT locally). Nowcast/route stay public — sign-in gates only these extras.

class SavedLocationCreate(BaseModel):
    label: str
    lat: float
    lon: float
    alerts_enabled: bool = False


class SavedLocationUpdate(BaseModel):
    label: str | None = None
    alerts_enabled: bool | None = None


@app.get("/me")
def me(user=Depends(get_current_user)):
    """Upsert + return the signed-in user (first authenticated call creates
    the users row)."""
    return accounts.upsert_user(user["id"], user.get("email"))


@app.get("/locations")
def locations_list(user=Depends(get_current_user)):
    return {"locations": accounts.list_locations(user["id"])}


@app.post("/locations")
def locations_add(req: SavedLocationCreate, user=Depends(get_current_user)):
    if not (6 < req.lat < 38 and 68 < req.lon < 98):
        raise HTTPException(status_code=400, detail="Coordinates outside India bounds.")
    label = (req.label or "").strip()[:60]
    if not label:
        raise HTTPException(status_code=400, detail="Label required.")
    accounts.upsert_user(user["id"], user.get("email"))
    loc = accounts.add_location(user["id"], label, req.lat, req.lon,
                                req.alerts_enabled)
    if loc is None:
        raise HTTPException(status_code=400,
                            detail=f"Limit of {accounts.MAX_LOCATIONS_PER_USER} saved places reached.")
    return loc


@app.patch("/locations/{loc_id}")
def locations_update(loc_id: int, req: SavedLocationUpdate,
                     user=Depends(get_current_user)):
    label = req.label.strip()[:60] if req.label is not None else None
    ok = accounts.update_location(user["id"], loc_id, label=label,
                                  alerts_enabled=req.alerts_enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Location not found.")
    return {"ok": True}


@app.delete("/locations/{loc_id}")
def locations_delete(loc_id: int, user=Depends(get_current_user)):
    if not accounts.delete_location(user["id"], loc_id):
        raise HTTPException(status_code=404, detail="Location not found.")
    return {"ok": True}


# ENDPOINT: Verified prediction accuracy (automated hit-rate tracking)

@app.get("/stats/accuracy")
def stats_accuracy(days: int = 30):
    """
    Aggregated verified accuracy of past predictions, graded automatically
    against later radar frames. POD/FAR/CSI computed from outcome counts.
    """
    try:
        days = max(1, min(int(days), 365))
        return verification.accuracy_stats(days=days)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Accuracy stats failed: {str(e)}\n{traceback.format_exc()}",
        )


# ENDPOINT: AI Rain Chatbot (Gemini 2.5 Flash + function calling)

import chatbot  # type: ignore


class ChatMessage(BaseModel):
    role: str  # "user" | "model"
    text: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]


# Wire the chatbot's tools to the existing in-process endpoint logic.
# Each wrapper mirrors an existing endpoint so tools reuse the real radar engine.
def _tool_get_nowcast(lat: float, lon: float):
    return nowcast_location(NowcastRequest(lat=lat, lon=lon))


def _tool_get_route_rain(start_lat, start_lon, end_lat, end_lon):
    return predict_rain(RouteRequest(
        start_lat=start_lat, start_lon=start_lon,
        end_lat=end_lat, end_lon=end_lon,
    ))


def _tool_get_rain_movement():
    return get_movement()


def _tool_get_accuracy_stats(days: int = 30):
    return stats_accuracy(days=days)


chatbot.register_tools({
    "get_nowcast": _tool_get_nowcast,
    "get_route_rain": _tool_get_route_rain,
    "get_rain_movement": _tool_get_rain_movement,
    "get_accuracy_stats": _tool_get_accuracy_stats,
})


@app.post("/chat")
def chat(req: ChatRequest):
    """
    AI rain assistant. Streams Server-Sent Events:
    {"type":"text","delta"} | {"type":"tool","name"} | {"type":"done"} | {"type":"error","message"}
    """
    if not chatbot.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Chatbot not configured (GEMINI_API_KEY missing on server).",
        )
    messages = [{"role": m.role, "text": m.text} for m in req.messages]
    return StreamingResponse(
        chatbot.chat_stream(messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# RUN INSTRUCTIONS:
# cd backend
# .\venv\Scripts\activate
# uvicorn main:app --reload --port 8000
