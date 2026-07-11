# Garaj Baras — radar_scene.py
#
# Builds the compact JSON "scene" behind the v2 radar animation. Instead of
# server-rendered PNG frames (forecast_gif.py), the backend ships DATA and the
# frontend canvas player renders + interpolates it:
#
#   history : the cached radar frames (~10-min cadence, real OCR'd timestamps)
#             as low-res dBZ grids over a crop around the user
#   now     : same grid for the latest frame PLUS an "owner" grid mapping each
#             rain cell to the storm patch that claims it
#   patches : per-patch velocity + decay parameters (mirrors nowcast's
#             _patch_fade rules) so the frontend can advect rain forward
#             continuously for the +0..+60 min forecast half
#
# One scene ≈ 60 KB JSON vs ~500 KB of base64 PNGs from forecast_frames.

import base64
from datetime import datetime, timezone, timedelta

import numpy as np
from PIL import Image

from fuzzy import COLOR_TABLE
from nowcast import (PATCH_SEARCH_RADIUS_PX, NEW_CELL_DECAY_DBZ_PER_10MIN,
                     NEW_CELL_MIN_DBZ, NEW_CELL_MAX_ASSERT_MINS,
                     _find_track_for_patch)

IST = timezone(timedelta(hours=5, minutes=30))

CELL_PX = 2            # grid cell size in radar pixels (0.877 km/px → ~1.75 km)

# View radius of the animation crop (px). Half the 120-px prediction search
# radius — this is only the viewport; predictions still use the full radius.
VIEW_RADIUS_PX = PATCH_SEARCH_RADIUS_PX // 2

# Notable places per radar, drawn as labels on the frontend canvas (like the
# city abbreviations on IMD's own frames, but full readable names).
PLACES = {
    "delhi": [
        ("Delhi", 28.6139, 77.2090), ("Noida", 28.5355, 77.3910),
        ("Gurugram", 28.4595, 77.0266), ("Faridabad", 28.4089, 77.3178),
        ("Ghaziabad", 28.6692, 77.4538), ("Meerut", 28.9845, 77.7064),
        ("Rohtak", 28.8955, 76.6066), ("Sonipat", 28.9950, 77.0230),
        ("Panipat", 29.3909, 76.9635), ("Jind", 29.3160, 76.3150),
        ("Rewari", 28.1990, 76.6170), ("Palwal", 28.1447, 77.3290),
        ("Alwar", 27.5530, 76.6346), ("Mathura", 27.4924, 77.6737),
        ("Aligarh", 27.8974, 78.0880), ("Bulandshahr", 28.4069, 77.8497),
        ("Karnal", 29.6857, 76.9905), ("Muzaffarnagar", 29.4727, 77.7085),
        ("Bijnor", 29.3727, 78.1363),
    ],
    "lucknow": [
        ("Lucknow", 26.8467, 80.9462), ("Sitapur", 27.5680, 80.6790),
        ("Hardoi", 27.3970, 80.1310), ("Sandila", 27.0700, 80.5200),
        ("Barabanki", 26.9260, 81.1840), ("Kanpur", 26.4499, 80.3319),
        ("Unnao", 26.5470, 80.4879), ("Raebareli", 26.2345, 81.2409),
        ("Bahraich", 27.5743, 81.5943), ("Ayodhya", 26.7730, 82.1458),
        ("Kannauj", 27.0550, 79.9190), ("Lakhimpur", 27.9490, 80.7790),
        ("Shahjahanpur", 27.8830, 79.9100),
    ],
    "patna": [
        ("Patna", 25.5941, 85.1376), ("Ara", 25.5560, 84.6600),
        ("Chhapra", 25.7810, 84.7470), ("Hajipur", 25.6860, 85.2100),
        ("Muzaffarpur", 26.1225, 85.3906), ("Bihar Sharif", 25.1970, 85.5140),
        ("Gaya", 24.7955, 84.9994), ("Jehanabad", 25.2130, 84.9870),
        ("Begusarai", 25.4180, 86.1290), ("Samastipur", 25.8630, 85.7810),
    ],
    "jaipur": [
        ("Jaipur", 26.9124, 75.7873), ("Ajmer", 26.4499, 74.6399),
        ("Sikar", 27.6094, 75.1399), ("Alwar", 27.5530, 76.6346),
        ("Tonk", 26.1664, 75.7885), ("Dausa", 26.8932, 76.3367),
        ("Sawai Madhopur", 25.9928, 76.3597), ("Kota", 25.2138, 75.8648),
        ("Bhilwara", 25.3407, 74.6313), ("Kishangarh", 26.5904, 74.8564),
    ],
    "paradip": [
        ("Paradip", 20.3167, 86.6110), ("Cuttack", 20.4625, 85.8828),
        ("Bhubaneswar", 20.2961, 85.8245), ("Kendrapara", 20.5017, 86.4225),
        ("Jagatsinghpur", 20.2548, 86.1710), ("Jajpur", 20.8360, 86.3280),
        ("Bhadrak", 21.0580, 86.5150), ("Puri", 19.8135, 85.8312),
        ("Khordha", 20.1826, 85.6186), ("Dhenkanal", 20.6667, 85.5983),
    ],
    "bhopal": [
        ("Bhopal", 23.2599, 77.4126), ("Sehore", 23.2000, 77.0850),
        ("Vidisha", 23.5251, 77.8081), ("Raisen", 23.3310, 77.7810),
        ("Sanchi", 23.4860, 77.7380), ("Itarsi", 22.6140, 77.7620),
        ("Hoshangabad", 22.7530, 77.7220), ("Ashta", 23.0180, 76.7220),
        ("Dewas", 22.9660, 76.0550), ("Rajgarh", 24.0070, 76.7280),
    ],
}
COLOR_MAX_DIST = 80.0  # same tolerance as fuzzy.rgb_to_dbz
DEAD_DBZ_THRESHOLD = 8.0  # mirrors decay.py / forecast_gif.py

_COLORS = np.array([[r, g, b] for r, g, b, _d in COLOR_TABLE], np.float32)
_DBZS = np.array([d for _r, _g, _b, d in COLOR_TABLE], np.uint8)


def _classify_dbz(rgb_crop: np.ndarray) -> np.ndarray:
    """Vectorized fuzzy.rgb_to_dbz over an HxWx3 uint8 crop → HxW uint8 dBZ."""
    flat = rgb_crop.reshape(-1, 3).astype(np.float32)
    d2 = ((flat[:, None, :] - _COLORS[None, :, :]) ** 2).sum(axis=2)
    idx = d2.argmin(axis=1)
    dbz = _DBZS[idx]
    dbz[d2[np.arange(len(idx)), idx] > COLOR_MAX_DIST ** 2] = 0
    return dbz.reshape(rgb_crop.shape[:2])


def _pool_max(a: np.ndarray, cell: int) -> np.ndarray:
    """Max-pool a 2D array by cell×cell blocks (input padded to a multiple)."""
    h, w = a.shape
    ph, pw = (-h) % cell, (-w) % cell
    if ph or pw:
        a = np.pad(a, ((0, ph), (0, pw)))
    gh, gw = a.shape[0] // cell, a.shape[1] // cell
    return a.reshape(gh, cell, gw, cell).max(axis=(1, 3))


def _grid_b64(grid: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(grid, np.uint8).tobytes()).decode()


def _frame_dbz_grid(frame_path: str, box, cell: int) -> np.ndarray:
    x0, y0, x1, y1 = box
    rgb = np.array(Image.open(frame_path).convert("RGB"))[y0:y1, x0:x1]
    return _pool_max(_classify_dbz(rgb), cell)


def _patch_params(patch: dict, tracks) -> dict:
    """Per-patch motion + decay parameters, mirroring nowcast/_patch_fade."""
    raw = float(patch.get("max_dbz", 0) or 0)
    track = _find_track_for_patch(patch, tracks)
    if track is not None and len(getattr(track, "mean_dbzs", []) or []) >= 2:
        # Frontend player fades linearly; clamp to the same rate bounds the
        # damped backend projection uses so the visual can't wildly diverge.
        from decay import MAX_GROWTH_RATE_DBZ_PER_10MIN, MAX_DECAY_RATE_DBZ_PER_10MIN
        clamped = min(max(float(track.decay_rate), MAX_DECAY_RATE_DBZ_PER_10MIN),
                      MAX_GROWTH_RATE_DBZ_PER_10MIN)
        mode, rate, min_dbz, max_assert = "track", clamped, DEAD_DBZ_THRESHOLD, None
    else:
        mode, rate, min_dbz, max_assert = "new", NEW_CELL_DECAY_DBZ_PER_10MIN, NEW_CELL_MIN_DBZ, NEW_CELL_MAX_ASSERT_MINS
    return {
        "vx": float(patch.get("dx_10", 0.0)),   # px per 10 min
        "vy": float(patch.get("dy_10", 0.0)),
        "raw_dbz": raw,
        "decay_mode": mode,
        "decay_rate": rate,                      # dBZ per 10 min
        "min_dbz": min_dbz,                      # dead below this
        "max_assert_mins": max_assert,           # 'new' cells stop asserting after this
    }


def build_radar_scene(frame_data, latest_rain_mask, patches, tracks,
                      gdx, gdy, lag_mins, user_px, user_py,
                      radius_px: int = VIEW_RADIUS_PX,
                      cell_px: int = CELL_PX,
                      radar_name: str = None,
                      latlon_to_pixel_fn=None) -> dict:
    """
    frame_data       : list of (frame_png_path, ist_datetime), oldest→newest
    latest_rain_mask : HxW rain mask of the newest frame (isolate_rain output)
    patches / tracks : compute_patch_motion / compute_decay_tracks output
    gdx, gdy         : global motion vector, px per 10 min
    """
    if not frame_data:
        raise ValueError("no radar frames available")

    mask = np.asarray(latest_rain_mask) > 0
    h, w = mask.shape
    ux, uy = int(user_px), int(user_py)
    r = int(radius_px)
    x0, x1 = max(0, ux - r), min(w, ux + r)
    y0, y1 = max(0, uy - r), min(h, uy + r)
    box = (x0, y0, x1, y1)

    latest_ts = max(ts for _p, ts in frame_data)

    # --- history: one dBZ grid per cached frame, real timestamps ------------
    # frame_data can contain duplicate timestamps (GIF frames + appended
    # "current image"); keep one frame per timestamp, chronological.
    seen = {}
    for path, ts in frame_data:
        seen[ts] = path
    frames_sorted = sorted(((p, ts) for ts, p in seen.items()), key=lambda x: x[1])

    history = []
    for path, ts in frames_sorted:
        grid = _frame_dbz_grid(path, box, cell_px)
        history.append({
            "mins": round((ts - latest_ts).total_seconds() / 60.0, 1),  # ≤ 0
            "time_ist": ts.strftime("%H:%M"),
            "dbz": _grid_b64(grid),
        })

    gh = -(-(y1 - y0) // cell_px)
    gw = -(-(x1 - x0) // cell_px)

    # --- owner grid: which patch claims each rain cell of the newest frame --
    owner_full = np.zeros((h, w), np.uint8)  # 0 = unclaimed (global drift)
    scene_patches = []
    for i, p in enumerate(patches or []):
        bm = p.get("mask")
        if bm is None or i >= 255:
            continue
        pid = len(scene_patches) + 1
        sub = np.zeros((h, w), bool)
        bm.paint_into(sub)
        owner_full[sub] = pid
        scene_patches.append({"id": pid, **_patch_params(p, tracks)})
    owner_grid = _pool_max(owner_full[y0:y1, x0:x1], cell_px)

    # --- named places inside the crop, in grid coordinates -------------------
    places = []
    if latlon_to_pixel_fn is not None:
        for name, plat, plon in PLACES.get(radar_name or "", []):
            try:
                ppx, ppy = latlon_to_pixel_fn(plat, plon)
            except Exception:
                continue
            if x0 + 2 <= ppx < x1 - 2 and y0 + 2 <= ppy < y1 - 2:
                places.append({"name": name,
                               "gx": round((ppx - x0) / cell_px, 2),
                               "gy": round((ppy - y0) / cell_px, 2)})

    now_ist = datetime.now(IST)
    return {
        "places": places,
        "grid": {"w": gw, "h": gh, "cell_px": cell_px, "km_per_px": 0.877},
        "crop": {"x0": x0, "y0": y0,
                 "user_gx": (ux - x0) / cell_px, "user_gy": (uy - y0) / cell_px,
                 "radius_cells": r / cell_px},
        "history": history,          # oldest → newest; last one is "now"
        "owner": _grid_b64(owner_grid),
        "patches": scene_patches,
        "global": {"vx": float(gdx), "vy": float(gdy)},
        "lag_mins": round(float(lag_mins), 1),
        "generated_ist": now_ist.strftime("%H:%M"),
        "latest_frame_ist": latest_ts.strftime("%H:%M"),
    }
