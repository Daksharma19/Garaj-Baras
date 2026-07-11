# Garaj Baras - radar_patna.py
# Patna IMD radar GIF management.
# Mirrors the radar_lucknow.py interface; reuses parameterized helpers from radar.py.

import os
import threading

from radar import (
    download_gif,
    extract_frames,
    gif_is_fresh as _gif_is_fresh,
    clear_frames_folder,
    get_radar_lag_mins,
    augment_with_current_image as _augment_with_current_image,
    RADAR_TTL_SEC,
)

GIF_URL       = "https://mausam.imd.gov.in/Radar/animation/Converted/PTN_MAXZ.gif"
GIF_SAVE_PATH = os.path.join(os.path.dirname(__file__), "patna_radar.gif")
FRAMES_FOLDER = os.path.join(os.path.dirname(__file__), "frames_patna")

# IMD's single "current radar" image (same 880x720 MAX-Z layout as Delhi, so
# the default OCR/crop boxes apply). Updates ~10 min while the animation GIF
# rebuilds lazily. Appended as newest frame when strictly newer — see radar.py.
CURRENT_IMG_URL       = "https://mausam.imd.gov.in/Radar/caz_ptn.gif"
CURRENT_IMG_SAVE_PATH = os.path.join(os.path.dirname(__file__), "patna_current.gif")


def augment_current(frame_data):
    """Append IMD's current image as the newest frame if strictly newer."""
    return _augment_with_current_image(
        frame_data, FRAMES_FOLDER,
        current_url=CURRENT_IMG_URL,
        current_save_path=CURRENT_IMG_SAVE_PATH,
    )

_refresh_lock = threading.Lock()


def gif_is_fresh(ttl_sec=RADAR_TTL_SEC):
    return _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH)


def get_all_frames():
    success, _ = download_gif(GIF_URL, GIF_SAVE_PATH)
    if success:
        return extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
    print("Patna radar: GIF download failed.")
    return []


def refresh_frames_if_stale(*, ttl_sec=RADAR_TTL_SEC, force=False, clear_pngs=True):
    if not force and _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH):
        return ([], False)

    with _refresh_lock:
        if not force and _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH):
            return ([], False)

        success, _ = download_gif(GIF_URL, GIF_SAVE_PATH)
        if not success:
            print("Patna radar: GIF download failed.")
            return ([], False)

        if clear_pngs:
            deleted = clear_frames_folder(FRAMES_FOLDER)
            if deleted:
                print(f"Patna: cleared {deleted} old frame PNGs")

        frame_data = extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
        return (frame_data, True)
