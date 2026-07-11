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
# Raised 8 → 20: slots the engine itself only believes at <20% were shown as
# rain and dominated false alarms in the far slots.
MIN_PROBABILITY: int = 20

# Patch-forward: search circle radius around user (pixels)
PATCH_SEARCH_RADIUS_PX: int = 120

# Patches with |dx_10| < this AND |dy_10| < this are fresh pop-ups — skip forward projection
FRESH_POPUP_VELOCITY_THRESH: float = 0.3

# ── New-cell (single-observation) lifecycle ───────────────────────────────────
# A cell seen in only ONE frame has no measured motion or intensity trend.
# Previously it was assumed "stable" forever, so a fresh pop-up sitting on the
# user predicted unchanged heavy rain for the full 2-h horizon. Small convective
# pop-ups typically live 30–60 min, so give unobserved cells a synthetic
# climatological decay instead. Tracked patches (>= 2 observations) are NEVER
# touched by this — their measured decay_rate (≈0 for a consistent mover) wins.
NEW_CELL_DECAY_DBZ_PER_10MIN: float = -2.5
# A never-observed-moving cell is asserted for THREE checkpoints only
# (now, +15, +30). Beyond that, only tracked motion / measured decay logic
# may claim rain — a pop-up we've never seen move earns no longer horizon.
NEW_CELL_MAX_ASSERT_MINS: float = 30.0
# Projected dBZ below this → treat the new cell as rained out
NEW_CELL_MIN_DBZ: float = 10.0

# Widespread-rain detection: if rain covers >= this fraction of a wide circle
# around the source point, it's a broad shield (stratiform / monsoon band),
# not a convective pop-up — the 30-min new-cell kill must not apply.
WIDESPREAD_RADIUS_PX: int = 60
WIDESPREAD_MIN_FRACTION: float = 0.5


def _track_has_history(track) -> bool:
    """True if the decay track has >= 2 observations (measured trend exists)."""
    return track is not None and len(getattr(track, "mean_dbzs", []) or []) >= 2


def _new_cell_projection(raw_dbz: float, eff_mins: float, slot_mins: float):
    """
    Synthetic lifecycle for a cell with no observed history.
    Returns (proj_dbz, decay_status, has_rain).
    """
    if slot_mins > NEW_CELL_MAX_ASSERT_MINS:
        return 0.0, "new_cell", False
    proj = max(0.0, float(raw_dbz) + NEW_CELL_DECAY_DBZ_PER_10MIN * (eff_mins / 10.0))
    if proj < NEW_CELL_MIN_DBZ:
        return proj, "new_cell", False
    return proj, "new_cell", True


@dataclass
class NowcastEvent:
    eta_mins: float        # minutes until this rain wave reaches the user
    duration_mins: float   # estimated duration at this location
    probability: int       # 0–100, derived from projected dBZ after decay
    intensity: str         # IMD label at projected dBZ (e.g. "Light Rain")
    projected_dbz: float   # estimated dBZ when the rain arrives (post-decay)
    raw_dbz: float         # current dBZ of the source patch in the latest frame
    decay_status: str      # stable | weakening | dying | dead


# ── dBZ → intensity probability (legacy, used by compute_nowcast events) ─────

def dbz_to_probability(projected_dbz: float) -> int:
    """Maps projected dBZ to a rough intensity-based probability (legacy path)."""
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


# ── Motion ensemble — data-driven "will it actually reach me?" ───────────────
#
# Instead of a hardcoded lead-time table, arrival probability is now computed
# from the radar data itself:
#
#   1. The motion vector is perturbed into a 25-member ensemble
#      (5 direction offsets × 5 speed factors, Gaussian-weighted). A fixed
#      angular/speed error compounds with distance travelled, so positional
#      spread grows naturally with lead time — no explicit time penalty needed.
#   2. Each member back-projects the user's pixel (or forward-projects the
#      patch centroid) and scores the *fraction* of rain pixels in the
#      neighborhood — not a binary any(). Sitting deep inside a wide rain
#      shield scores ~1.0; clipping the edge of a small cell scores ~0.2.
#   3. The weighted mean over members is the geometric arrival probability.
#   4. A mild lead-time skill factor accounts for the irreducible loss of
#      extrapolation skill (storms grow/die/turn), and the existing decay
#      penalties still apply.
#
# Result: a broad steady rain shield stays at high probability across the
# whole horizon, while a small wobbly cell far away decays quickly — the
# probability now tracks the storm, not the clock.

# Narrowed (±24° → ±15°, 0.70–1.30× → 0.85–1.15×): the old cone was so wide
# that storms passing 10–20 km to the side still landed enough members on the
# user to cross the floor — a major false-positive source. The vector comes
# from 5 recency-weighted frame pairs, so ±15° is still a fair error budget.
ENSEMBLE_ANGLES_DEG = (-15.0, -7.5, 0.0, 7.5, 15.0)
ENSEMBLE_SPEED_FACTORS = (0.85, 0.925, 1.0, 1.075, 1.15)
_ENSEMBLE_W1D = (0.06, 0.24, 0.40, 0.24, 0.06)

# Extrapolation skill fades with lead time even for a perfect geometric hit
SKILL_FLOOR: float = 0.60
SKILL_HORIZON_MINS: float = 105.0

# Geometric probability below this (0-1) → the slot is treated as no-rain
# Raised 0.12 → 0.30: at 12%, a graze by the ensemble's edge members was
# enough to declare rain; now a weighted ~third of members must cover the user.
GEO_PROB_FLOOR: float = 0.30


def _perturbed_vectors(dx: float, dy: float):
    """25-member (weight, dx, dy) ensemble around the given motion vector."""
    members = []
    for ai, ang in enumerate(ENSEMBLE_ANGLES_DEG):
        rad = math.radians(ang)
        ca, sa = math.cos(rad), math.sin(rad)
        rdx = dx * ca - dy * sa
        rdy = dx * sa + dy * ca
        for si, s in enumerate(ENSEMBLE_SPEED_FACTORS):
            members.append((_ENSEMBLE_W1D[ai] * _ENSEMBLE_W1D[si], rdx * s, rdy * s))
    return members


def _rain_fraction(rain_mask: np.ndarray, fx: float, fy: float, radius: int) -> float:
    """Fraction (0-1) of rain pixels within `radius` of (fx, fy)."""
    h, w = rain_mask.shape
    x0 = max(0, int(fx) - radius)
    x1 = min(w - 1, int(fx) + radius)
    y0 = max(0, int(fy) - radius)
    y1 = min(h - 1, int(fy) + radius)
    if x1 < x0 or y1 < y0:
        return 0.0
    win = rain_mask[y0:y1 + 1, x0:x1 + 1]
    return float(np.count_nonzero(win)) / float(win.size)


def _ensemble_arrival(
    user_px: float,
    user_py: float,
    dx: float,
    dy: float,
    eff_mins: float,
    rain_mask: np.ndarray,
    radius: int,
):
    """
    Back-project the user's pixel with every ensemble member and score the
    rain coverage there. Returns (prob 0-1, best_px, best_py) where best_*
    is the member point with the highest rain fraction (used for dBZ sampling).
    """
    total = 0.0
    best_frac = 0.0
    best_pt = (user_px - dx * eff_mins / 10.0, user_py - dy * eff_mins / 10.0)
    for w, pdx, pdy in _perturbed_vectors(dx, dy):
        ox = user_px - pdx * eff_mins / 10.0
        oy = user_py - pdy * eff_mins / 10.0
        f = _rain_fraction(rain_mask, ox, oy, radius)
        total += w * f
        if f > best_frac:
            best_frac = f
            best_pt = (ox, oy)
    return min(1.0, total), best_pt[0], best_pt[1]


def _ensemble_patch_prob(
    patch: dict,
    user_px: float,
    user_py: float,
    eff_mins: float,
    radius: int,
) -> float:
    """
    Probability (0-1) that this patch's projected footprint covers the user,
    over the 25-member velocity ensemble. Members that land just outside the
    footprint get partial credit (soft edge) — a storm predicted to graze the
    user is a maybe, not a hard no.
    """
    cx, cy = patch["centroid_px"]
    vx = patch.get("dx_10", 0.0)
    vy = patch.get("dy_10", 0.0)
    patch_radius = max(5, int(math.sqrt(patch.get("area_px", 25) / math.pi)))
    hit_r = patch_radius + radius
    total = 0.0
    for w, pvx, pvy in _perturbed_vectors(vx, vy):
        px = cx + pvx * eff_mins / 10.0
        py = cy + pvy * eff_mins / 10.0
        d = math.sqrt((px - user_px) ** 2 + (py - user_py) ** 2)
        if d <= hit_r:
            total += w
        elif d <= hit_r * 1.5:
            total += w * (1.0 - (d - hit_r) / (hit_r * 0.5))
    return min(1.0, total)


def _lead_time_skill(slot_mins: float) -> float:
    """Extrapolation skill factor: 1.0 now → SKILL_FLOOR at the horizon."""
    frac = min(1.0, max(0.0, slot_mins / SKILL_HORIZON_MINS))
    return 1.0 - (1.0 - SKILL_FLOOR) * frac


def _decay_factor(decay_status: str) -> float:
    """Survival penalty for weakening storms (unchanged from legacy logic)."""
    if decay_status == "dead":
        return 0.0
    if decay_status == "dying":
        return 0.45
    if decay_status == "weakening":
        return 0.75
    if decay_status == "new_cell":
        return 0.8
    return 1.0


# ── Arrival confidence (legacy lead-time table — kept for reference/compat) ──

def arrival_confidence(
    slot_mins: float,
    decay_status: str,
    is_patch_hit: bool,
    patch_dist_px: float = 0.0,
) -> int:
    """
    Probability (0–100) that detected rain actually reaches the user.

    Completely independent of rain intensity — a light drizzle sitting
    directly on the user is 95% certain; a heavy cell 100px away at the
    90-min horizon might be only 25% certain.

    Factors:
      slot_mins      — further ahead = more time for the storm to deviate/die
      decay_status   — dying/weakening patches may not survive to arrival
      is_patch_hit   — tracked cell (high confidence) vs global-flow fallback
      patch_dist_px  — how far the patch centroid is from the user right now
    """
    # Base: degrades with time horizon
    if slot_mins <= 0:
        base = 95
    elif slot_mins <= 15:
        base = 88
    elif slot_mins <= 30:
        base = 78
    elif slot_mins <= 45:
        base = 67
    elif slot_mins <= 60:
        base = 56
    elif slot_mins <= 75:
        base = 46
    elif slot_mins <= 90:
        base = 37
    else:
        base = 29

    # Source quality: tracked patch cell > global-flow guess
    if is_patch_hit:
        base = int(base * 1.08)
    else:
        base = int(base * 0.85)

    # Distance penalty: farther patch has more room to miss
    if is_patch_hit and patch_dist_px > 0:
        if patch_dist_px > 80:
            base = int(base * 0.78)
        elif patch_dist_px > 50:
            base = int(base * 0.87)
        elif patch_dist_px > 25:
            base = int(base * 0.94)

    # Decay penalty: weakening/dying storm may not survive to arrival
    if decay_status == "dead":
        return 0
    if decay_status == "dying":
        base = int(base * 0.45)
    elif decay_status == "weakening":
        base = int(base * 0.75)
    elif decay_status == "new_cell":
        # Brand-new cell with no observed history — short-term confidence only
        base = int(base * 0.8)

    return max(0, min(99, base))


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
    MATCH_RADIUS_PX = 50
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
        if track.mask_latest.hit(int(orig_px), int(orig_py), radius=10):
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
    from decay import project_dbz, _classify, project_area_fraction  # type: ignore
    from fuzzy import dbz_to_label            # type: ignore

    eta = rain_start
    duration = max(2.0, rain_end - rain_start)

    track = _find_patch_track(hit_px, hit_py, patch_tracks)
    if track:
        proj_dbz = max(0.0, project_dbz(track, eta + lag_mins))
        decay_status = _classify(proj_dbz, track.decay_rate,
                                 project_area_fraction(track, eta + lag_mins))
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
    from decay import project_dbz, _classify, dbz_change, project_area_fraction  # type: ignore
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

        has_rain = False
        proj_dbz = 0.0
        decay_status = "stable"
        patch_hit = None
        is_patch_hit = False
        geo_prob = 0.0  # 0-1 ensemble probability that rain covers the user

        # ── 1. PATCH-FORWARD PASS (velocity ensemble per patch) ─────────────
        for patch in _sorted_patches:
            cx, cy = patch["centroid_px"]
            vx = patch.get("dx_10", 0.0)
            vy = patch.get("dy_10", 0.0)

            cur_dist = math.sqrt((cx - user_px) ** 2 + (cy - user_py) ** 2)
            if cur_dist > search_radius_px:
                continue
            if abs(vx) < FRESH_POPUP_VELOCITY_THRESH and abs(vy) < FRESH_POPUP_VELOCITY_THRESH:
                continue

            p = _ensemble_patch_prob(patch, user_px, user_py, eff, radius)
            if p > geo_prob:
                geo_prob = p
                patch_hit = patch

        # ── 2. GLOBAL-FLOW back-projection ensemble ──────────────────────────
        # Always computed: widespread rain behind a small tracked patch must
        # not be under-reported just because the patch only grazes the user.
        fb_prob, orig_px, orig_py = _ensemble_arrival(
            user_px, user_py, dx, dy, eff, rain_mask, radius,
        )

        # Use whichever source is more confident
        if patch_hit is not None and geo_prob >= max(fb_prob, GEO_PROB_FLOOR):
            has_rain = True
            is_patch_hit = True
        elif fb_prob >= GEO_PROB_FLOOR:
            has_rain = True
            geo_prob = fb_prob
            patch_hit = None
        else:
            patch_hit = None
            geo_prob = 0.0

        # ── 3. dBZ / decay lookup ─────────────────────────────────────────────
        if has_rain and patch_hit is not None:
            track = _find_track_for_patch(patch_hit, patch_tracks)
            raw = float(patch_hit.get("max_dbz", 0))
            if track:
                proj_dbz = max(0.0, raw + dbz_change(track.decay_rate, eff))
                decay_status = _classify(proj_dbz, track.decay_rate,
                                         project_area_fraction(track, eff))
            else:
                proj_dbz = raw
                decay_status = "stable"
        elif has_rain:
            track = _find_patch_track(orig_px, orig_py, patch_tracks)
            raw = _sample_raw_dbz(rain_mask, rgb_arr, orig_px, orig_py, radius)
            if _track_has_history(track):
                proj_dbz = max(0.0, raw + dbz_change(track.decay_rate, eff))
                decay_status = _classify(proj_dbz, track.decay_rate,
                                         project_area_fraction(track, eff))
            elif _rain_fraction(rain_mask, orig_px, orig_py, WIDESPREAD_RADIUS_PX) >= WIDESPREAD_MIN_FRACTION:
                # Broad rain shield around the source point — not a pop-up.
                # Keep the climatological decay but allow the full horizon.
                proj_dbz = max(0.0, raw + NEW_CELL_DECAY_DBZ_PER_10MIN * (eff / 10.0))
                decay_status = "stable"
                has_rain = proj_dbz >= NEW_CELL_MIN_DBZ
            else:
                # Untracked / single-observation cell: synthetic lifecycle.
                # Moving patches with a measured trend never reach this branch
                # — they are handled by the patch-forward pass or the
                # _track_has_history case above.
                proj_dbz, decay_status, has_rain = _new_cell_projection(raw, eff, t)

        # ── 3b. Background rain override (slot 0 only) ────────────────────────
        # If the latest frame OBSERVES rain at the user's location, "now" must
        # say rain — light background drizzle is real weather, and neither the
        # thresholds above nor a fast global vector may erase an observation.
        is_observed_now = False
        if t == 0 and not has_rain and _is_rain_near(rain_mask, user_px, user_py, radius):
            raw = _sample_raw_dbz(rain_mask, rgb_arr, user_px, user_py, radius)
            if raw > 0:
                has_rain = True
                is_observed_now = True
                proj_dbz = raw
                track = _find_patch_track(user_px, user_py, patch_tracks)
                decay_status = (_classify(raw, track.decay_rate)
                                if _track_has_history(track) else "stable")

        # ── 4. Two separate scores ────────────────────────────────────────────
        # arrival_confidence: ensemble geometry × skill × decay — "will it reach me?"
        # intensity:          dBZ label                         — "how heavy is the rain?"
        if is_observed_now:
            # Not a projection — the radar sees it (modulo lag)
            arr_conf = 90
        elif has_rain:
            arr_conf = int(round(
                100.0 * geo_prob * _lead_time_skill(t) * _decay_factor(decay_status)
            ))
            arr_conf = max(0, min(97, arr_conf))
        else:
            arr_conf = 0

        if arr_conf < MIN_PROBABILITY:
            has_rain = False
            proj_dbz = 0.0
            arr_conf = 0

        intensity_label = "No Rain"
        if has_rain:
            intensity_label, _ = dbz_to_label(max(0.0, proj_dbz))

        slots.append({
            "slot_mins": t,
            "has_rain": has_rain,
            "probability": arr_conf,          # kept for backward compat (bar height)
            "arrival_confidence": arr_conf,   # "will it reach me?" 0–100
            "intensity": intensity_label,     # "how heavy?" IMD label
            "projected_dbz": round(proj_dbz, 1),
            "decay_status": decay_status,
            "source": ("observed" if is_observed_now
                       else "patch" if is_patch_hit
                       else "fallback" if has_rain else "none"),
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
