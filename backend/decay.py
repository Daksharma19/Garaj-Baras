"""
decay.py — Rain patch intensity tracking across radar frames.

For each rain cell (connected component), we track its mean dBZ across
consecutive frames and fit a linear trend. This tells us whether the patch
is growing, stable, weakening, or dying — and projects what intensity the
traveller will actually encounter at their ETA.

Three scenarios surfaced to the frontend:
  stable/growing  → rain is real, warn normally
  weakening       → rain will be lighter by the time you arrive
  dying/dead      → rain patch will likely be gone when you get there
"""

import math

import numpy as np
import cv2
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

from bbox_mask import BBoxMask


# ── dBZ threshold below which a patch is considered gone ──────────────────────
DEAD_DBZ_THRESHOLD = 8      # < 8 dBZ = effectively no rain
DYING_DBZ_THRESHOLD = 18    # < 18 dBZ = very light, nearly gone
WEAKENING_DBZ_THRESHOLD = 32  # < 32 dBZ and declining = weakening

# Minimum pixels for a blob to be tracked (noise guard)
MIN_BLOB_AREA_PX = 6

# Match radius in pixels: if expected centroid is within this of a real blob
# in the next frame → same patch
MATCH_RADIUS_PX = 35

# Minimum frames a patch must appear in to trust its decay rate
MIN_TRACK_LENGTH = 3

# A measured trend from ≤6 noisy observations can't be trusted linearly for
# 2 hours: damp it toward zero with lead time so the total projected change
# saturates at rate × TAU/10 dBZ instead of growing without bound.
TREND_DAMPING_TAU_MINS = 45.0
# Growth trends overshoot worse than decay (storms peak and collapse), so cap
# positive rates harder than negative ones.
MAX_GROWTH_RATE_DBZ_PER_10MIN = 1.5
MAX_DECAY_RATE_DBZ_PER_10MIN = -8.0

# ── Area trend (survivor-bias fix) ─────────────────────────────────────────
# Mean dBZ over a thresholded mask is blind to the most common decay mode:
# a dying storm loses its weak edges FIRST, so the mean over surviving pixels
# stays flat (or rises) while the blob visibly shrinks. So we also track blob
# AREA and classify against the projected surviving-area fraction.
# Area evolves multiplicatively → fit the trend on ln(area); the log-rate is
# clamped per 10 min: shrink to no less than ~30% (ln 0.3) and grow to no
# more than ~1.5× (ln 1.5) per 10 min.
MAX_AREA_SHRINK_LOG_PER_10MIN = -1.2
MAX_AREA_GROWTH_LOG_PER_10MIN = 0.4
# Projected surviving-area fraction thresholds for classification
AREA_DEAD_FRACTION = 0.15       # < 15 % of current area left → dead
AREA_DYING_FRACTION = 0.35      # < 35 % left → dying
AREA_WEAKENING_FRACTION = 0.65  # < 65 % left → weakening


@dataclass
class PatchTrack:
    frame_indices: List[int] = field(default_factory=list)
    centroids: List[Tuple[float, float]] = field(default_factory=list)
    mean_dbzs: List[float] = field(default_factory=list)
    areas: List[int] = field(default_factory=list)   # blob px area per frame
    mask_latest: Optional[BBoxMask] = None   # blob mask in last frame (bbox crop)
    decay_rate: float = 0.0     # dBZ per 10 real minutes (negative = dying)
    area_log_rate: float = 0.0  # ln(area) change per 10 real minutes (clamped)
    dbz_latest: float = 0.0
    status: str = "stable"      # stable | weakening | dying | dead


def _frame_blobs(rain_mask: np.ndarray, rgb_arr: np.ndarray) -> List[dict]:
    """
    Find connected rain blobs in a rain mask and measure their mean dBZ.
    Returns list of {centroid, mean_dbz, area_px, mask}.
    """
    from fuzzy import rgb_to_dbz

    n_labels, label_img, stats, centroids = cv2.connectedComponentsWithStats(
        rain_mask, connectivity=8
    )
    blobs = []
    for lbl in range(1, n_labels):  # 0 = background
        area = int(stats[lbl, cv2.CC_STAT_AREA])
        if area < MIN_BLOB_AREA_PX:
            continue
        bm = BBoxMask.from_labels(label_img, lbl, stats[lbl])
        ys, xs = bm.nonzero_full()
        dbzs = [rgb_to_dbz(int(rgb_arr[y, x, 0]),
                            int(rgb_arr[y, x, 1]),
                            int(rgb_arr[y, x, 2])) for y, x in zip(ys, xs)]
        mean_dbz = float(np.mean([d for d in dbzs if d > 0]) if any(d > 0 for d in dbzs) else 0.0)
        cx, cy = float(centroids[lbl][0]), float(centroids[lbl][1])
        blobs.append({"centroid": (cx, cy), "mean_dbz": mean_dbz,
                      "area_px": area, "mask": bm})
    return blobs


def _theil_sen_rate_per_10min(minutes: List[float], dbzs: List[float]) -> float:
    """
    Median of all pairwise slopes (Theil–Sen) over (real minutes, dBZ) points,
    scaled to dBZ per 10 min. Immune to a single outlier frame, which a
    least-squares fit over ≤6 points is not.
    """
    slopes = []
    for i in range(len(minutes)):
        for j in range(i + 1, len(minutes)):
            dt = minutes[j] - minutes[i]
            if dt > 0:
                slopes.append((dbzs[j] - dbzs[i]) / dt)
    if not slopes:
        return 0.0
    return float(np.median(slopes)) * 10.0


def _frame_minutes(frame_data: list) -> List[float]:
    """Cumulative minutes of each frame relative to the first, from real
    timestamps (10-min fallback per gap when a timestamp is missing)."""
    minutes = [0.0]
    for fi in range(1, len(frame_data)):
        ts_prev, ts_curr = frame_data[fi - 1][1], frame_data[fi][1]
        if ts_prev and ts_curr:
            gap = max(1.0, (ts_curr - ts_prev).total_seconds() / 60.0)
        else:
            gap = 10.0
        minutes.append(minutes[-1] + gap)
    return minutes


def dbz_change(decay_rate: float, mins_ahead: float) -> float:
    """
    Projected dBZ change after mins_ahead for a measured trend, with the rate
    damped exponentially toward zero (convective trends don't persist), so the
    change saturates instead of extrapolating linearly for hours.
    """
    rate = min(max(decay_rate, MAX_DECAY_RATE_DBZ_PER_10MIN),
               MAX_GROWTH_RATE_DBZ_PER_10MIN)
    tau = TREND_DAMPING_TAU_MINS
    return rate * (tau / 10.0) * (1.0 - math.exp(-max(0.0, mins_ahead) / tau))


def project_area_fraction(track: Optional["PatchTrack"], mins_ahead: float) -> float:
    """
    Projected fraction of the blob's current area still raining at mins_ahead,
    from the clamped ln(area) trend, damped with the same lead-time tau as the
    dBZ trend. 1.0 for None / no measured trend / growing blobs.
    """
    if track is None:
        return 1.0
    log_rate = min(max(float(getattr(track, "area_log_rate", 0.0)),
                       MAX_AREA_SHRINK_LOG_PER_10MIN),
                   MAX_AREA_GROWTH_LOG_PER_10MIN)
    if log_rate >= 0.0:
        return 1.0  # growth doesn't boost survival above certain
    tau = TREND_DAMPING_TAU_MINS
    log_change = log_rate * (tau / 10.0) * (1.0 - math.exp(-max(0.0, mins_ahead) / tau))
    return float(math.exp(log_change))


# Measured dBZ trend at/above this (per 10 real min) → the cell is intensifying
GROWING_RATE_DBZ_PER_10MIN = 1.0


def _classify(dbz_at_eta: float, decay_rate: float, area_frac: float = 1.0) -> str:
    if dbz_at_eta < DEAD_DBZ_THRESHOLD or area_frac < AREA_DEAD_FRACTION:
        return "dead"
    if dbz_at_eta < DYING_DBZ_THRESHOLD or area_frac < AREA_DYING_FRACTION:
        return "dying"
    if ((dbz_at_eta < WEAKENING_DBZ_THRESHOLD and decay_rate < -1.5)
            or area_frac < AREA_WEAKENING_FRACTION):
        return "weakening"
    # Pre-peak cell: intensity climbing and footprint not shrinking — it will
    # likely be at least as strong on arrival as it is now.
    if decay_rate >= GROWING_RATE_DBZ_PER_10MIN and area_frac >= 1.0:
        return "growing"
    return "stable"


def compute_decay_tracks(
    frame_data: list,           # list of (frame_path, timestamp)
    dx: float,
    dy: float,
    clutter_mask: Optional[np.ndarray] = None,
) -> List[PatchTrack]:
    """
    Walk all consecutive frame pairs, detect blobs, link same patches across
    frames using expected centroid (global optical flow), and fit a linear
    decay rate to each track.

    Returns a list of PatchTrack objects anchored to the LATEST frame.
    """
    from optical_flow import isolate_rain
    from PIL import Image

    if not frame_data or len(frame_data) < 2:
        return []

    # ── Build per-frame blob lists ────────────────────────────────────────────
    per_frame_blobs = []
    for fp, ts in frame_data:
        try:
            rain_mask = isolate_rain(fp, clutter_mask=clutter_mask)
            rgb_arr = np.array(Image.open(fp).convert("RGB"))
            blobs = _frame_blobs(rain_mask, rgb_arr)
        except Exception:
            blobs = []
        per_frame_blobs.append(blobs)

    n_frames = len(per_frame_blobs)

    # ── Link blobs across frames into tracks ──────────────────────────────────
    # Each track is a list of (frame_idx, blob_dict)
    active_tracks: List[List[Tuple[int, dict]]] = []

    # Seed tracks from frame 0
    for blob in per_frame_blobs[0]:
        active_tracks.append([(0, blob)])

    for fi in range(1, n_frames):
        # Compute time gap for expected centroid shift
        ts_prev = frame_data[fi - 1][1]
        ts_curr = frame_data[fi][1]
        if ts_prev and ts_curr:
            gap_mins = max(1.0, (ts_curr - ts_prev).total_seconds() / 60.0)
        else:
            gap_mins = 10.0
        shift_factor = gap_mins / 10.0

        current_blobs = per_frame_blobs[fi]
        matched_blob_indices = set()
        new_tracks = []

        for track in active_tracks:
            last_fi, last_blob = track[-1]
            if last_fi != fi - 1:
                # Track already lost — don't try to extend
                new_tracks.append(track)
                continue

            cx, cy = last_blob["centroid"]
            exp_cx = cx + dx * shift_factor
            exp_cy = cy + dy * shift_factor

            # Find nearest unmatched blob in this frame
            best_idx = None
            best_dist = MATCH_RADIUS_PX
            for bi, blob in enumerate(current_blobs):
                if bi in matched_blob_indices:
                    continue
                bcx, bcy = blob["centroid"]
                dist = ((bcx - exp_cx) ** 2 + (bcy - exp_cy) ** 2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best_idx = bi

            if best_idx is not None:
                matched_blob_indices.add(best_idx)
                track = track + [(fi, current_blobs[best_idx])]
            new_tracks.append(track)

        # Unmatched blobs in this frame start new tracks
        for bi, blob in enumerate(current_blobs):
            if bi not in matched_blob_indices:
                new_tracks.append([(fi, blob)])

        active_tracks = new_tracks

    # ── Convert tracks to PatchTrack objects ──────────────────────────────────
    patch_tracks = []
    latest_fi = n_frames - 1
    frame_mins = _frame_minutes(frame_data)

    for track in active_tracks:
        if not track:
            continue

        frame_indices = [t[0] for t in track]
        mean_dbzs = [t[1]["mean_dbz"] for t in track]
        centroids = [t[1]["centroid"] for t in track]
        areas = [t[1]["area_px"] for t in track]

        # Only use tracks that reach the latest frame (anchored to NOW)
        if frame_indices[-1] != latest_fi:
            continue

        dbz_latest = mean_dbzs[-1]
        mask_latest = track[-1][1]["mask"]

        # Fit trend against REAL minutes (frame gaps vary: dedup + IMD cadence
        # drift), robust to a single outlier frame via Theil–Sen.
        obs_mins = [frame_mins[fi] for fi in frame_indices]
        if len(mean_dbzs) >= MIN_TRACK_LENGTH:
            decay_rate = _theil_sen_rate_per_10min(obs_mins, mean_dbzs)
        elif len(mean_dbzs) == 2:
            dt = max(1.0, obs_mins[1] - obs_mins[0])
            decay_rate = float(mean_dbzs[1] - mean_dbzs[0]) / dt * 10.0
        else:
            decay_rate = 0.0  # single observation — assume stable

        # Area trend in log domain (area evolves multiplicatively), same
        # Theil–Sen / real-minute treatment as the dBZ trend, clamped so one
        # merge/split glitch can't declare instant death or explosive growth.
        log_areas = [math.log(max(1.0, a)) for a in areas]
        if len(log_areas) >= MIN_TRACK_LENGTH:
            area_log_rate = _theil_sen_rate_per_10min(obs_mins, log_areas)
        elif len(log_areas) == 2:
            dt = max(1.0, obs_mins[1] - obs_mins[0])
            area_log_rate = (log_areas[1] - log_areas[0]) / dt * 10.0
        else:
            area_log_rate = 0.0
        area_log_rate = min(max(area_log_rate, MAX_AREA_SHRINK_LOG_PER_10MIN),
                            MAX_AREA_GROWTH_LOG_PER_10MIN)

        pt = PatchTrack(
            frame_indices=frame_indices,
            centroids=centroids,
            mean_dbzs=mean_dbzs,
            areas=areas,
            mask_latest=mask_latest,
            decay_rate=decay_rate,
            area_log_rate=area_log_rate,
            dbz_latest=dbz_latest,
            status="stable",  # filled below
        )
        patch_tracks.append(pt)

    return patch_tracks


def project_dbz(track: PatchTrack, eta_mins: float) -> float:
    """Estimated dBZ of this patch when traveller arrives (eta_mins from now)."""
    return track.dbz_latest + dbz_change(track.decay_rate, eta_mins)


def get_decay_status_at_pixel(
    px: int,
    py: int,
    eta_mins: float,
    patch_tracks: List[PatchTrack],
) -> dict:
    """
    For a given pixel (waypoint location in radar frame) and ETA, find which
    patch it belongs to and return its decay status + projected dBZ.

    Returns dict: {decay_status, projected_dbz, current_dbz}
    """
    for track in patch_tracks:
        if track.mask_latest is None:
            continue
        if track.mask_latest.hit(px, py, radius=0):
            proj = project_dbz(track, eta_mins)
            status = _classify(proj, track.decay_rate,
                               project_area_fraction(track, eta_mins))
            return {
                "decay_status": status,
                "projected_dbz": round(max(0.0, proj), 1),
                "current_dbz": round(track.dbz_latest, 1),
                "decay_rate": round(track.decay_rate, 2),
            }
    # Pixel is in rain mask but not in any tracked patch — treat as stable
    return {
        "decay_status": "stable",
        "projected_dbz": None,
        "current_dbz": None,
        "decay_rate": 0.0,
    }
