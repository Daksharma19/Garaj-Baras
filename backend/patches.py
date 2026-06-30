# Garaj Baras — patches.py
# Per-patch rain motion and route intercept analysis.

import numpy as np
import cv2
from PIL import Image

from georef import IMAGE_WIDTH, IMAGE_HEIGHT, latlon_to_pixel, pixel_to_latlon, CENTER_LAT, CENTER_LON
from optical_flow import isolate_rain, get_direction_string
from fuzzy import COLOR_TABLE

_roi_cache = {}
_roi_cache_generic = {}

# km per pixel — kept consistent with optical_flow.py
_KM_PER_PX = 0.877

# Pre-built arrays for vectorized dBZ lookup (avoids per-pixel Python loops)
_CT_RGB = np.array([(r, g, b) for r, g, b, _ in COLOR_TABLE], dtype=np.int16)
_CT_DBZ = np.array([d for _, _, _, d in COLOR_TABLE], dtype=np.int32)
_MAX_DIST2 = 80 ** 2  # beyond this RGB distance, pixel is not a legend color


def build_roi_mask(latlon_to_pixel_fn, image_width, image_height, center_lat, center_lon, radius_km=150.0):
    """
    Generic ROI mask builder — not tied to any specific radar's georef.
    Returns uint8 mask (255 = inside coverage circle, 0 = outside).
    """
    key = (id(latlon_to_pixel_fn), round(radius_km, 1))
    if key in _roi_cache_generic:
        return _roi_cache_generic[key]

    cx, cy = latlon_to_pixel_fn(center_lat, center_lon)
    radius_px = radius_km / _KM_PER_PX

    ys, xs = np.ogrid[:image_height, :image_width]
    dist2 = (xs - cx) ** 2 + (ys - cy) ** 2
    mask = np.where(dist2 <= radius_px ** 2, 255, 0).astype(np.uint8)
    _roi_cache_generic[key] = mask
    return mask


def build_ncr_roi_mask(radius_km=150.0):
    """
    Returns a uint8 mask (255 = inside NCR region, 0 = outside).
    Computed once in pixel-space using Euclidean distance from Delhi center
    scaled by km/px, then cached for the process lifetime.
    """
    key = round(radius_km, 1)
    if key in _roi_cache:
        return _roi_cache[key]

    cx, cy = latlon_to_pixel(CENTER_LAT, CENTER_LON)
    radius_px = radius_km / _KM_PER_PX

    ys, xs = np.ogrid[:IMAGE_HEIGHT, :IMAGE_WIDTH]
    dist2 = (xs - cx) ** 2 + (ys - cy) ** 2
    mask = np.where(dist2 <= radius_px ** 2, 255, 0).astype(np.uint8)
    _roi_cache[key] = mask
    return mask


def _patch_max_dbz(img_rgb, px_mask):
    """Vectorized max dBZ for pixels selected by px_mask (boolean H×W array)."""
    pixels = img_rgb[px_mask].astype(np.int16)
    if len(pixels) == 0:
        return 0
    diff = pixels[:, None, :] - _CT_RGB[None, :, :]
    dist2 = np.sum(diff.astype(np.int32) ** 2, axis=2)
    nearest = dist2.argmin(axis=1)
    min_dist2 = dist2[np.arange(len(pixels)), nearest]
    valid = min_dist2 <= _MAX_DIST2
    if not valid.any():
        return 0
    dbz_vals = np.where(valid, _CT_DBZ[nearest], 0)
    return int(dbz_vals.max())


def compute_patch_motion(frame_paths, gap_mins=10.0,
                         clutter_mask=None, roi_mask=None, min_area_px=4,
                         pixel_to_latlon_fn=None):
    """
    Detects individual rain patches (connected components) in the latest radar
    frame and computes each patch's own motion vector via dense optical flow.

    frame_paths: list of frame file paths, oldest first, latest last.
                 Use at least 4 frames (3 pairs) for reliable velocity averaging.
                 With 2 paths the result is identical to the previous behaviour.

    Optical flow is computed for every consecutive pair and averaged. This gives
    stable patches a smooth multi-frame velocity estimate. Fresh pop-ups (only
    visible in the last frame) naturally get a dampened velocity because earlier
    pairs show near-zero flow at those pixels.

    Returns a list of patch dicts. Each dict includes a 'mask' key (numpy bool
    array) that must be stripped before JSON serialization (_patch_to_public).
    """
    if len(frame_paths) < 2:
        return []

    p_last = frame_paths[-1]
    mask_last = isolate_rain(p_last, clutter_mask=clutter_mask)
    if mask_last.max() == 0:
        return []

    # Compute optical flow for every consecutive pair.
    imgs_gray = [np.array(Image.open(p).convert('L')) for p in frame_paths]
    flows = []
    for i in range(len(frame_paths) - 1):
        f = cv2.calcOpticalFlowFarneback(
            imgs_gray[i], imgs_gray[i + 1], None,
            pyr_scale=0.5, levels=3, winsize=15,
            iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
        )
        flows.append(f)

    # Rain masks for intermediate frames (frame_paths[1:-1]).
    # older_rain_masks[k] is the mask of frame_paths[k+1].
    # Used per-patch below to decide whether to include flows[k] in the average.
    older_rain_masks = [
        isolate_rain(p, clutter_mask=clutter_mask)
        for p in frame_paths[1:-1]
    ]

    latest_flow = flows[-1]  # always used — patch exists in the latest frame

    _, labels, _stats, centroids = cv2.connectedComponentsWithStats(mask_last, connectivity=8)
    img_rgb = np.array(Image.open(p_last).convert('RGB'))

    gap = max(gap_mins, 1.0)
    opposite = {"N": "S", "NE": "SW", "E": "W", "SE": "NW",
                "S": "N", "SW": "NE", "W": "E", "NW": "SE"}

    patches = []
    for comp_id in range(1, int(labels.max()) + 1):
        px_mask = labels == comp_id
        area = int(px_mask.sum())
        if area < min_area_px:
            continue

        # centroids[comp_id] = (cx_col, cy_row)
        cx_f = float(centroids[comp_id][0])
        cy_f = float(centroids[comp_id][1])

        ci, cj = int(round(cy_f)), int(round(cx_f))

        # Build per-patch valid flow list.
        # Always include the latest pair. Include an older pair only if rain
        # existed near the patch centroid in that intermediate frame — avoids
        # diluting velocity with zero-flow from frames where the patch didn't exist.
        valid_flows = [latest_flow]
        CENTROID_SEARCH_PX = 12
        for k, old_mask in enumerate(older_rain_masks):
            r0 = max(0, ci - CENTROID_SEARCH_PX)
            r1 = min(old_mask.shape[0], ci + CENTROID_SEARCH_PX + 1)
            c0 = max(0, cj - CENTROID_SEARCH_PX)
            c1 = min(old_mask.shape[1], cj + CENTROID_SEARCH_PX + 1)
            if old_mask[r0:r1, c0:c1].any():
                valid_flows.append(flows[k])

        dx = float(np.mean([np.mean(f[:, :, 0][px_mask]) for f in valid_flows]))
        dy = float(np.mean([np.mean(f[:, :, 1][px_mask]) for f in valid_flows]))

        magnitude = (dx ** 2 + dy ** 2) ** 0.5
        speed_kmh = (magnitude * _KM_PER_PX / gap) * 60.0
        dx_10 = dx * (10.0 / gap)
        dy_10 = dy * (10.0 / gap)

        direction_to = get_direction_string(dx, dy)
        direction_from = opposite.get(direction_to, "Unknown")

        in_ncr = True
        if roi_mask is not None:
            ci, cj = int(round(cy_f)), int(round(cx_f))
            if 0 <= ci < roi_mask.shape[0] and 0 <= cj < roi_mask.shape[1]:
                in_ncr = bool(roi_mask[ci, cj] > 0)
            else:
                in_ncr = False

        max_dbz = _patch_max_dbz(img_rgb, px_mask)
        _ptl = pixel_to_latlon_fn if pixel_to_latlon_fn is not None else pixel_to_latlon
        lat, lon = _ptl(int(round(cx_f)), int(round(cy_f)))

        patches.append({
            "id": comp_id,
            "area_px": area,
            "centroid_px": (cx_f, cy_f),
            "centroid_latlon": (lat, lon),
            "dx_10": dx_10,
            "dy_10": dy_10,
            "speed_kmh": speed_kmh,
            "direction_from": direction_from,
            "direction_to": direction_to,
            "max_dbz": max_dbz,
            "in_ncr": in_ncr,
            "mask": px_mask,
        })

    return patches


def score_patches_for_route(patches_motion, waypoints_pixels,
                             lag_mins=0.0, horizon_mins=120.0):
    """
    For each patch, predict its centroid position at each waypoint ETA and
    check whether it intercepts the route within a tolerance radius.

    Mutates patch dicts in place (adds relevance, will_hit_route, intercept_eta,
    intercept_px). Concurrent route requests may race on these fields — acceptable
    at current traffic; fix later by returning fresh dicts instead of annotating.

    Returns:
        {
          "first":    earliest-intercepting NCR patch (or None),
          "relevant": NCR patches sorted by relevance descending,
          "patches":  all patches sorted by relevance descending,
        }
    """
    INTERCEPT_RADIUS_PX = 25  # ~22 km at 0.877 km/px

    for patch in patches_motion:
        patch["will_hit_route"] = False
        patch["intercept_eta"] = None
        patch["intercept_px"] = None
        patch["relevance"] = 0.0

        if not patch.get("in_ncr", False):
            continue

        dx_10 = patch.get("dx_10", 0.0)
        dy_10 = patch.get("dy_10", 0.0)
        cx, cy = patch["centroid_px"]

        for px, py, eta_mins in waypoints_pixels:
            if eta_mins > horizon_mins:
                continue
            total_mins = lag_mins + eta_mins
            shifts = total_mins / 10.0
            pred_cx = cx + dx_10 * shifts
            pred_cy = cy + dy_10 * shifts
            dist = ((pred_cx - px) ** 2 + (pred_cy - py) ** 2) ** 0.5
            if dist <= INTERCEPT_RADIUS_PX:
                patch["will_hit_route"] = True
                patch["intercept_eta"] = eta_mins
                patch["intercept_px"] = (pred_cx, pred_cy)
                break

        rel = 0.0
        if patch["will_hit_route"]:
            rel += 100.0
            eta = patch["intercept_eta"]
            if eta is not None:
                rel += max(0.0, horizon_mins - eta)
        rel += patch.get("max_dbz", 0) * 0.5
        rel += min(patch.get("area_px", 0), 5000) * 0.01
        patch["relevance"] = rel

    hitting = sorted(
        (p for p in patches_motion if p.get("will_hit_route")),
        key=lambda p: p.get("intercept_eta") or float("inf"),
    )
    ncr = [p for p in patches_motion if p.get("in_ncr", False)]
    relevant = sorted(ncr, key=lambda p: -p.get("relevance", 0.0))
    all_sorted = sorted(patches_motion, key=lambda p: -p.get("relevance", 0.0))

    return {
        "first": hitting[0] if hitting else None,
        "relevant": relevant,
        "patches": all_sorted,
    }
