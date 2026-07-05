import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import cv2
import requests
from PIL import Image
from optical_flow import isolate_rain

logger = logging.getLogger(__name__)

# Public OSRM demo server — rate-limited; use self-hosted OSRM in production.
OSRM_ROUTE_URL = "http://router.project-osrm.org/route/v1/driving/{coords}"
OSRM_REQUEST_TIMEOUT_SEC = 12.0

DRIVING_SPEED_KMH = 40.0  # legacy fallback: city driving speed for Delhi NCR


def debug_route_on_image(frames, waypoints_pixels, clutter_mask):
    """
    Temporary debug: Prints the raw pixel color at each waypoint
    to see what's actually under the hood.
    """
    img = Image.open(frames[-1]).convert('RGB')
    arr = np.array(img)

    print("\nDEBUG: Pixel colors at each waypoint:")
    for i, (px, py, eta) in enumerate(waypoints_pixels):
        # Clip coordinates to image bounds
        h, w = arr.shape[:2]
        safe_y = max(0, min(py, h - 1))
        safe_x = max(0, min(px, w - 1))

        r, g, b = arr[safe_y, safe_x]

        # Simple classification heuristics
        is_green = (70 < r < 180) and (100 < g < 200) and (40 < b < 100)
        is_blue = (b > 150) and (r < 100)
        is_clutter = clutter_mask[safe_y, safe_x] > 0

        label = "GREEN(background)" if is_green else \
                "BLUE(rain)" if is_blue else \
                "OTHER"
        clutter_status = "CLUTTER" if is_clutter else "clean"

        print(f"  WP{i+1:02d} pixel({px:3d},{py:3d}) RGB({r:3d},{g:3d},{b:3d}) -> {label} | {clutter_status}")


def haversine_km(lat1, lon1, lat2, lon2):
    """
    Returns great-circle distance in km between two lat/lon points.
    """
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    return haversine_km(lat1, lon1, lat2, lon2) * 1000.0


def decode_polyline(polyline_str: str, precision: int = 5) -> List[Tuple[float, float]]:
    """
    Decode an OSRM/Google encoded polyline to [(lon, lat), ...].
    """
    if not polyline_str:
        return []
    index = 0
    lat = 0
    lng = 0
    coordinates: List[Tuple[float, float]] = []
    factor = float(10 ** precision)
    length = len(polyline_str)

    while index < length:
        # latitude
        shift = 0
        result = 0
        while True:
            if index >= length:
                return coordinates
            b = ord(polyline_str[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        # longitude
        shift = 0
        result = 0
        while True:
            if index >= length:
                return coordinates
            b = ord(polyline_str[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        coordinates.append((lng / factor, lat / factor))

    return coordinates


def _fetch_osrm_route_json(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> Optional[Dict[str, Any]]:
    """
    Request route from public OSRM with full geometry and steps (per-segment duration/distance).
    Returns parsed JSON dict or None on failure.
    """
    # OSRM expects lon,lat order in the URL
    coords = f"{start_lon},{start_lat};{end_lon},{end_lat}"
    url = OSRM_ROUTE_URL.format(coords=coords)
    params = {
        "overview": "full",
        "steps": "true",
        "geometries": "polyline",
    }
    try:
        resp = requests.get(
            url,
            params=params,
            timeout=OSRM_REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        logger.warning("OSRM request failed: %s", e)
        return None

    if resp.status_code >= 400:
        logger.warning("OSRM HTTP %s: %s", resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
    except ValueError as e:
        logger.warning("OSRM JSON parse error: %s", e)
        return None

    if data.get("code") != "Ok" or not data.get("routes"):
        logger.warning("OSRM bad response code=%s routes=%s", data.get("code"), bool(data.get("routes")))
        return None

    return data


def _timed_points_from_steps(steps: List[Dict[str, Any]], route_duration_sec: float) -> List[Tuple[float, float, float]]:
    """
    Build dense (lat, lon, time_sec) along the route using each step's duration
    and decoded step geometry. Time increases linearly with distance along each step's polyline.
    """
    timed: List[Tuple[float, float, float]] = []
    step_start_sec = 0.0

    for step in steps:
        geom = step.get("geometry")
        if not geom:
            continue
        dur = float(step.get("duration", 0.0))
        coords = decode_polyline(geom)
        if not coords:
            continue

        # segment lengths in meters along decoded vertices
        seg_lens: List[float] = []
        for i in range(len(coords) - 1):
            lon1, lat1 = coords[i]
            lon2, lat2 = coords[i + 1]
            seg_lens.append(haversine_m(lat1, lon1, lat2, lon2))
        total_len = sum(seg_lens)

        if len(coords) == 1:
            lon, lat = coords[0]
            timed.append((lat, lon, step_start_sec))
            step_start_sec += dur
            continue

        if total_len < 1e-3:
            # Degenerate: stack all points at same time gradient — advance full duration at last point
            for i, (lon, lat) in enumerate(coords):
                frac = i / max(1, len(coords) - 1)
                t = step_start_sec + frac * dur
                timed.append((lat, lon, t))
            step_start_sec += dur
            continue

        cum_m = 0.0
        for i, (lon, lat) in enumerate(coords):
            if i == 0:
                t = step_start_sec
            else:
                cum_m += seg_lens[i - 1]
                frac = cum_m / total_len
                t = step_start_sec + frac * dur
            timed.append((lat, lon, t))

        step_start_sec += dur

    # If steps produced nothing usable, return empty
    if not timed:
        return []

    # Snap last point to route total duration if OSRM step sum drifted slightly
    expected_end = float(route_duration_sec)
    if timed[-1][2] < expected_end - 1.0 or timed[-1][2] > expected_end + 1.0:
        lat, lon, _ = timed[-1]
        timed[-1] = (lat, lon, expected_end)
    return timed


def _timed_points_from_route_geometry(route: Dict[str, Any]) -> List[Tuple[float, float, float]]:
    """
    Fallback: single encoded geometry for whole route + linear time by accumulated path length.
    """
    geom = route.get("geometry")
    if not geom:
        return []
    dur = float(route.get("duration", 0.0))
    coords = decode_polyline(geom)
    if not coords:
        return []

    seg_lens: List[float] = []
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        seg_lens.append(haversine_m(lat1, lon1, lat2, lon2))
    total_len = sum(seg_lens) or 1e-6

    timed: List[Tuple[float, float, float]] = []
    cum_m = 0.0
    for i, (lon, lat) in enumerate(coords):
        if i == 0:
            t = 0.0
        else:
            cum_m += seg_lens[i - 1]
            t = dur * (cum_m / total_len)
        timed.append((lat, lon, t))
    return timed


def _merge_duplicate_vertices(timed: List[Tuple[float, float, float]]) -> List[Tuple[float, float, float]]:
    """Drop consecutive duplicate (lat,lon) keeping the later timestamp."""
    if not timed:
        return []
    out: List[Tuple[float, float, float]] = [timed[0]]
    for lat, lon, t in timed[1:]:
        plat, plon, _ = out[-1]
        if abs(lat - plat) < 1e-9 and abs(lon - plon) < 1e-9:
            out[-1] = (lat, lon, max(out[-1][2], t))
        else:
            out.append((lat, lon, t))
    return out


def interpolate_latlon_at_time(
    timed_points: List[Tuple[float, float, float]],
    t_sec: float,
) -> Tuple[float, float]:
    """
    Piecewise linear interpolation of (lat, lon) over time (seconds along route).
    """
    if not timed_points:
        raise ValueError("empty timed_points")
    t_sec = max(0.0, min(t_sec, timed_points[-1][2]))
    if t_sec <= timed_points[0][2]:
        return timed_points[0][0], timed_points[0][1]

    for i in range(1, len(timed_points)):
        lat1, lon1, t1 = timed_points[i - 1]
        lat2, lon2, t2 = timed_points[i]
        if t_sec <= t2 or i == len(timed_points) - 1:
            span = t2 - t1
            if span <= 1e-9:
                return lat2, lon2
            u = (t_sec - t1) / span
            u = max(0.0, min(1.0, u))
            return lat1 + u * (lat2 - lat1), lon1 + u * (lon2 - lon1)
    return timed_points[-1][0], timed_points[-1][1]


def resample_route_by_driving_time(
    timed_points: List[Tuple[float, float, float]],
    interval_minutes: float,
) -> List[Dict[str, float]]:
    """
    Resample a time-tagged polyline so waypoints are evenly spaced in *driving time*
    (e.g. one point every `interval_minutes`).
    """
    if not timed_points:
        return []
    if interval_minutes <= 0:
        interval_minutes = 2.0

    timed_points = sorted(timed_points, key=lambda x: x[2])
    timed_points = _merge_duplicate_vertices(timed_points)

    max_t = timed_points[-1][2]
    if max_t <= 0:
        lat, lon, _ = timed_points[-1]
        return [{"lat": lat, "lon": lon, "eta_minutes": 0.0}]

    interval_sec = interval_minutes * 60.0
    targets: List[float] = []
    t = 0.0
    while t <= max_t + 1e-6:
        targets.append(t)
        t += interval_sec

    if not targets or abs(targets[-1] - max_t) > 1e-2:
        targets.append(max_t)

    out: List[Dict[str, float]] = []
    seen = set()
    for tgt in targets:
        key = round(tgt, 3)
        if key in seen and tgt < max_t - 1e-6:
            continue
        seen.add(key)
        lat, lon = interpolate_latlon_at_time(timed_points, tgt)
        out.append({
            "lat": float(lat),
            "lon": float(lon),
            "eta_minutes": float(tgt / 60.0),
        })
    return out


def route_waypoints_osrm(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    eta_spacing_minutes: float = 2.0,
) -> Optional[List[Dict[str, float]]]:
    """
    Full OSRM pipeline: fetch route, decode geometry, assign cumulative time from OSRM
    durations, resample by driving time. Returns None if routing fails.
    """
    data = _fetch_osrm_route_json(start_lat, start_lon, end_lat, end_lon)
    if not data:
        return None

    route = data["routes"][0]
    route_duration_sec = float(route.get("duration", 0.0))

    legs = route.get("legs") or []
    steps: List[Dict[str, Any]] = []
    for leg in legs:
        steps.extend(leg.get("steps") or [])

    if steps:
        timed = _timed_points_from_steps(steps, route_duration_sec)
    else:
        timed = _timed_points_from_route_geometry(route)

    if not timed:
        return None

    return resample_route_by_driving_time(timed, eta_spacing_minutes)


def generate_waypoints_straight_line(
    start_latlon: Tuple[float, float],
    end_latlon: Tuple[float, float],
    spacing_km: float = 2.0,
) -> List[Dict[str, float]]:
    """
    Legacy: great-circle segments at fixed spacing_km with fixed DRIVING_SPEED_KMH.
    Returns list of dicts with lat, lon, eta_minutes.
    """
    lat1, lon1 = start_latlon
    lat2, lon2 = end_latlon
    total_km = haversine_km(lat1, lon1, lat2, lon2)

    if total_km == 0:
        return [{"lat": lat1, "lon": lon1, "eta_minutes": 0.0}]

    num_segments = max(1, int(math.ceil(total_km / spacing_km)))
    out: List[Dict[str, float]] = []
    for i in range(num_segments + 1):
        t = i / num_segments
        lat = lat1 + t * (lat2 - lat1)
        lon = lon1 + t * (lon2 - lon1)
        dist_from_start = t * total_km
        eta_mins = (dist_from_start / DRIVING_SPEED_KMH) * 60.0
        out.append({"lat": lat, "lon": lon, "eta_minutes": eta_mins})
    return out


def generate_route_waypoints_dicts(
    start_latlon: Tuple[float, float],
    end_latlon: Tuple[float, float],
    *,
    spacing_km: float = 2.0,
    eta_spacing_minutes: float = 2.0,
    use_osrm: bool = True,
) -> List[Dict[str, float]]:
    """
    Prefer OSRM road geometry + OSRM durations, resampled every eta_spacing_minutes.
    On failure, fall back to straight-line waypoints every spacing_km at 40 km/h.
    """
    lat1, lon1 = start_latlon
    lat2, lon2 = end_latlon

    if use_osrm:
        routed = route_waypoints_osrm(lat1, lon1, lat2, lon2, eta_spacing_minutes=eta_spacing_minutes)
        if routed is not None:
            return routed
        logger.info("OSRM unavailable; using straight-line fallback")

    return generate_waypoints_straight_line(start_latlon, end_latlon, spacing_km=spacing_km)


def generate_waypoints(
    start_latlon,
    end_latlon,
    spacing_km=2.0,
    eta_spacing_minutes: float = 2.0,
    use_osrm: bool = True,
):
    """
    Generate waypoints between start and end.

    Primary: OSRM driving route with geometry + step durations, resampled every
    ``eta_spacing_minutes`` of driving time.

    Fallback: evenly spaced along a straight line every ``spacing_km`` at
    DRIVING_SPEED_KMH (40 km/h) if OSRM fails.

    Returns:
        list of (lat, lon, eta_mins) tuples (backward compatible with check_route_rain / API).
    """
    dicts = generate_route_waypoints_dicts(
        start_latlon,
        end_latlon,
        spacing_km=spacing_km,
        eta_spacing_minutes=eta_spacing_minutes,
        use_osrm=use_osrm,
    )
    return [(d["lat"], d["lon"], d["eta_minutes"]) for d in dicts]


def predict_rain_position(latest_frame_path, dx, dy, minutes_ahead,
                          clutter_mask=None, base_rain_mask=None):
    """
    Predicts where rain will be after minutes_ahead minutes.

    Shifts the current rain mask forward using the optical flow vector.
    Each 10-minute frame corresponds to one (dx, dy) movement unit.
    Clutter pixels are removed before shifting if clutter_mask is provided.

    For per-request perf: if ``base_rain_mask`` is supplied we skip
    ``isolate_rain`` (which re-opens the PIL image). The caller is expected
    to compute the base mask once and pass it for every time offset.

    Returns: predicted rain mask (numpy array, uint8, 0=no rain, 255=rain)
    """
    if base_rain_mask is None:
        rain_mask = isolate_rain(latest_frame_path, clutter_mask=clutter_mask)
    else:
        rain_mask = base_rain_mask
    rain_mask = rain_mask.astype(np.float32)

    frames_ahead = minutes_ahead / 10.0
    shift_x = dx * frames_ahead
    shift_y = dy * frames_ahead

    h, w = rain_mask.shape
    M = np.float32([[1, 0, shift_x],
                    [0, 1, shift_y]])
    shifted = cv2.warpAffine(rain_mask, M, (w, h), flags=cv2.INTER_LINEAR)

    return (shifted > 127).astype(np.uint8) * 255


def is_rain_at_pixel(predicted_mask, px, py, radius=1):
    """
    Checks if predicted rain exists at or near pixel (px, py).
    Uses a radius of 1 pixel (3x3 grid) to account for minor error.
    Returns True if rain detected, False otherwise.
    """
    h, w = predicted_mask.shape
    # Ensure coordinates are within bounds
    py = max(0, min(py, h - 1))
    px = max(0, min(px, w - 1))

    # BUG 4: Check center pixel first
    if predicted_mask[py, px] > 0:
        return True

    # Check radius only if center is not rain
    x0 = max(0, px - radius)
    x1 = min(w, px + radius + 1)
    y0 = max(0, py - radius)
    y1 = min(h, py + radius + 1)

    region = predicted_mask[y0:y1, x0:x1]
    return bool(np.any(region > 0))


def _mask_hit_no_clamp(mask, px, py, radius=1):
    """
    Neighborhood rain check that treats out-of-frame source pixels as a miss
    (no clamping — a back-projected point outside the frame means the rain
    that would arrive there is beyond radar knowledge, not at the edge).
    """
    h, w = mask.shape[:2]
    x0 = max(0, px - radius)
    x1 = min(w, px + radius + 1)
    y0 = max(0, py - radius)
    y1 = min(h, py + radius + 1)
    if x0 >= x1 or y0 >= y1:
        return False
    return bool(np.any(mask[y0:y1, x0:x1]))


def check_route_rain(waypoints_pixels, dx, dy, latest_frame_path,
                     eta_minutes, clutter_mask=None, lag_mins=25.0,
                     frame_rgb=None, base_rain_mask=None, patches=None):
    """
    Checks each waypoint on a route for expected rain at its ETA,
    adjusted for radar lag.

    Args:
        waypoints_pixels   : list of (px, py, eta_mins) tuples
        dx, dy             : GLOBAL rain movement vector (pixels per 10-min frame)
        latest_frame_path  : path to the most recent radar frame
        eta_minutes        : total trip duration (kept for API clarity)
        clutter_mask       : optional clutter mask to clean predictions
        lag_mins           : minutes elapsed since the radar frame was last updated
        frame_rgb          : optional pre-loaded RGB numpy array of the latest frame
                             (avoids re-opening the PIL image)
        base_rain_mask     : optional pre-computed rain mask for latest_frame
                             (avoids re-running ``isolate_rain`` per time offset)
        patches            : optional list of patch dicts from compute_patch_motion
                             (each with 'mask', 'dx_10', 'dy_10'). When provided,
                             each patch is advected by its OWN motion vector
                             instead of shifting the whole field by the global
                             average — the global average of divergent monsoon
                             cells is often near-zero, which made forecasts look
                             like a static snapshot of current weather.

    For each waypoint, checks rain at (effective_eta), (effective_eta+5),
    and (effective_eta+10) minutes.
    """
    # Compute base rain mask ONCE per request; all time offsets reuse it.
    if base_rain_mask is None:
        base_rain_mask = isolate_rain(latest_frame_path, clutter_mask=clutter_mask)

    # --- Per-patch advection setup ---
    # A waypoint is rainy at time t iff some patch, moved by its own velocity
    # for t minutes, covers the waypoint. Equivalent (and cheaper) test:
    # back-project the waypoint by that patch's vector and check the patch's
    # CURRENT mask. Rain pixels not claimed by any patch (tiny components)
    # fall back to the global vector against the leftover mask.
    patch_list = []
    leftover_mask = None
    if patches:
        from bbox_mask import as_bbox_mask
        for p in patches:
            m = p.get("mask")
            if m is None:
                continue
            patch_list.append((float(p.get("dx_10", 0.0)),
                               float(p.get("dy_10", 0.0)),
                               as_bbox_mask(m)))
        if patch_list:
            union = np.zeros(base_rain_mask.shape[:2], dtype=bool)
            for _pdx, _pdy, bm in patch_list:
                bm.paint_into(union)
            leftover_mask = (base_rain_mask > 0) & ~union

    def rain_source_at(px, py, t_mins):
        """Where is the rain NOW that will be over (px,py) at t_mins? -> (hit, sx, sy)."""
        shifts = t_mins / 10.0
        for pdx, pdy, pmask in patch_list:
            sx = int(round(px - pdx * shifts))
            sy = int(round(py - pdy * shifts))
            if pmask.hit(sx, sy, radius=1):
                return True, sx, sy
        sx = int(round(px - dx * shifts))
        sy = int(round(py - dy * shifts))
        fb = leftover_mask if leftover_mask is not None else (base_rain_mask > 0)
        if _mask_hit_no_clamp(fb, sx, sy, radius=1):
            return True, sx, sy
        return False, None, None

    # Load latest frame to check if waypoints start as green/background
    if frame_rgb is not None:
        img_arr = frame_rgb
    else:
        try:
            img_arr = np.array(Image.open(latest_frame_path).convert('RGB'))
        except Exception:
            img_arr = None

    results = []
    for i, (px, py, eta) in enumerate(waypoints_pixels):
        # Clamp coordinates once up-front. Some (lat,lon)->pixel mappings near
        # the radar edge can round slightly outside bounds; numpy negative
        # indices also wrap around silently, causing "missed rain" artifacts.
        try:
            mh, mw = base_rain_mask.shape[:2]
        except Exception:
            mh, mw = None, None
        if mw is not None and mh is not None:
            px = int(max(0, min(int(px), int(mw) - 1)))
            py = int(max(0, min(int(py), int(mh) - 1)))
        else:
            px = int(px)
            py = int(py)

        effective_eta = eta + lag_mins

        # Buffer: check effective_eta, +5, +10
        check_times = [effective_eta, effective_eta + 5, effective_eta + 10]
        hits = 0

        # Check if center pixel of the ORIGINAL frame is green (background)
        is_center_green = False
        if img_arr is not None:
            # Clamp coordinates for safety
            h, w = img_arr.shape[:2]
            spy = max(0, min(py, h - 1))
            spx = max(0, min(px, w - 1))
            r, g, b = img_arr[spy, spx]
            is_center_green = (70 < r < 180) and (100 < g < 200) and (40 < b < 100)

        src_px = src_py = None
        for t in check_times:
            # Always use a small neighborhood check; being strict on "green"
            # backgrounds causes false negatives when the georef rounds a
            # waypoint onto an adjacent green pixel near a rain edge.
            hit, sx, sy = rain_source_at(px, py, max(0.0, t))
            if hit:
                hits += 1
                if src_px is None:
                    src_px, src_py = sx, sy

        if hits == 3:
            confidence = "high"
        elif hits == 2:
            confidence = "medium"
        elif hits == 1:
            confidence = "low"
        else:
            confidence = "none"

        # Long range confidence nerf
        if eta > 60 and confidence != "none":
            confidence = "low"

        results.append({
            "px": px,
            "py": py,
            "eta_mins": eta,
            "effective_eta": effective_eta,
            "rain_expected": hits > 0,
            "confidence": confidence,
            "lag_applied": lag_mins,
            # Pixel in the LATEST frame holding the rain that arrives at this
            # waypoint at its ETA (per-patch back-projection). Lets enrichment
            # sample intensity from the correct source cell.
            "src_px": src_px,
            "src_py": src_py,
        })

    return results


if __name__ == "__main__":
    from radar import get_recent_frames, get_all_frames
    from optical_flow import get_movement_vector, build_clutter_mask
    from georef import latlon_to_pixel, is_within_radar

    # Setup - run once for both routes
    print("Setting up radar data...")
    all_frame_data = get_all_frames()
    recent_frame_data = get_recent_frames(n=6)
    all_paths = [p for (p, _ts) in all_frame_data]
    clutter_mask = build_clutter_mask(all_paths)
    dx, dy, dir_from, dir_to, speed = get_movement_vector(
        recent_frame_data, clutter_mask=clutter_mask
    )
    latest_frame = recent_frame_data[-1][0]
    print(f"Rain moving FROM {dir_from} -> TO {dir_to} at {speed:.1f} km/h")
    print()

    # Define both routes
    routes = [
        {
            "name": "Ghaziabad -> Jind",
            "start": (28.6692, 77.4538),  # Ghaziabad
            "end":   (29.3162, 76.3148),  # Jind
        },
        {
            "name": "Etah -> Sambhal",
            "start": (27.5590, 78.6624),  # Etah
            "end":   (28.5836, 78.5685),  # Sambhal
        },
    ]

    # Run prediction for each route
    for route in routes:
        print("=" * 50)
        print(f"ROUTE: {route['name']}")
        print("=" * 50)

        # Check bounds first
        start_in = is_within_radar(route['start'][0], route['start'][1])
        end_in   = is_within_radar(route['end'][0],   route['end'][1])
        print(f"Start in radar bounds: {start_in}")
        print(f"End   in radar bounds: {end_in}")

        if not start_in or not end_in:
            print("WARNING: Part of route outside Delhi DWR coverage!")
            print("   Predictions for out-of-bounds waypoints unreliable!")
            print()

        # Generate waypoints (OSRM + 2-min spacing, or fallback)
        waypoints_latlon = generate_waypoints(
            route['start'],
            route['end'],
            spacing_km=2.0,
            eta_spacing_minutes=2.0,
        )

        total_dist = haversine_km(
            route['start'][0], route['start'][1],
            route['end'][0],   route['end'][1]
        )
        print(f"Great-circle distance: {total_dist:.1f} km")
        print(f"Waypoints: {len(waypoints_latlon)}")
        print()

        # Convert to pixels
        waypoints_pixels = []
        for lat, lon, eta in waypoints_latlon:
            px, py = latlon_to_pixel(lat, lon)
            in_bounds = is_within_radar(lat, lon)
            waypoints_pixels.append((px, py, eta, in_bounds))

        # Run rain check
        max_eta = waypoints_latlon[-1][2]
        results = check_route_rain(
            [(px, py, eta) for px, py, eta, _ in waypoints_pixels],
            dx, dy,
            latest_frame,
            eta_minutes=max_eta,
            clutter_mask=clutter_mask
        )

        # Print results
        print(f"{'WP':<4} {'Lat':>8} {'Lon':>8} {'ETA':>6} {'Status':<6} {'Conf':<8} {'Note'}")
        print("-" * 65)
        for i, (r, (lat, lon, eta)) in enumerate(
            zip(results, waypoints_latlon)
        ):
            in_bounds = is_within_radar(lat, lon)
            status = "RAIN" if r["rain_expected"] else "CLEAR"
            conf   = r["confidence"]
            note   = ""
            if not in_bounds:
                note = "out of bounds"
            elif eta > 60:
                note = "long range"
            print(f"WP{i+1:02d} {lat:>8.4f} {lon:>8.4f} {eta:>5.0f}m {status:<6s} {conf:<8} {note}")

        print()
