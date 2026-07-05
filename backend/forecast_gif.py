# Garaj Baras — forecast_gif.py
#
# Renders a 1-hour FORECAST radar animation around a point: the same
# simulation the nowcast/route prediction runs internally, drawn as frames.
#
#   frame 0 : now            (latest radar, lag-corrected)
#   frame 1 : +15 min
#   frame 2 : +30 min
#   frame 3 : +45 min
#   frame 4 : +60 min
#
# Faithful to the prediction system:
#   - each patch advected by its OWN motion vector (dx_10/dy_10)
#   - intensity faded by its decay track; single-observation cells use the
#     synthetic new-cell lifecycle (-2.5 dBZ / 10 min, dead below 10 dBZ)
#   - rain not claimed by any patch drifts with the global vector
#
# The view is cropped to the nowcast patch-search circle around the user.

import io
import math
from datetime import datetime, timezone, timedelta

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from nowcast import (PATCH_SEARCH_RADIUS_PX, NEW_CELL_DECAY_DBZ_PER_10MIN,
                     NEW_CELL_MIN_DBZ, _find_track_for_patch)

IST = timezone(timedelta(hours=5, minutes=30))

SLOTS_MINS = [0, 15, 30, 45, 60]
FRAME_DURATION_MS = 900
UPSCALE = 2
MIN_FADE = 0.30  # never fade surviving rain below this visibility


def _patch_fade(patch, tracks, eff_mins):
    """
    Visibility multiplier for a patch at eff_mins after the frame time.
    Returns 0.0 when the prediction system considers the cell dead.
    Mirrors nowcast: measured decay trend when the track has history,
    synthetic new-cell lifecycle otherwise.
    """
    raw = float(patch.get("max_dbz", 0) or 0)
    if raw <= 0:
        return 1.0
    track = _find_track_for_patch(patch, tracks)
    if track is not None and len(getattr(track, "mean_dbzs", []) or []) >= 2:
        proj = raw + float(track.decay_rate) * (eff_mins / 10.0)
        if proj < 8.0:       # DEAD_DBZ_THRESHOLD
            return 0.0
    else:
        proj = raw + NEW_CELL_DECAY_DBZ_PER_10MIN * (eff_mins / 10.0)
        if proj < NEW_CELL_MIN_DBZ:
            return 0.0
    return max(MIN_FADE, min(1.0, proj / raw))


def render_forecast_frames(frame_rgb, rain_mask, patches, tracks,
                           gdx, gdy, lag_mins, user_px, user_py,
                           radius_px=PATCH_SEARCH_RADIUS_PX):
    """
    Renders the 1-hour forecast around (user_px, user_py).
    Returns list of (slot_mins, label, PIL RGB image).

    frame_rgb : HxWx3 uint8 of the latest radar frame
    rain_mask : HxW uint8/bool rain mask of the latest frame
    patches   : patch dicts from compute_patch_motion (BBoxMask 'mask')
    tracks    : PatchTrack list from compute_decay_tracks
    gdx, gdy  : global motion vector (px per 10 min)
    lag_mins  : radar lag at request time
    """
    h, w = frame_rgb.shape[:2]
    ux, uy = int(user_px), int(user_py)
    r = int(radius_px)

    # Crop box around the user (clamped to the frame)
    x0, x1 = max(0, ux - r), min(w, ux + r)
    y0, y1 = max(0, uy - r), min(h, uy + r)

    rain_bool = np.asarray(rain_mask) > 0

    # Background = latest frame with today's rain wiped out (the forecast
    # repaints rain where the simulation moves it).
    crop_bg = frame_rgb[y0:y1, x0:x1]
    crop_rain = rain_bool[y0:y1, x0:x1]
    if (~crop_rain).sum() > 0:
        fill = np.median(crop_bg[~crop_rain].reshape(-1, 3), axis=0).astype(np.uint8)
    else:
        fill = np.array([30, 42, 34], np.uint8)
    background = frame_rgb.copy()
    background[rain_bool] = fill

    # Rain pixels not claimed by any patch drift with the global vector
    union = np.zeros((h, w), bool)
    for p in patches or []:
        m = p.get("mask")
        if m is not None:
            m.paint_into(union)
    lo_ys, lo_xs = np.nonzero(rain_bool & ~union)

    try:
        font = ImageFont.truetype("arial.ttf", 15)
        font_small = ImageFont.truetype("arial.ttf", 12)
    except Exception:
        font = ImageFont.load_default()
        font_small = font

    now_ist = datetime.now(IST)
    frames_out = []

    for t in SLOTS_MINS:
        eff = float(lag_mins) + t
        shifts = eff / 10.0
        canvas = background.copy()

        # Global-drift leftovers
        if len(lo_ys):
            dxs = (lo_xs + gdx * shifts).round().astype(int)
            dys = (lo_ys + gdy * shifts).round().astype(int)
            ok = (dxs >= 0) & (dxs < w) & (dys >= 0) & (dys < h)
            src_c = frame_rgb[lo_ys[ok], lo_xs[ok]].astype(np.float32)
            blended = (src_c * 0.9 + fill.astype(np.float32) * 0.1).astype(np.uint8)
            canvas[dys[ok], dxs[ok]] = blended

        # Per-patch advection with decay fade
        for p in patches or []:
            bm = p.get("mask")
            if bm is None:
                continue
            fade = _patch_fade(p, tracks, eff)
            if fade <= 0.0:
                continue
            vx = float(p.get("dx_10", 0.0))
            vy = float(p.get("dy_10", 0.0))
            ys, xs = bm.nonzero_full()
            dxs = (xs + vx * shifts).round().astype(int)
            dys = (ys + vy * shifts).round().astype(int)
            ok = (dxs >= 0) & (dxs < w) & (dys >= 0) & (dys < h)
            src_c = frame_rgb[ys[ok], xs[ok]].astype(np.float32)
            blended = (src_c * fade + fill.astype(np.float32) * (1.0 - fade)).astype(np.uint8)
            canvas[dys[ok], dxs[ok]] = blended

        # Crop, upscale, annotate
        img = Image.fromarray(canvas[y0:y1, x0:x1]).resize(
            ((x1 - x0) * UPSCALE, (y1 - y0) * UPSCALE), Image.NEAREST)
        draw = ImageDraw.Draw(img, "RGBA")

        cx, cy = (ux - x0) * UPSCALE, (uy - y0) * UPSCALE
        cr = r * UPSCALE - 2
        # Patch-search circle (the exact radius the nowcast scans)
        draw.ellipse([cx - cr, cy - cr, cx + cr, cy + cr],
                     outline=(96, 165, 250, 200), width=2)
        # User marker
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(255, 255, 255, 255))
        draw.ellipse([cx - 9, cy - 9, cx + 9, cy + 9],
                     outline=(255, 255, 255, 180), width=2)

        # Labels
        slot_time = (now_ist + timedelta(minutes=t)).strftime("%H:%M")
        title = f"Now · {slot_time} IST" if t == 0 else f"+{t} min · {slot_time} IST"
        pad = 6
        tw = draw.textlength(title, font=font)
        draw.rectangle([8, 8, 8 + tw + 2 * pad, 36], fill=(5, 16, 31, 210))
        draw.text((8 + pad, 12), title, fill=(255, 255, 255, 255), font=font)

        tag = "FORECAST" if t > 0 else f"NOW · RADAR +{lag_mins:.0f}m LAG"
        tw2 = draw.textlength(tag, font=font_small)
        gw, gh = img.size
        draw.rectangle([gw - tw2 - 2 * pad - 8, 8, gw - 8, 32], fill=(5, 16, 31, 210))
        draw.text((gw - tw2 - pad - 8, 12), tag,
                  fill=(96, 165, 250, 255) if t > 0 else (74, 222, 128, 255),
                  font=font_small)

        frames_out.append((t, title, img))

    return frames_out


def render_forecast_gif(frame_rgb, rain_mask, patches, tracks,
                        gdx, gdy, lag_mins, user_px, user_py,
                        radius_px=PATCH_SEARCH_RADIUS_PX):
    """Animated-GIF bytes of render_forecast_frames output."""
    frames = render_forecast_frames(frame_rgb, rain_mask, patches, tracks,
                                    gdx, gdy, lag_mins, user_px, user_py,
                                    radius_px=radius_px)
    pal = [img.convert("P", palette=Image.ADAPTIVE) for _t, _l, img in frames]
    buf = io.BytesIO()
    pal[0].save(
        buf, format="GIF", save_all=True, append_images=pal[1:],
        duration=[1200] + [FRAME_DURATION_MS] * (len(pal) - 1),
        loop=0, optimize=False,
    )
    return buf.getvalue()


def render_forecast_frames_payload(frame_rgb, rain_mask, patches, tracks,
                                   gdx, gdy, lag_mins, user_px, user_py,
                                   radius_px=PATCH_SEARCH_RADIUS_PX):
    """
    JSON-ready payload: frames as base64 PNG data-URLs, for the frontend
    player (pause/play/scrub — a GIF can't be paused).
    """
    import base64
    frames = render_forecast_frames(frame_rgb, rain_mask, patches, tracks,
                                    gdx, gdy, lag_mins, user_px, user_py,
                                    radius_px=radius_px)
    out = []
    for t, label, img in frames:
        b = io.BytesIO()
        img.save(b, format="PNG", optimize=True)
        out.append({
            "slot_mins": t,
            "label": label,
            "data": "data:image/png;base64," + base64.b64encode(b.getvalue()).decode(),
        })
    return {"frames": out, "interval_ms": FRAME_DURATION_MS, "lag_mins": round(float(lag_mins), 1)}
