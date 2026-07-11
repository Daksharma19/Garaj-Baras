# Garaj Baras - radar_lucknow.py
# Lucknow IMD radar GIF management.
# Mirrors the radar.py public interface; reuses parameterized helpers from radar.py.
#
# Lucknow GIF is 704x594 (NOT the standard 880x720).
#
# OCR crop must start BELOW the "Max Range:250 km" line — including it makes
# Tesseract drop the timestamp lines entirely (verified live: the old
# (530,165,...) crop OCR'd as just '= Max Range:250 km' and every frame's
# timestamp came back Unknown, forcing the 25-min default lag).

# Lucknow-specific OCR crop (x0,y0,x1,y1) in full-GIF coordinates (704x594):
# captures the '14:22:10Z / 5 JUL 2026 UTC / 19:52:10 IST' block only.
_OCR_CROP = (540, 175, 704, 268)

# Lucknow's radar-circle crop, in full-GIF coordinates (704x594 raw frame —
# NOT 880x720 like Delhi, and the circle sits at a different offset). Measured
# directly off the green-disc bounding box in a live frame: x 1-391, y 177-567.
# Previously this radar reused radar.py's Delhi-tuned box (0,125,527,650),
# which on this smaller/differently-laid-out frame pulled in the RHI/legend
# panel on the right and black-padded the bottom 56px — desyncing every pixel
# from georef_lucknow.py's fit. See georef_lucknow.py for the matching GCPs.
CROP_BOX = (0, 176, 392, 568)

import os
import threading

from radar import (
    download_gif,
    extract_frames as _extract_frames,
    gif_is_fresh as _gif_is_fresh,
    clear_frames_folder,
    get_radar_lag_mins,
    RADAR_TTL_SEC,
)

GIF_URL       = "https://mausam.imd.gov.in/Radar/animation/Converted/LKN_MAXZ.gif"
GIF_SAVE_PATH = os.path.join(os.path.dirname(__file__), "lucknow_radar.gif")
FRAMES_FOLDER = os.path.join(os.path.dirname(__file__), "frames_lucknow")

_refresh_lock = threading.Lock()


def gif_is_fresh(ttl_sec=RADAR_TTL_SEC):
    return _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH)


def extract_frames(gif_path, output_folder):
    """Lucknow-specific wrapper: always applies this radar's OCR/crop boxes."""
    return _extract_frames(gif_path, output_folder, ocr_crop=_OCR_CROP, crop_box=CROP_BOX)


def get_all_frames():
    success, _ = download_gif(GIF_URL, GIF_SAVE_PATH)
    if success:
        return extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
    # IMD unreachable: serve the last GIF we have — the lag system will
    # honestly report its age. Stale radar beats an empty state.
    if os.path.exists(GIF_SAVE_PATH):
        print("Lucknow radar: download failed — using last GIF on disk")
        return extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
    print("Lucknow radar: GIF download failed.")
    return []


def refresh_frames_if_stale(*, ttl_sec=RADAR_TTL_SEC, force=False, clear_pngs=True):
    if not force and _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH):
        return ([], False)

    with _refresh_lock:
        if not force and _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH):
            return ([], False)

        success, _ = download_gif(GIF_URL, GIF_SAVE_PATH)
        if not success:
            if os.path.exists(GIF_SAVE_PATH):
                print("Lucknow radar: download failed — using last GIF on disk")
                frame_data = extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
                return (frame_data, True)
            print("Lucknow radar: GIF download failed.")
            return ([], False)

        if clear_pngs:
            deleted = clear_frames_folder(FRAMES_FOLDER)
            if deleted:
                print(f"Lucknow: cleared {deleted} old frame PNGs")

        frame_data = extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
        return (frame_data, True)
