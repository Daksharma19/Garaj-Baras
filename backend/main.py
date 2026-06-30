from fastapi import FastAPI, HTTPException  # type: ignore
from fastapi.middleware.cors import CORSMiddleware  # type: ignore
from fastapi.responses import FileResponse  # type: ignore
from fastapi.staticfiles import StaticFiles  # type: ignore
from pydantic import BaseModel  # type: ignore
from typing import List
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


@app.on_event("startup")
def _warm_radar_cache_on_startup():
    """
    Kick radar cache warm-up in the background at server boot so the very first
    /predict_waypoints call doesn't pay the full download + OCR + optical-flow
    cost (which on Render free tier compounds with cold-start latency).
    """
    def _worker():
        try:
            _load_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
            print("Delhi radar cache warmed at startup.")
        except Exception as e:
            print(f"Delhi startup warm-up failed (non-fatal): {e}")

    def _lucknow_worker():
        try:
            _load_lucknow_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
            print("Lucknow radar cache warmed at startup.")
        except Exception as e:
            print(f"Lucknow startup warm-up failed (non-fatal): {e}")

    def _patna_worker():
        try:
            _load_patna_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
            print("Patna radar cache warmed at startup.")
        except Exception as e:
            print(f"Patna startup warm-up failed (non-fatal): {e}")

    def _bhopal_worker():
        try:
            _load_bhopal_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
            print("Bhopal radar cache warmed at startup.")
        except Exception as e:
            print(f"Bhopal startup warm-up failed (non-fatal): {e}")

    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_lucknow_worker, daemon=True).start()
    threading.Thread(target=_patna_worker, daemon=True).start()
    threading.Thread(target=_bhopal_worker, daemon=True).start()

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


# Global state - loaded once at startup
# Lazy-cache refreshed by TTL
radar_cache = {
    "clutter_mask": None,
    "last_loaded": None
}

lucknow_cache = {
    "clutter_mask": None,
    "last_loaded": None
}

patna_cache = {
    "clutter_mask": None,
    "last_loaded": None
}

bhopal_cache = {
    "clutter_mask": None,
    "last_loaded": None
}

_radar_state_lock   = threading.Lock()
_lucknow_state_lock = threading.Lock()
_patna_state_lock   = threading.Lock()
_bhopal_state_lock  = threading.Lock()
RADAR_CACHE_TTL_SEC = RADAR_TTL_SEC  # keep a single source of truth


def _cache_is_fresh(ttl_sec: float) -> bool:
    """
    True iff radar_cache was populated less than ttl_sec seconds ago AND has
    all the fields downstream code depends on. Purely in-memory — does NOT
    consult the filesystem, so ephemeral filesystem quirks on Render can't
    bust the cache.
    """
    last = radar_cache.get("last_loaded")
    if not last:
        return False
    if (time.time() - float(last)) >= float(ttl_sec):
        return False
    if not radar_cache.get("frame_data"):
        return False
    if not radar_cache.get("movement"):
        return False
    if radar_cache.get("clutter_mask") is None:
        return False
    if not radar_cache.get("latest_frame"):
        return False
    return True


def _load_radar_state(ttl_sec: float = RADAR_CACHE_TTL_SEC, *, force: bool = False) -> dict:
    """
    Lazy cache manager used by /predict.

    Fresh path (cache populated < ttl_sec ago): returns in-memory cache with
    no disk I/O. Stale path: refreshes GIF, clears old PNGs, extracts frames
    + timestamps, then recomputes clutter mask + movement vector once.
    """
    # Fast path: purely in-memory, no filesystem checks
    if (not force) and _cache_is_fresh(ttl_sec):
        return radar_cache

    with _radar_state_lock:
        # Re-check inside lock
        if (not force) and _cache_is_fresh(ttl_sec):
            return radar_cache

        now = time.time()
        try:
            gif_fresh = os.path.exists(GIF_SAVE_PATH) and (now - os.path.getmtime(GIF_SAVE_PATH) < ttl_sec)
        except Exception:
            gif_fresh = False

        # If stale/missing, refresh (download + clear PNGs + extract frames)
        frame_data, did_refresh = refresh_frames_if_stale(ttl_sec=ttl_sec, force=force, clear_pngs=True)
        if did_refresh:
            all_frame_data = frame_data
        else:
            # If GIF is fresh but we don't have in-memory cache (e.g. server restart),
            # do a *no-download* extract from the existing GIF once.
            # NOTE: we import FRAMES_FOLDER locally but DO NOT rebind GIF_SAVE_PATH here —
            # importing it inside this function body would make Python treat GIF_SAVE_PATH
            # as a local for the whole function and raise UnboundLocalError when the
            # branch above (`did_refresh=True`) runs instead.
            from radar import extract_frames, FRAMES_FOLDER  # type: ignore
            all_frame_data = extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER) if gif_fresh else get_all_frames()

        recent_frame_data = all_frame_data[-6:] if len(all_frame_data) > 6 else all_frame_data
        all_paths = [p for (p, _ts) in all_frame_data]
        clutter_mask = build_clutter_mask(all_paths)
        dx, dy, dir_from, dir_to, speed = get_movement_vector(
            recent_frame_data, clutter_mask=clutter_mask
        )
        latest_frame = recent_frame_data[-1][0] if recent_frame_data else None
        latest_ts = recent_frame_data[-1][1] if recent_frame_data else None
        lag_info = get_radar_lag_mins(latest_ts)

        # --- Per-patch motion (NCR-focused) ---
        # Use last 4 frames (3 pairs) so optical flow is averaged over multiple
        # frame transitions — more stable velocity for persistent patches.
        # Fresh pop-ups (only in the last frame) are naturally dampened.
        patches_motion = []
        roi_mask = None
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
                    motion_frames, gap_mins=gap,
                    clutter_mask=clutter_mask, roi_mask=roi_mask, min_area_px=4,
                )
                print(f"  Per-patch: {len(patches_motion)} patch(es) detected (gap={gap:.0f}m, frames={len(motion_frames)})")
        except Exception as _pe:
            print(f"  Per-patch motion failed (non-fatal): {_pe}")
            patches_motion, roi_mask = [], None

        # --- Decay track computation ---
        decay_tracks = []
        try:
            decay_tracks = compute_decay_tracks(all_frame_data, dx, dy, clutter_mask=clutter_mask)
            print(f"  Decay tracks: {len(decay_tracks)} patch(es) tracked across {len(all_frame_data)} frames")
        except Exception as _de:
            print(f"  Decay tracking failed (non-fatal): {_de}")

        radar_cache.update({
            "frame_data": all_frame_data,
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
        })
        return radar_cache


# ── Lucknow radar cache ───────────────────────────────────────────────────────

def _lucknow_cache_is_fresh(ttl_sec: float) -> bool:
    last = lucknow_cache.get("last_loaded")
    if not last:
        return False
    if (time.time() - float(last)) >= float(ttl_sec):
        return False
    if not lucknow_cache.get("frame_data"):
        return False
    if not lucknow_cache.get("movement"):
        return False
    if lucknow_cache.get("clutter_mask") is None:
        return False
    if not lucknow_cache.get("latest_frame"):
        return False
    return True


def _load_lucknow_radar_state(ttl_sec: float = RADAR_CACHE_TTL_SEC, *, force: bool = False) -> dict:
    if (not force) and _lucknow_cache_is_fresh(ttl_sec):
        return lucknow_cache

    with _lucknow_state_lock:
        if (not force) and _lucknow_cache_is_fresh(ttl_sec):
            return lucknow_cache

        now = time.time()
        lk_gif = radar_lucknow.GIF_SAVE_PATH
        try:
            gif_fresh = os.path.exists(lk_gif) and (now - os.path.getmtime(lk_gif) < ttl_sec)
        except Exception:
            gif_fresh = False

        frame_data, did_refresh = radar_lucknow.refresh_frames_if_stale(
            ttl_sec=ttl_sec, force=force, clear_pngs=True
        )
        if did_refresh:
            all_frame_data = frame_data
        else:
            if gif_fresh:
                all_frame_data = radar_lucknow.extract_frames(lk_gif, radar_lucknow.FRAMES_FOLDER)
            else:
                all_frame_data = radar_lucknow.get_all_frames()

        recent_frame_data = all_frame_data[-6:] if len(all_frame_data) > 6 else all_frame_data
        all_paths = [p for (p, _ts) in all_frame_data]
        clutter_mask = build_clutter_mask(all_paths)
        dx, dy, dir_from, dir_to, speed = get_movement_vector(
            recent_frame_data, clutter_mask=clutter_mask
        )
        latest_frame = recent_frame_data[-1][0] if recent_frame_data else None
        latest_ts    = recent_frame_data[-1][1] if recent_frame_data else None
        lag_info     = radar_lucknow.get_radar_lag_mins(latest_ts)

        patches_motion = []
        roi_mask = None
        try:
            roi_mask = build_roi_mask(
                georef_lucknow.latlon_to_pixel,
                georef_lucknow.IMAGE_WIDTH,
                georef_lucknow.IMAGE_HEIGHT,
                georef_lucknow.CENTER_LAT,
                georef_lucknow.CENTER_LON,
                radius_km=150.0,
            )
            motion_frames = [p for p, _ in recent_frame_data[-4:]]
            ts_prev = recent_frame_data[-2][1] if len(recent_frame_data) >= 2 else None
            ts_last = recent_frame_data[-1][1] if recent_frame_data else None
            gap = 10.0
            if ts_prev and ts_last:
                gap = max(1.0, (ts_last - ts_prev).total_seconds() / 60.0)
            if len(motion_frames) >= 2:
                patches_motion = compute_patch_motion(
                    motion_frames, gap_mins=gap,
                    clutter_mask=clutter_mask, roi_mask=roi_mask, min_area_px=4,
                    pixel_to_latlon_fn=georef_lucknow.pixel_to_latlon,
                )
                print(f"  Lucknow per-patch: {len(patches_motion)} patch(es) detected (gap={gap:.0f}m, frames={len(motion_frames)})")
        except Exception as _pe:
            print(f"  Lucknow per-patch motion failed (non-fatal): {_pe}")
            patches_motion, roi_mask = [], None

        decay_tracks = []
        try:
            decay_tracks = compute_decay_tracks(all_frame_data, dx, dy, clutter_mask=clutter_mask)
            print(f"  Lucknow decay tracks: {len(decay_tracks)} patch(es) across {len(all_frame_data)} frames")
        except Exception as _de:
            print(f"  Lucknow decay tracking failed (non-fatal): {_de}")

        lucknow_cache.update({
            "frame_data": all_frame_data,
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
        })
        return lucknow_cache


# ── Patna radar cache ─────────────────────────────────────────────────────────

def _patna_cache_is_fresh(ttl_sec: float) -> bool:
    last = patna_cache.get("last_loaded")
    if not last:
        return False
    if (time.time() - float(last)) >= float(ttl_sec):
        return False
    if not patna_cache.get("frame_data"):
        return False
    if not patna_cache.get("movement"):
        return False
    if patna_cache.get("clutter_mask") is None:
        return False
    if not patna_cache.get("latest_frame"):
        return False
    return True


def _load_patna_radar_state(ttl_sec: float = RADAR_CACHE_TTL_SEC, *, force: bool = False) -> dict:
    if (not force) and _patna_cache_is_fresh(ttl_sec):
        return patna_cache

    with _patna_state_lock:
        if (not force) and _patna_cache_is_fresh(ttl_sec):
            return patna_cache

        now = time.time()
        ptn_gif = radar_patna.GIF_SAVE_PATH
        try:
            gif_fresh = os.path.exists(ptn_gif) and (now - os.path.getmtime(ptn_gif) < ttl_sec)
        except Exception:
            gif_fresh = False

        frame_data, did_refresh = radar_patna.refresh_frames_if_stale(
            ttl_sec=ttl_sec, force=force, clear_pngs=True
        )
        if did_refresh:
            all_frame_data = frame_data
        else:
            if gif_fresh:
                all_frame_data = radar_patna.extract_frames(ptn_gif, radar_patna.FRAMES_FOLDER)
            else:
                all_frame_data = radar_patna.get_all_frames()

        recent_frame_data = all_frame_data[-6:] if len(all_frame_data) > 6 else all_frame_data
        all_paths = [p for (p, _ts) in all_frame_data]
        clutter_mask = build_clutter_mask(all_paths)
        dx, dy, dir_from, dir_to, speed = get_movement_vector(
            recent_frame_data, clutter_mask=clutter_mask
        )
        latest_frame = recent_frame_data[-1][0] if recent_frame_data else None
        latest_ts    = recent_frame_data[-1][1] if recent_frame_data else None
        lag_info     = radar_patna.get_radar_lag_mins(latest_ts)

        patches_motion = []
        roi_mask = None
        try:
            roi_mask = build_roi_mask(
                georef_patna.latlon_to_pixel,
                georef_patna.IMAGE_WIDTH,
                georef_patna.IMAGE_HEIGHT,
                georef_patna.CENTER_LAT,
                georef_patna.CENTER_LON,
                radius_km=150.0,
            )
            motion_frames = [p for p, _ in recent_frame_data[-4:]]
            ts_prev = recent_frame_data[-2][1] if len(recent_frame_data) >= 2 else None
            ts_last = recent_frame_data[-1][1] if recent_frame_data else None
            gap = 10.0
            if ts_prev and ts_last:
                gap = max(1.0, (ts_last - ts_prev).total_seconds() / 60.0)
            if len(motion_frames) >= 2:
                patches_motion = compute_patch_motion(
                    motion_frames, gap_mins=gap,
                    clutter_mask=clutter_mask, roi_mask=roi_mask, min_area_px=4,
                    pixel_to_latlon_fn=georef_patna.pixel_to_latlon,
                )
                print(f"  Patna per-patch: {len(patches_motion)} patch(es) detected (gap={gap:.0f}m, frames={len(motion_frames)})")
        except Exception as _pe:
            print(f"  Patna per-patch motion failed (non-fatal): {_pe}")
            patches_motion, roi_mask = [], None

        decay_tracks = []
        try:
            decay_tracks = compute_decay_tracks(all_frame_data, dx, dy, clutter_mask=clutter_mask)
            print(f"  Patna decay tracks: {len(decay_tracks)} patch(es) across {len(all_frame_data)} frames")
        except Exception as _de:
            print(f"  Patna decay tracking failed (non-fatal): {_de}")

        patna_cache.update({
            "frame_data": all_frame_data,
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
        })
        return patna_cache


# ── Bhopal radar cache ────────────────────────────────────────────────────────

def _bhopal_cache_is_fresh(ttl_sec: float) -> bool:
    last = bhopal_cache.get("last_loaded")
    if not last:
        return False
    if (time.time() - float(last)) >= float(ttl_sec):
        return False
    if not bhopal_cache.get("frame_data"):
        return False
    if not bhopal_cache.get("movement"):
        return False
    if bhopal_cache.get("clutter_mask") is None:
        return False
    if not bhopal_cache.get("latest_frame"):
        return False
    return True


def _load_bhopal_radar_state(ttl_sec: float = RADAR_CACHE_TTL_SEC, *, force: bool = False) -> dict:
    if (not force) and _bhopal_cache_is_fresh(ttl_sec):
        return bhopal_cache

    with _bhopal_state_lock:
        if (not force) and _bhopal_cache_is_fresh(ttl_sec):
            return bhopal_cache

        now = time.time()
        bhp_gif = radar_bhopal.GIF_SAVE_PATH
        try:
            gif_fresh = os.path.exists(bhp_gif) and (now - os.path.getmtime(bhp_gif) < ttl_sec)
        except Exception:
            gif_fresh = False

        frame_data, did_refresh = radar_bhopal.refresh_frames_if_stale(
            ttl_sec=ttl_sec, force=force, clear_pngs=True
        )
        if did_refresh:
            all_frame_data = frame_data
        else:
            if gif_fresh:
                all_frame_data = radar_bhopal.extract_frames(bhp_gif, radar_bhopal.FRAMES_FOLDER)
            else:
                all_frame_data = radar_bhopal.get_all_frames()

        recent_frame_data = all_frame_data[-6:] if len(all_frame_data) > 6 else all_frame_data
        all_paths = [p for (p, _ts) in all_frame_data]
        clutter_mask = build_clutter_mask(all_paths)
        dx, dy, dir_from, dir_to, speed = get_movement_vector(
            recent_frame_data, clutter_mask=clutter_mask
        )
        latest_frame = recent_frame_data[-1][0] if recent_frame_data else None
        latest_ts    = recent_frame_data[-1][1] if recent_frame_data else None
        lag_info     = radar_bhopal.get_radar_lag_mins(latest_ts)

        patches_motion = []
        roi_mask = None
        try:
            roi_mask = build_roi_mask(
                georef_bhopal.latlon_to_pixel,
                georef_bhopal.IMAGE_WIDTH,
                georef_bhopal.IMAGE_HEIGHT,
                georef_bhopal.CENTER_LAT,
                georef_bhopal.CENTER_LON,
                radius_km=150.0,
            )
            motion_frames = [p for p, _ in recent_frame_data[-4:]]
            ts_prev = recent_frame_data[-2][1] if len(recent_frame_data) >= 2 else None
            ts_last = recent_frame_data[-1][1] if recent_frame_data else None
            gap = 10.0
            if ts_prev and ts_last:
                gap = max(1.0, (ts_last - ts_prev).total_seconds() / 60.0)
            if len(motion_frames) >= 2:
                patches_motion = compute_patch_motion(
                    motion_frames, gap_mins=gap,
                    clutter_mask=clutter_mask, roi_mask=roi_mask, min_area_px=4,
                    pixel_to_latlon_fn=georef_bhopal.pixel_to_latlon,
                )
                print(f"  Bhopal per-patch: {len(patches_motion)} patch(es) detected (gap={gap:.0f}m, frames={len(motion_frames)})")
        except Exception as _pe:
            print(f"  Bhopal per-patch motion failed (non-fatal): {_pe}")
            patches_motion, roi_mask = [], None

        decay_tracks = []
        try:
            decay_tracks = compute_decay_tracks(all_frame_data, dx, dy, clutter_mask=clutter_mask)
            print(f"  Bhopal decay tracks: {len(decay_tracks)} patch(es) across {len(all_frame_data)} frames")
        except Exception as _de:
            print(f"  Bhopal decay tracking failed (non-fatal): {_de}")

        bhopal_cache.update({
            "frame_data": all_frame_data,
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
        })
        print("Bhopal radar cache warmed.")
        return bhopal_cache


# ENDPOINT 1: Health Check

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Garaj Baras API",
        "version": "1.0.0"
    }


@app.get("/debug/cache")
def debug_cache():
    """Inspect radar cache state. Used to diagnose slow-request issues."""
    frame_data = radar_cache.get("frame_data") or []
    last = radar_cache.get("last_loaded")
    age = (time.time() - float(last)) if last else None
    return {
        "has_frame_data": bool(frame_data),
        "frame_data_len": len(frame_data),
        "has_movement": bool(radar_cache.get("movement")),
        "has_clutter_mask": radar_cache.get("clutter_mask") is not None,
        "has_latest_frame": bool(radar_cache.get("latest_frame")),
        "last_loaded": last,
        "cache_age_sec": age,
        "ttl_sec": RADAR_CACHE_TTL_SEC,
        "cache_is_fresh": _cache_is_fresh(RADAR_CACHE_TTL_SEC),
        "pid": os.getpid(),
        "gif_path_exists": os.path.exists(GIF_SAVE_PATH),
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
    lag_info = state.get("lag_info") or {"lag_mins": 25.0, "freshness": "stale", "message": "Radar ~25 mins old (estimate)"}

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

        clutter_mask = state["clutter_mask"]
        dx, dy, dir_from, dir_to, speed = state["movement"]
        latest_frame = state["latest_frame"]
        lag_info = state["lag_info"]
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
                # Back-project pixel to its position in the latest frame
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
        clutter_mask = state["clutter_mask"]
        dx, dy, dir_from, dir_to, speed = state["movement"]
        latest_frame = state["latest_frame"]
        lag_info = state["lag_info"]

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
        lag_info = state.get("lag_info") or {}
        lag_mins = float(lag_info.get("lag_mins", 10.0))
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


# RUN INSTRUCTIONS:
# cd backend
# .\venv\Scripts\activate
# uvicorn main:app --reload --port 8000
