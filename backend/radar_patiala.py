# Garaj Baras - radar_patiala.py
# Patiala IMD radar GIF management.
# Mirrors the radar_jaipur.py interface; reuses parameterized helpers from radar.py.
#
# Patiala's raw GIF IS the standard 880x720 MAXZ frame, but (like Jaipur/Paradip)
# its map panel sits at the BOTTOM-LEFT (x 0-519, y 200-719) instead of Delhi's
# offset — so it needs its own crop box. The info-panel/timestamp block IS at
# Delhi's position, so the default OCR crop applies (verified on a live frame).
#
# 300 km radar. The 300 km range rings are centered on the station crosshair at
# crop (260, 259); see georef_patiala.py for the matching AEQD fit.
CROP_BOX = (0, 200, 520, 720)

import os
import threading

from radar import (
    download_gif,
    extract_frames as _extract_frames,
    gif_is_fresh as _gif_is_fresh,
    clear_frames_folder,
    get_radar_lag_mins,
    augment_with_current_image as _augment_with_current_image,
    RADAR_TTL_SEC,
)

GIF_URL       = "https://mausam.imd.gov.in/Radar/animation/Converted/PTL_MAXZ.gif"
GIF_SAVE_PATH = os.path.join(os.path.dirname(__file__), "patiala_radar.gif")
FRAMES_FOLDER = os.path.join(os.path.dirname(__file__), "frames_patiala")

# IMD's single "current radar" image (same 880x720 MAX-Z layout) updates ~10
# min while the animation GIF rebuilds lazily. Appended as the newest frame
# when strictly newer — see radar.py.
CURRENT_IMG_URL       = "https://mausam.imd.gov.in/Radar/caz_ptl.gif"
CURRENT_IMG_SAVE_PATH = os.path.join(os.path.dirname(__file__), "patiala_current.gif")


def augment_current(frame_data):
    """Append IMD's current image as the newest frame if strictly newer."""
    return _augment_with_current_image(
        frame_data, FRAMES_FOLDER,
        current_url=CURRENT_IMG_URL,
        current_save_path=CURRENT_IMG_SAVE_PATH,
        crop_box=CROP_BOX,
    )

_refresh_lock = threading.Lock()


def gif_is_fresh(ttl_sec=RADAR_TTL_SEC):
    return _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH)


def extract_frames(gif_path, output_folder):
    """Patiala-specific wrapper: always applies this radar's crop box."""
    return _extract_frames(gif_path, output_folder, crop_box=CROP_BOX)


def get_all_frames():
    success, _ = download_gif(GIF_URL, GIF_SAVE_PATH)
    if success:
        return extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
    # IMD unreachable: serve the last GIF we have — the lag system will
    # honestly report its age. Stale radar beats an empty state.
    if os.path.exists(GIF_SAVE_PATH):
        print("Patiala radar: download failed — using last GIF on disk")
        return extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
    print("Patiala radar: GIF download failed.")
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
                print("Patiala radar: download failed — using last GIF on disk")
                frame_data = extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
                return (frame_data, True)
            print("Patiala radar: GIF download failed.")
            return ([], False)

        if clear_pngs:
            deleted = clear_frames_folder(FRAMES_FOLDER)
            if deleted:
                print(f"Patiala: cleared {deleted} old frame PNGs")

        frame_data = extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
        return (frame_data, True)
