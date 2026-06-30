# Garaj Baras - radar_bhopal.py
# Bhopal IMD radar GIF management.
# Mirrors the radar_patna.py interface; reuses parameterized helpers from radar.py.

import os
import threading

from radar import (
    download_gif,
    extract_frames,
    gif_is_fresh as _gif_is_fresh,
    clear_frames_folder,
    get_radar_lag_mins,
    RADAR_TTL_SEC,
)

GIF_URL       = "https://mausam.imd.gov.in/Radar/animation/Converted/BHP_MAXZ.gif"
GIF_SAVE_PATH = os.path.join(os.path.dirname(__file__), "bhopal_radar.gif")
FRAMES_FOLDER = os.path.join(os.path.dirname(__file__), "frames_bhopal")

_refresh_lock = threading.Lock()


def gif_is_fresh(ttl_sec=RADAR_TTL_SEC):
    return _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH)


def get_all_frames():
    success, _ = download_gif(GIF_URL, GIF_SAVE_PATH)
    if success:
        return extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
    print("Bhopal radar: GIF download failed.")
    return []


def refresh_frames_if_stale(*, ttl_sec=RADAR_TTL_SEC, force=False, clear_pngs=True):
    if not force and _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH):
        return ([], False)

    with _refresh_lock:
        if not force and _gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH):
            return ([], False)

        success, _ = download_gif(GIF_URL, GIF_SAVE_PATH)
        if not success:
            print("Bhopal radar: GIF download failed.")
            return ([], False)

        if clear_pngs:
            deleted = clear_frames_folder(FRAMES_FOLDER)
            if deleted:
                print(f"Bhopal: cleared {deleted} old frame PNGs")

        frame_data = extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
        return (frame_data, True)
