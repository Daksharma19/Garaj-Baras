# Garaj Baras - optical_flow.py

import numpy as np
import cv2
from PIL import Image

# The 10 legend colors from the IMD dBZ scale (matches fuzzy.py's COLOR_TABLE).
# Rain is detected by matching these directly, not by excluding background.
RAIN_PALETTE = np.array([
    (0,   25,  176),  # ~20 dBZ
    (0,   58,  200),  # ~25
    (0,   71,  255),  # ~30
    (26,  163, 255),  # ~35
    (135, 241, 255),  # ~38
    (0,   200, 0),    # ~41
    (255, 255, 0),    # ~44
    (255, 165, 0),    # ~50
    (255, 0,   0),    # ~55
    (255, 255, 255),  # ~60
], dtype=np.int16)


def isolate_rain(frame_path, clutter_mask=None, tolerance=65):
    """
    Returns a mask of rain pixels by matching each pixel to the known dBZ
    legend palette (RAIN_PALETTE). A pixel is rain only if it lies within
    `tolerance` RGB-distance of a real legend color.

    tolerance: lower = stricter. Try 55–75 to tune.
    Returns: numpy array (uint8), 0=no rain, 255=rain
    """
    img = np.array(Image.open(frame_path).convert('RGB')).astype(np.int16)
    h, w, _ = img.shape
    flat = img.reshape(-1, 3)  # (H*W, 3)

    # Chunked nearest-palette distance: the full (N, P, 3) int32 tensor peaks
    # at ~35 MB per call, which matters on 512 MB dynos. Row bands keep the
    # transient under ~2 MB with identical results.
    tol2 = tolerance ** 2
    is_rain = np.empty(flat.shape[0], dtype=bool)
    CHUNK = 16384
    for i in range(0, flat.shape[0], CHUNK):
        part = flat[i:i + CHUNK]
        diff = part[:, None, :] - RAIN_PALETTE[None, :, :]    # (chunk, P, 3)
        dist2 = np.sum(diff.astype(np.int32) ** 2, axis=2)    # (chunk, P)
        is_rain[i:i + CHUNK] = dist2.min(axis=1) <= tol2

    rain_mask = np.where(is_rain, 255, 0).astype(np.uint8).reshape(h, w)

    # Remove thin map furniture (range rings, text, station labels) that share
    # colors with the dBZ palette. Real rain is blobby and survives a small open.
    _k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    rain_mask = cv2.morphologyEx(rain_mask, cv2.MORPH_OPEN, _k, iterations=1)

    return rain_mask


def build_clutter_mask(frames_list, threshold=0.75):
    """
    Identifies permanent ground clutter pixels by analyzing all radar frames.

    A pixel is clutter if it appears as "rain" in MORE THAN threshold (80%) of frames.
    Real rain moves between frames; clutter stays fixed across all frames.

    Args:
        frames_list : list of all frame paths (use all 16 frames)
        threshold   : fraction of frames a pixel must be "rain" in to be clutter (default 0.80)

    Returns: clutter_mask (numpy array, uint8, 255=clutter, 0=not clutter)
    """
    if not frames_list:
        return None

    # Stack rain masks — without clutter correction (raw masks).
    # Dilate each frame's mask by 1 px before stacking so that a stationary
    # label that wobbles ±1 px between scans still registers as "present" in
    # every frame and crosses the 0.6 threshold.
    masks = []
    for path in frames_list:
        m = (isolate_rain(path, clutter_mask=None) > 0).astype(np.uint8)
        masks.append(m > 0)

    stacked = np.stack(masks, axis=0)  # shape: (N, H, W)
    rain_fraction = stacked.mean(axis=0)  # 0.0–1.0 per pixel

    clutter_mask = np.where(rain_fraction > threshold, 255, 0).astype(np.uint8)
    return clutter_mask


def calculate_optical_flow(frame1_path, frame2_path, clutter_mask=None):
    """
    Calculates dense optical flow between two consecutive radar frames.
    Only considers pixels where rain exists in the first frame.
    Returns: (mean_dx, mean_dy) in pixels.
    """
    rain_mask = isolate_rain(frame1_path, clutter_mask=clutter_mask)

    img1 = np.array(Image.open(frame1_path).convert('L'))
    img2 = np.array(Image.open(frame2_path).convert('L'))

    flow = cv2.calcOpticalFlowFarneback(
        img1, img2, None,
        pyr_scale=0.5, levels=3, winsize=15,
        iterations=3, poly_n=5, poly_sigma=1.2, flags=0
    )

    rain_pixels = rain_mask > 0
    if rain_pixels.sum() == 0:
        return (0.0, 0.0)

    mean_dx = float(np.mean(flow[:, :, 0][rain_pixels]))
    mean_dy = float(np.mean(flow[:, :, 1][rain_pixels]))
    return (mean_dx, mean_dy)


def get_direction_string(dx, dy):
    """
    Converts (dx, dy) movement to 8-point compass direction.
    dx positive=East, dy positive=South.
    """
    angle = np.degrees(np.arctan2(dy, dx)) % 360
    bearing = (angle + 90) % 360
    directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return directions[int((bearing + 22.5) / 45) % 8]


def get_movement_vector(frame_data, clutter_mask=None):
    """
    Calculates average rain movement across all consecutive frame pairs.

    Builds a clutter mask from ALL provided frames first (if not supplied),
    then uses clutter-cleaned masks for optical flow.

    Args:
        frame_data: list of (frame_path, timestamp_or_None)

    Uses OCR timestamps to compute the real time gap between consecutive frames.
    - Skips pairs with gap > 20 mins
    - Uses assumed gap=10 mins if OCR timestamp missing

    Returns dx/dy normalized to a 10-minute movement unit, so that
    downstream shifting via minutes_ahead/10.0 remains correct.

    Discards pairs where movement > 30 px (optical flow noise).

    Returns: (mean_dx, mean_dy, direction_from, direction_to, speed_kmh)
    """
    if len(frame_data) < 2:
        return (0.0, 0.0, "N", "S", 0.0)

    valid_vectors = []  # (dx_10, dy_10, speed_kmh)

    for i in range(len(frame_data) - 1):
        path1, ts1 = frame_data[i]
        path2, ts2 = frame_data[i + 1]

        # Check time gap
        if ts1 is not None and ts2 is not None:
            gap_mins = (ts2 - ts1).total_seconds() / 60.0

            if gap_mins < 0:
                print(f"  SKIP (time went backward): frame_{i:02d}->{i+1:02d}")
                continue
            if gap_mins > 120:
                # Truly stale pair — skip only if gap is absurdly large
                print(f"  SKIP (gap={gap_mins:.0f}mins, > 2hr): frame_{i:02d}->{i+1:02d}")
                continue

            print(f"  USE gap={gap_mins:.0f}mins: frame_{i:02d}->{i+1:02d}")
        else:
            # No timestamps - assume 10 min gap
            gap_mins = 10.0
            print(f"  USE gap=10mins(assumed): frame_{i:02d}->{i+1:02d}")

        # Calculate optical flow
        dx, dy = calculate_optical_flow(
            path1,
            path2,
            clutter_mask=clutter_mask
        )

        magnitude = (dx ** 2 + dy ** 2) ** 0.5
        if magnitude < 0.5:
            print(f"  SKIP (no rain) mag={magnitude:.2f}px: frame_{i:02d}->{i+1:02d}")
            continue
        if magnitude > 30:
            print(f"  SKIP (noise)   mag={magnitude:.1f}px: frame_{i:02d}->{i+1:02d}")
            continue

        # Calculate REAL speed using real time gap
        km_moved = magnitude * 0.877
        speed_kmh = (km_moved / gap_mins) * 60.0

        # Normalize dx/dy to a 10-min unit for downstream predictions.
        dx_10 = dx * (10.0 / gap_mins)
        dy_10 = dy * (10.0 / gap_mins)

        print(
            f"  OK dx={dx:.2f} dy={dy:.2f} mag={magnitude:.1f}px "
            f"speed={speed_kmh:.1f}km/h (gap={gap_mins:.1f}m)"
        )
        valid_vectors.append((dx_10, dy_10, speed_kmh))

    if not valid_vectors:
        return (0.0, 0.0, "Unknown", "Unknown", 0.0)

    # Weight recent pairs more
    weights = list(range(1, len(valid_vectors) + 1))
    total_w = sum(weights)

    avg_dx = sum(v[0] * w for v, w in zip(valid_vectors, weights)) / total_w
    avg_dy = sum(v[1] * w for v, w in zip(valid_vectors, weights)) / total_w
    avg_speed = sum(v[2] * w for v, w in zip(valid_vectors, weights)) / total_w

    direction_to = get_direction_string(avg_dx, avg_dy)
    opposite = {"N":"S","NE":"SW","E":"W","SE":"NW","S":"N","SW":"NE","W":"E","NW":"SE"}
    direction_from = opposite[direction_to]

    return (avg_dx, avg_dy, direction_from, direction_to, avg_speed)


if __name__ == "__main__":
    from radar import get_recent_frames, get_all_frames
    all_frame_data = get_all_frames()
    recent_frame_data = get_recent_frames(n=6)

    all_paths = [p for (p, _ts) in all_frame_data]
    print(f"Building clutter mask from {len(all_paths)} frames...")
    clutter_mask  = build_clutter_mask(all_paths)
    clutter_pixels = int((clutter_mask > 0).sum())
    print(f"Clutter pixels identified: {clutter_pixels}")
    print()

    dx, dy, direction_from, direction_towards, speed = get_movement_vector(
        recent_frame_data, clutter_mask=clutter_mask
    )
    print(f"Rain coming FROM: {direction_from}")
    print(f"Rain moving TO:   {direction_towards}")
    print(f"Approx speed:     {speed:.1f} km/h")
