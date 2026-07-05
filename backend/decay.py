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


@dataclass
class PatchTrack:
    frame_indices: List[int] = field(default_factory=list)
    centroids: List[Tuple[float, float]] = field(default_factory=list)
    mean_dbzs: List[float] = field(default_factory=list)
    mask_latest: Optional[BBoxMask] = None   # blob mask in last frame (bbox crop)
    decay_rate: float = 0.0     # dBZ per 10-min frame (negative = dying)
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


def _classify(dbz_at_eta: float, decay_rate: float) -> str:
    if dbz_at_eta < DEAD_DBZ_THRESHOLD:
        return "dead"
    if dbz_at_eta < DYING_DBZ_THRESHOLD:
        return "dying"
    if dbz_at_eta < WEAKENING_DBZ_THRESHOLD and decay_rate < -1.5:
        return "weakening"
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

    for track in active_tracks:
        if not track:
            continue

        frame_indices = [t[0] for t in track]
        mean_dbzs = [t[1]["mean_dbz"] for t in track]
        centroids = [t[1]["centroid"] for t in track]

        # Only use tracks that reach the latest frame (anchored to NOW)
        if frame_indices[-1] != latest_fi:
            continue

        dbz_latest = mean_dbzs[-1]
        mask_latest = track[-1][1]["mask"]

        # Fit linear trend if enough observations
        if len(mean_dbzs) >= MIN_TRACK_LENGTH:
            xs = np.arange(len(mean_dbzs), dtype=float)
            coeffs = np.polyfit(xs, mean_dbzs, 1)
            decay_rate = float(coeffs[0])   # dBZ per frame
        elif len(mean_dbzs) == 2:
            decay_rate = float(mean_dbzs[1] - mean_dbzs[0])
        else:
            decay_rate = 0.0  # single observation — assume stable

        pt = PatchTrack(
            frame_indices=frame_indices,
            centroids=centroids,
            mean_dbzs=mean_dbzs,
            mask_latest=mask_latest,
            decay_rate=decay_rate,
            dbz_latest=dbz_latest,
            status="stable",  # filled below
        )
        patch_tracks.append(pt)

    return patch_tracks


def project_dbz(track: PatchTrack, eta_mins: float) -> float:
    """Estimated dBZ of this patch when traveller arrives (eta_mins from now)."""
    frames_ahead = eta_mins / 10.0
    return track.dbz_latest + track.decay_rate * frames_ahead


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
            status = _classify(proj, track.decay_rate)
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
