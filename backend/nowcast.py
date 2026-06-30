"""
nowcast.py — Point-based rain arrival prediction for a fixed location.

Algorithm
---------
For each time step T (0 → 120 min, resolution 2 min):
  1. Back-project the user's pixel to the latest radar frame:
         orig_px = user_px - dx * (T + lag_mins) / 10
         orig_py = user_py - dy * (T + lag_mins) / 10
  2. Check whether (orig_px, orig_py) falls within the latest rain mask.
     This naturally catches brand-new pop-up patches in the latest frame.
  3. Identify contiguous rain windows → NowcastEvent.
  4. For each event, look up the nearest decay track to get projected dBZ.
     Convert projected dBZ → rain probability (dying patch → low probability).

Why back-projection works for pop-ups
--------------------------------------
New patches that appeared only in the latest frame have no decay history
(decay_rate=0, stable assumption). They ARE in the latest rain mask so they
are caught by the raw mask scan at T where back-projected pixel hits them.
The global optical flow (dx, dy) is used as their movement vector.

Priority of recent frames
--------------------------
The dx, dy passed in come from get_movement_vector() which weights recent
frame pairs linearly higher (weight = pair index + 1). No extra work needed.

Deduplication
-------------
Byte-identical GIF frames are already dropped by extract_frames() in radar.py
before any of this runs.
"""

from __future__ import annotations

import math
import numpy as np
from dataclasses import dataclass
from typing import List, Optional

# Temporal resolution of the nowcast scan
TIME_STEP_MINS: int = 2

# Search radius around user's pixel (accounts for minor georef error + patch edges)
LOCATION_RADIUS_PX: int = 5

# Maximum prediction horizon
MAX_NOWCAST_MINS: int = 120

# Minimum probability to include an event (below this we treat rain as not coming)
MIN_PROBABILITY: int = 8

# Patch-forward: search circle radius around user (pixels)
PATCH_SEARCH_RADIUS_PX: int = 120

# Patches with |dx_10| < this AND |dy_10| < this are fresh pop-ups — skip forward projection
FRESH_POPUP_VELOCITY_THRESH: float = 0.3


@dataclass
class NowcastEvent:
    eta_mins: float        # minutes until this rain wave reaches the user
    duration_mins: float   # estimated duration at this location
    probability: int       # 0–100, derived from projected dBZ after decay
    intensity: str         # IMD label at projected dBZ (e.g. "Light Rain")
    projected_dbz: float   # estimated dBZ when the rain arrives (post-decay)
    raw_dbz: float         # current dBZ of the source patch in the latest frame
    decay_status: str      # stable | weakening | dying | dead


# ── dBZ → probability ────────────────────────────────────────────────────────

def dbz_to_probability(projected_dbz: float) -> int:
    """
    Map projected dBZ to rain probability (0–100).

    Using projected dBZ (which already bakes in decay) means a weakening patch
    naturally yields lower probability without any extra logic.
    """
    d = max(0.0, float(projected_dbz))
    if d <  5:  return 0
    if d < 10:  return 8
    if d < 15:  return 20
    if d < 20:  return 35
    if d < 25:  return 50
    if d < 30:  return 64
    if d < 35:  return 76
    if d < 40:  return 85
    if d < 45:  return 92
    if d < 50:  return 96
    return 99


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_rain_near(rain_mask: np.ndarray, fx: float, fy: float, radius: int) -> bool:
    """True if any pixel within `radius` of (fx, fy) is rain."""
    h, w = rain_mask.shape
    x0 = max(0, int(fx) - radius)
    x1 = min(w - 1, int(fx) + radius)
    y0 = max(0, int(fy) - radius)
    y1 = min(h - 1, int(fy) + radius)
    if x1 < x0 or y1 < y0:
        return False
    return bool(rain_mask[y0:y1 + 1, x0:x1 + 1].any())


def _find_track_for_patch(patch: dict, patch_tracks) -> Optional[object]:
    """Return the PatchTrack whose latest-frame centroid is nearest to this patch's centroid."""
    MATCH_RADIUS_PX = 35
    cx, cy = patch["centroid_px"]
    best_track, best_dist = None, float(MATCH_RADIUS_PX)
    for track in (patch_tracks or []):
        if not track.centroids:
            continue
        tcx, tcy = track.centroids[-1]
        dist = math.sqrt((tcx - cx) ** 2 + (tcy - cy) ** 2)
        if dist < best_dist:
            best_dist = dist
            best_track = track
    return best_track


def _find_patch_track(orig_px: float, orig_py: float, patch_tracks) -> Optional[object]:
    """Return the PatchTrack whose latest-frame mask contains (orig_px, orig_py)."""
    for track in (patch_tracks or []):
        if track.mask_latest is None:
            continue
        h, w = track.mask_latest.shape
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                ny = int(orig_py) + dy
                nx = int(orig_px) + dx
                if 0 <= ny < h and 0 <= nx < w and track.mask_latest[ny, nx]:
                    return track
    return None


def _sample_raw_dbz(
    rain_mask: np.ndarray,
    rgb_arr: np.ndarray,
    fx: float,
    fy: float,
    radius: int,
) -> float:
    """Sample mean dBZ directly from the RGB radar frame near (fx, fy)."""
    from fuzzy import rgb_to_dbz  # type: ignore
    h, w = rain_mask.shape
    dbzs = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            ny, nx = int(fy) + dy, int(fx) + dx
            if 0 <= ny < h and 0 <= nx < w and rain_mask[ny, nx]:
                d = rgb_to_dbz(
                    int(rgb_arr[ny, nx, 0]),
                    int(rgb_arr[ny, nx, 1]),
                    int(rgb_arr[ny, nx, 2]),
                )
                if d > 0:
                    dbzs.append(d)
    return float(np.mean(dbzs)) if dbzs else 0.0


def _build_event(
    rain_start: float,
    rain_end: float,
    hit_px: float,
    hit_py: float,
    lag_mins: float,
    patch_tracks,
    rain_mask: np.ndarray,
    rgb_arr: np.ndarray,
    radius: int,
) -> Optional[NowcastEvent]:
    """Build a NowcastEvent for a detected rain window [rain_start, rain_end)."""
    from decay import project_dbz, _classify  # type: ignore
    from fuzzy import dbz_to_label            # type: ignore

    eta = rain_start
    duration = max(2.0, rain_end - rain_start)

    track = _find_patch_track(hit_px, hit_py, patch_tracks)
    if track:
        proj_dbz = max(0.0, project_dbz(track, eta + lag_mins))
        decay_status = _classify(proj_dbz, track.decay_rate)
        raw_dbz = track.dbz_latest
    else:
        # New pop-up: no decay history → use raw dBZ, assume stable
        raw_dbz = _sample_raw_dbz(rain_mask, rgb_arr, hit_px, hit_py, radius)
        proj_dbz = raw_dbz
        decay_status = "stable"

    prob = dbz_to_probability(proj_dbz)
    if prob < MIN_PROBABILITY:
        return None

    intensity_label, _ = dbz_to_label(max(0.0, proj_dbz))

    return NowcastEvent(
        eta_mins=float(eta),
        duration_mins=float(duration),
        probability=prob,
        intensity=intensity_label,
        projected_dbz=round(proj_dbz, 1),
        raw_dbz=round(raw_dbz, 1),
        decay_status=decay_status,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

SLOT_INTERVAL_MINS: int = 15
NUM_SLOTS: int = 8  # 8 × 15 min = 120 min horizon


def compute_nowcast_slots(
    user_px: float,
    user_py: float,
    dx: float,
    dy: float,
    rain_mask: np.ndarray,
    rgb_arr: np.ndarray,
    patch_tracks,
    lag_mins: float = 0.0,
    num_slots: int = NUM_SLOTS,
    slot_interval: int = SLOT_INTERVAL_MINS,
    radius: int = LOCATION_RADIUS_PX,
    patches_motion: list = None,
    search_radius_px: int = PATCH_SEARCH_RADIUS_PX,
) -> list:
    """
    Return 8 discrete 15-min checkpoint predictions (0, 15, 30 … 105 min).

    Each slot:
      slot_mins    — minutes from now
      has_rain     — bool
      probability  — 0-100
      intensity    — IMD label or "No Rain"
      projected_dbz
      decay_status
    """
    from decay import project_dbz, _classify  # type: ignore
    from fuzzy import dbz_to_label            # type: ignore

    # Pre-sort patches by current distance from user (closest first, checked first per slot)
    _sorted_patches = sorted(
        patches_motion or [],
        key=lambda p: (p["centroid_px"][0] - user_px) ** 2 + (p["centroid_px"][1] - user_py) ** 2,
    )

    slots = []
    for i in range(num_slots):
        t = float(i * slot_interval)
        eff = t + lag_mins
        shifts = eff / 10.0

        has_rain = False
        proj_dbz = 0.0
        decay_status = "stable"
        patch_hit = None

        # ── 1. PATCH-FORWARD PASS ─────────────────────────────────────────────
        # For each patch in the search circle, forward-project its centroid using
        # its own velocity and check if it reaches the user at this slot's time.
        for patch in _sorted_patches:
            cx, cy = patch["centroid_px"]
            vx = patch.get("dx_10", 0.0)
            vy = patch.get("dy_10", 0.0)

            # Only consider patches currently inside the search circle
            if math.sqrt((cx - user_px) ** 2 + (cy - user_py) ** 2) > search_radius_px:
                continue

            # Skip fresh pop-ups — no reliable direction, handled by fallback below
            if abs(vx) < FRESH_POPUP_VELOCITY_THRESH and abs(vy) < FRESH_POPUP_VELOCITY_THRESH:
                continue

            # Where will this patch be at time `eff`?
            pred_cx = cx + vx * shifts
            pred_cy = cy + vy * shifts

            # Hit test: patch edge (from area) + user location tolerance
            patch_radius = max(5, int(math.sqrt(patch.get("area_px", 25) / math.pi)))
            dist = math.sqrt((pred_cx - user_px) ** 2 + (pred_cy - user_py) ** 2)
            if dist <= (patch_radius + radius):
                has_rain = True
                patch_hit = patch
                break  # closest patch wins for this slot

        # ── 2. GLOBAL-FLOW FALLBACK ───────────────────────────────────────────
        # Catches diffuse rain, fresh pop-ups, and cases with no tracked patches.
        orig_px = orig_py = None
        if not has_rain:
            orig_px = user_px - dx * eff / 10.0
            orig_py = user_py - dy * eff / 10.0
            has_rain = _is_rain_near(rain_mask, orig_px, orig_py, radius)

        # ── 3. dBZ / decay lookup ─────────────────────────────────────────────
        if has_rain and patch_hit is not None:
            track = _find_track_for_patch(patch_hit, patch_tracks)
            if track:
                proj_dbz = max(0.0, project_dbz(track, eff))
                decay_status = _classify(proj_dbz, track.decay_rate)
            else:
                proj_dbz = float(patch_hit.get("max_dbz", 0))
                decay_status = "stable"
        elif has_rain:
            track = _find_patch_track(orig_px, orig_py, patch_tracks)
            if track:
                proj_dbz = max(0.0, project_dbz(track, eff))
                decay_status = _classify(proj_dbz, track.decay_rate)
            else:
                proj_dbz = _sample_raw_dbz(rain_mask, rgb_arr, orig_px, orig_py, radius)
                decay_status = "stable"

        prob = dbz_to_probability(proj_dbz) if has_rain else 0
        if prob < MIN_PROBABILITY:
            has_rain = False
            proj_dbz = 0.0
            prob = 0

        intensity_label = "No Rain"
        if has_rain:
            intensity_label, _ = dbz_to_label(max(0.0, proj_dbz))

        slots.append({
            "slot_mins": t,
            "has_rain": has_rain,
            "probability": prob,
            "intensity": intensity_label,
            "projected_dbz": round(proj_dbz, 1),
            "decay_status": decay_status,
        })

    return slots


def compute_nowcast(
    user_px: float,
    user_py: float,
    dx: float,
    dy: float,
    rain_mask: np.ndarray,
    rgb_arr: np.ndarray,
    patch_tracks,
    lag_mins: float = 0.0,
    max_mins: int = MAX_NOWCAST_MINS,
    radius: int = LOCATION_RADIUS_PX,
) -> List[NowcastEvent]:
    """
    Predict rain events at pixel (user_px, user_py) within the next max_mins minutes.

    Parameters
    ----------
    user_px, user_py : pixel coordinates of the user's location in radar frame
    dx, dy           : global optical-flow movement (pixels per 10-min frame)
    rain_mask        : latest-frame binary rain mask (uint8, 255=rain)
    rgb_arr          : latest-frame RGB array for dBZ sampling
    patch_tracks     : list of PatchTrack objects from compute_decay_tracks()
    lag_mins         : radar staleness (minutes since the latest frame was captured)
    max_mins         : prediction horizon (default 120 min)
    radius           : search radius in pixels around user location

    Returns
    -------
    List of NowcastEvent sorted by eta_mins, filtered to probability >= MIN_PROBABILITY
    """
    events: List[NowcastEvent] = []
    in_rain = False
    rain_start: Optional[float] = None
    hit_orig_px: Optional[float] = None
    hit_orig_py: Optional[float] = None

    for t in range(0, max_mins + 1, TIME_STEP_MINS):
        # Account for radar staleness: the patch has already moved `lag_mins`
        # beyond where it appears in the latest frame.
        eff = t + lag_mins
        orig_px = user_px - dx * eff / 10.0
        orig_py = user_py - dy * eff / 10.0

        hit = _is_rain_near(rain_mask, orig_px, orig_py, radius)

        if hit and not in_rain:
            in_rain = True
            rain_start = float(t)
            hit_orig_px = orig_px
            hit_orig_py = orig_py

        elif (not hit) and in_rain:
            in_rain = False
            ev = _build_event(
                rain_start, float(t),
                hit_orig_px, hit_orig_py,
                lag_mins, patch_tracks, rain_mask, rgb_arr, radius,
            )
            if ev:
                events.append(ev)
            rain_start = hit_orig_px = hit_orig_py = None

    # Handle rain still active at the end of the window
    if in_rain and rain_start is not None:
        ev = _build_event(
            rain_start, float(max_mins),
            hit_orig_px, hit_orig_py,
            lag_mins, patch_tracks, rain_mask, rgb_arr, radius,
        )
        if ev:
            events.append(ev)

    return sorted(events, key=lambda e: e.eta_mins)
