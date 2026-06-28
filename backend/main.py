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
)
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
            print("Radar cache warmed at startup.")
        except Exception as e:
            print(f"Startup radar warm-up failed (non-fatal): {e}")

    threading.Thread(target=_worker, daemon=True).start()

# Serve extracted radar PNGs (and allow clients to fetch them)
try:
    from radar import FRAMES_FOLDER  # type: ignore

    if os.path.isdir(FRAMES_FOLDER):
        app.mount("/radar/frames", StaticFiles(directory=FRAMES_FOLDER), name="radar_frames")
except Exception:
    # Best-effort: API still works without static mounting (e.g. missing folder on first boot)
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

_radar_state_lock = threading.Lock()
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
        # Walk recent pairs newest-first; use the first pair where at least one
        # blob-sized rain pixel exists in the earlier frame (same skip logic as
        # get_movement_vector). Avoids all-zero flow when the last pair is rain-free.
        patches_motion = []
        roi_mask = None
        try:
            roi_mask = build_ncr_roi_mask(radius_km=150.0)
            pairs = list(zip(recent_frame_data[:-1], recent_frame_data[1:]))
            for (p_prev, ts_prev), (p_last, ts_last) in reversed(pairs):
                from optical_flow import isolate_rain as _ir  # type: ignore
                if _ir(p_prev, clutter_mask=clutter_mask).max() == 0:
                    continue
                gap = 10.0
                if ts_prev and ts_last:
                    gap = max(1.0, (ts_last - ts_prev).total_seconds() / 60.0)
                patches_motion = compute_patch_motion(
                    p_prev, p_last, gap_mins=gap,
                    clutter_mask=clutter_mask, roi_mask=roi_mask, min_area_px=4,
                )
                print(f"  Per-patch: {len(patches_motion)} patch(es) detected (gap={gap:.0f}m)")
                break
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
def get_radar_gif():
    """
    Returns the latest downloaded Delhi radar GIF (if present).
    Use with /frames/latest to ensure the GIF/frames are refreshed when stale.
    """
    if not os.path.exists(GIF_SAVE_PATH):
        raise HTTPException(status_code=404, detail="Radar GIF not found on server yet.")
    return FileResponse(GIF_SAVE_PATH, media_type="image/gif", filename="delhi_radar.gif")


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

    Use-case:
      Frontend computes a road route geometry + ETAs from user-provided avg speed,
      then asks the backend to score rain intensity at those points/times.
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

        # Load radar state with TTL lazy-cache
        state = _load_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
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
            px, py = latlon_to_pixel(wp.lat, wp.lon)
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
        )

        for e, (px, py, _eta) in zip(enriched, waypoints_pixels):
            e["in_radar_bounds"] = is_within_radar(e["lat"], e["lon"])
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

    Reuses the same cached radar state as /predict_waypoints — no extra GIF download.
    Returns a list of rain events sorted by ETA, each with probability (0–100)
    derived from projected dBZ after patch decay.
    """
    try:
        if not (6 < req.lat < 38 and 68 < req.lon < 98):
            raise HTTPException(status_code=400, detail="Coordinates outside India bounds.")

        if not is_within_radar(req.lat, req.lon):
            return {
                "in_radar_bounds": False,
                "events": [],
                "summary": "Location is outside radar coverage area.",
                "radar_as_of": None,
                "lag_mins": None,
                "total_events": 0,
            }

        state = _load_radar_state(ttl_sec=RADAR_CACHE_TTL_SEC, force=False)
        dx, dy, _dir_from, _dir_to, _speed = state["movement"]
        latest_frame = state.get("latest_frame")
        lag_info = state.get("lag_info") or {}
        lag_mins = float(lag_info.get("lag_mins", 10.0))
        patch_tracks = state.get("decay_tracks") or []
        clutter_mask = state.get("clutter_mask")
        latest_ts = state.get("latest_ts")

        if not latest_frame:
            raise HTTPException(status_code=503, detail="Radar data not yet loaded. Try again in a moment.")

        rain_mask = isolate_rain(latest_frame, clutter_mask=clutter_mask)
        rgb_arr = np.array(Image.open(latest_frame).convert("RGB"))

        user_px, user_py = latlon_to_pixel(req.lat, req.lon)

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
        )

        as_of = latest_ts.strftime("%H:%M IST") if latest_ts else "unknown"

        first_rain = next((s for s in slots if s["has_rain"]), None)
        if not first_rain:
            summary = "Clear skies for the next 2 hours"
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
