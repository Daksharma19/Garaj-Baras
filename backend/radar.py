# Garaj Baras - radar.py

import os
import threading
import time
import requests
from PIL import Image, ImageSequence
from datetime import datetime, timezone, timedelta

GIF_URL = "https://mausam.imd.gov.in/Radar/animation/Converted/DELHI_MAXZ.gif"
GIF_SAVE_PATH = os.path.join(os.path.dirname(__file__), "delhi_radar.gif")
FRAMES_FOLDER = os.path.join(os.path.dirname(__file__), "frames")

IST = timezone(timedelta(hours=5, minutes=30))

RADAR_TTL_SEC = 10 * 60  # 10 minutes
_refresh_lock = threading.Lock()


def _gif_age_seconds(gif_path: str) -> float:
    """
    Returns file age in seconds, or +inf if missing/unreadable.
    """
    try:
        if not os.path.exists(gif_path):
            return float("inf")
        mtime = os.path.getmtime(gif_path)
        return max(0.0, time.time() - float(mtime))
    except Exception:
        return float("inf")


def gif_is_fresh(ttl_sec: float = RADAR_TTL_SEC, gif_path: str = GIF_SAVE_PATH) -> bool:
    """
    True if gif exists and is newer than ttl_sec.
    """
    return _gif_age_seconds(gif_path) < float(ttl_sec)


def clear_frames_folder(frames_folder: str = FRAMES_FOLDER) -> int:
    """
    Delete all .png images in frames_folder. Returns number deleted.
    """
    deleted = 0
    try:
        if not os.path.exists(frames_folder):
            return 0
        for name in os.listdir(frames_folder):
            if name.lower().endswith(".png"):
                fp = os.path.join(frames_folder, name)
                try:
                    os.remove(fp)
                    deleted += 1
                except Exception:
                    # Best-effort cleanup: ignore individual delete failures
                    pass
    except Exception:
        return deleted
    return deleted


def download_gif(url, save_path):
    """
    Download the radar GIF from IMD.
    Returns (True, None) on success.
    """
    try:
        # Cache-bust: some networks/proxies may serve a cached GIF for the bare URL.
        # IMD servers tolerate a dummy querystring; it helps ensure we get the latest bytes.
        cache_bust_url = url
        if "?" in url:
            cache_bust_url = f"{url}&_={int(time.time())}"
        else:
            cache_bust_url = f"{url}?_={int(time.time())}"

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        response = requests.get(cache_bust_url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()
        
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        return True, None
    except Exception as e:
        print(f"Error downloading GIF: {e}")
        return False, None


# Full IMD frame is 880x720. Crop must match georef.py IMAGE_WIDTH x IMAGE_HEIGHT (527x525).
CROP_TOP    = 125          # Remove top brown panel
CROP_RIGHT  = 527          # left=0 → width 527 (Delhi radar circle, matches georef)
CROP_BOTTOM = 650          # top=125 → height 525


def extract_frames(gif_path, output_folder, ocr_crop=None):
    """
    Extract all frames from animated GIF.

    For each GIF frame:
    - OCR the timestamp from the exact RIGHT-panel coordinates (before cropping)
    - Crop the radar-circle region and save it as `frame_XX.png`

    Returns:
        frame_data: list of (frame_path, timestamp_or_None)
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    frame_data = []
    try:
        import re

        # OCR init (slow first time; cached by Tesseract/pytesseract)
        ocr_available = False
        pytesseract = None
        try:
            import pytesseract as _pytesseract
            pytesseract = _pytesseract
            pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
            ocr_available = True
        except Exception:
            ocr_available = False

        # Pass 1: decode every GIF frame and drop byte-identical consecutive duplicates.
        # IMD regularly pads the GIF with repeated frames; feeding them to optical flow
        # produces zero-motion vectors and wastes OCR time.
        unique_frames = []   # list of (full_rgb, radar_crop_image)
        last_crop_bytes = None
        skipped = 0

        with Image.open(gif_path) as im:
            for frame in ImageSequence.Iterator(im):
                full = frame.convert('RGB')
                radar_crop = full.crop((0, CROP_TOP, CROP_RIGHT, CROP_BOTTOM))
                crop_bytes = radar_crop.tobytes()
                if crop_bytes == last_crop_bytes:
                    skipped += 1
                    continue
                last_crop_bytes = crop_bytes
                unique_frames.append((full, radar_crop))

        if skipped:
            print(f"Dropped {skipped} byte-identical duplicate frame(s) ({len(unique_frames)} unique)")

        # Pass 2: OCR last unique frame, save all unique frames as PNGs.
        # Perf: OCR is expensive (~hundreds of ms). We only need the last frame's
        # timestamp; the repair loop below back-fills the rest at cadence.
        def valid_hms(hh, mm, ss):
            return 0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60

        for idx, (full, frame_cropped) in enumerate(unique_frames):
            timestamp = None
            is_last = (idx == len(unique_frames) - 1)

            if is_last and ocr_available and pytesseract is not None:
                try:
                    from PIL import ImageEnhance

                    _ocr_box = ocr_crop if ocr_crop is not None else (635, 215, 875, 355)
                    ts_crop = full.crop(_ocr_box)
                    w, h = ts_crop.size
                    ts_large = ts_crop.resize((w * 3, h * 3), Image.LANCZOS)
                    ts_gray = ts_large.convert('L')
                    enhancer = ImageEnhance.Contrast(ts_gray)
                    ts_ready = enhancer.enhance(2.0)

                    text = pytesseract.image_to_string(
                        ts_ready,
                        config='--psm 6'
                    ).strip()

                    upper = text.upper()

                    # Prefer UTC time; OCR sometimes misreads trailing Z as 2 or S.
                    utc_match = re.search(
                        r'(\d{1,2}):(\d{2}):(\d{2})\s*[Z2S]',
                        upper
                    )
                    # IST line: anchored to "IST" keyword to avoid matching the UTC line
                    ist_match = re.search(
                        r'(\d{1,2}):(\d{2}):(\d{2})\s+IST',
                        upper
                    )

                    if utc_match:
                        h_t, m_t, s_t = map(int, utc_match.groups())
                        if valid_hms(h_t, m_t, s_t):
                            today = datetime.now(timezone.utc).date()
                            dt_utc = datetime(
                                today.year, today.month, today.day,
                                h_t, m_t, s_t,
                                tzinfo=timezone.utc
                            )
                            timestamp = dt_utc.astimezone(IST)
                    elif ist_match:
                        h_t, m_t, s_t = map(int, ist_match.groups())
                        if valid_hms(h_t, m_t, s_t):
                            today = datetime.now(IST).date()
                            timestamp = datetime(
                                today.year, today.month, today.day,
                                h_t, m_t, s_t,
                                tzinfo=IST
                            )
                    else:
                        # Fallback: parse the first HH:MM:SS token.
                        # Decide timezone by presence of the 'UTC' word.
                        t_match = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', upper)
                        if t_match:
                            h_t, m_t, s_t = map(int, t_match.groups())
                            if valid_hms(h_t, m_t, s_t):
                                today_utc = datetime.now(timezone.utc).date()
                                today_ist = datetime.now(IST).date()
                                if 'UTC' in upper:
                                    dt_utc = datetime(
                                        today_utc.year, today_utc.month, today_utc.day,
                                        h_t, m_t, s_t,
                                        tzinfo=timezone.utc
                                    )
                                    timestamp = dt_utc.astimezone(IST)
                                else:
                                    timestamp = datetime(
                                        today_ist.year, today_ist.month, today_ist.day,
                                        h_t, m_t, s_t,
                                        tzinfo=IST
                                    )
                except Exception:
                    timestamp = None

            frame_filename = f"frame_{idx:02d}.png"
            frame_path = os.path.join(output_folder, frame_filename)
            frame_cropped.save(frame_path)

            frame_data.append((frame_path, timestamp))

            if idx == 0:
                print("Delhi crop frame size: %s" % (frame_cropped.size,))
    except Exception as e:
        print(f"Error extracting frames: {e}")

    # Post-process timestamps to make them usable for motion estimation.
    # OCR can occasionally misread minutes/hours and create huge or zero gaps,
    # which would otherwise cause optical-flow to skip too many frame pairs.
    # IMD radar scans are ~10 minutes apart, so we enforce a ~10-min cadence.
    assumed_step_mins = 10.0
    try:
        timestamps = [ts for (_fp, ts) in frame_data]
        if any(ts is not None for ts in timestamps):
            # Find last frame with a timestamp
            last_valid = max(i for i, ts in enumerate(timestamps) if ts is not None)

            # Backward fill/repair up to first frame
            for i in range(last_valid - 1, -1, -1):
                next_ts = timestamps[i + 1]
                if next_ts is None:
                    continue
                if timestamps[i] is None:
                    timestamps[i] = next_ts - timedelta(minutes=assumed_step_mins)
                    continue
                gap_mins = (next_ts - timestamps[i]).total_seconds() / 60.0
                # Accept only reasonable gaps; otherwise enforce ~10 minutes
                if gap_mins < 5 or gap_mins > 20:
                    timestamps[i] = next_ts - timedelta(minutes=assumed_step_mins)

            # Forward fill/repair after last_valid
            for i in range(last_valid + 1, len(timestamps)):
                prev_ts = timestamps[i - 1]
                if prev_ts is None:
                    continue
                if timestamps[i] is None:
                    timestamps[i] = prev_ts + timedelta(minutes=assumed_step_mins)
                    continue
                gap_mins = (timestamps[i] - prev_ts).total_seconds() / 60.0
                if gap_mins < 5 or gap_mins > 20:
                    timestamps[i] = prev_ts + timedelta(minutes=assumed_step_mins)

            # Write repaired timestamps back
            frame_data = [(fp, timestamps[i]) for i, (fp, _ts) in enumerate(frame_data)]
    except Exception:
        # If repair fails, keep original OCR timestamps.
        pass

    for i, (_fp, ts) in enumerate(frame_data):
        ts_str = ts.strftime('%H:%M:%S') if ts else 'Unknown'
        print(f"Frame {i:02d}: {ts_str}")

    return frame_data


def get_latest_frames():
    """Download fresh GIF and extract all frames with timestamps."""
    success, _ = download_gif(GIF_URL, GIF_SAVE_PATH)
    if success:
        return extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
    else:
        print("Failed to download radar GIF.")
        return []


def get_all_frames():
    """Alias for get_latest_frames."""
    return get_latest_frames()


def get_recent_frames(n=6):
    """Download fresh GIF and return only the LAST n frames with timestamps."""
    frame_data = get_all_frames()
    if len(frame_data) <= n:
        return frame_data
    return frame_data[-n:]


def refresh_frames_if_stale(
    *,
    ttl_sec: float = RADAR_TTL_SEC,
    force: bool = False,
    clear_pngs: bool = True,
) -> tuple[list[tuple[str, datetime | None]], bool]:
    """
    Lazy-cache refresh manager.

    - If delhi_radar.gif is fresh (< ttl_sec) and force=False, does nothing and
      returns ([], did_refresh=False). Caller should use its in-memory cache.
    - If stale/missing (or force=True), refreshes:
        - downloads latest GIF
        - clears existing frame PNGs (if clear_pngs)
        - extracts fresh frames + OCR timestamps
      Returns (frame_data, did_refresh=True).

    Concurrency safety:
      Uses a process-local lock so only one request refreshes at a time.
    """
    if not force and gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH):
        return ([], False)

    with _refresh_lock:
        # Re-check inside lock to avoid double refresh
        if not force and gif_is_fresh(ttl_sec=ttl_sec, gif_path=GIF_SAVE_PATH):
            return ([], False)

        success, _ = download_gif(GIF_URL, GIF_SAVE_PATH)
        if not success:
            print("Failed to download radar GIF.")
            return ([], False)

        if clear_pngs:
            deleted = clear_frames_folder(FRAMES_FOLDER)
            if deleted:
                print(f"Cleared {deleted} old frame PNGs")

        frame_data = extract_frames(GIF_SAVE_PATH, FRAMES_FOLDER)
        return (frame_data, True)


def extract_timestamp_from_gif():
    """
    Extracts timestamp directly from radar GIF.
    Timestamp is at exact location:
    X: 614-820, Y: 230-310
    In full 880x720 frame (BEFORE cropping)
    """
    try:
        import pytesseract
    except ImportError:
        # Render Linux won't have pytesseract installed (or it may be missing).
        print("pytesseract not available - using fallback")
        return None

    import re
    from datetime import datetime, timezone, timedelta
    from PIL import ImageEnhance

    IST = timezone(timedelta(hours=5, minutes=30))

    try:
        # On Windows we can point to the bundled/installed tesseract binary.
        # On Render Linux there is no binary; in that case the OCR call will fail,
        # and we must fall back safely.
        if os.name == "nt":
            pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

        # Load GIF and get LAST frame (most recent)
        gif = Image.open(GIF_SAVE_PATH)
        frames = list(ImageSequence.Iterator(gif))
        last_frame = frames[-1].convert('RGB')

        # Crop the right info panel — wide enough to capture full timestamp text
        ts_crop = last_frame.crop((635, 215, 875, 355))

        # Scale up 3x for better OCR accuracy
        w, h = ts_crop.size
        ts_large = ts_crop.resize((w * 3, h * 3), Image.LANCZOS)
        ts_gray = ts_large.convert('L')
        enhancer = ImageEnhance.Contrast(ts_gray)
        ts_ready = enhancer.enhance(2.0)

        # Read text
        text = pytesseract.image_to_string(
            ts_ready,
            config='--psm 6'
        ).strip()

        print(f"OCR raw text: {repr(text)}")

        # 1. Try UTC line: HH:MM:SSZ  (OCR sometimes misreads Z as 2 or S)
        utc_match = re.search(
            r'(\d{1,2}):(\d{2}):(\d{2})\s*[Zz2S]',
            text,
        )
        if utc_match:
            h, m, s = map(int, utc_match.groups())
            today = datetime.now(timezone.utc).date()
            dt_utc = datetime(
                today.year, today.month, today.day,
                h, m, s, tzinfo=timezone.utc
            )
            dt_ist = dt_utc.astimezone(IST)
            print(f"Timestamp extracted (UTC->IST): {dt_ist.strftime('%H:%M:%S IST')}")
            return dt_ist

        # 2. Try IST line explicitly: HH:MM:SS IST
        ist_match = re.search(
            r'(\d{1,2}):(\d{2}):(\d{2})\s+[Ii][Ss][Tt]',
            text
        )
        if ist_match:
            h, m, s = map(int, ist_match.groups())
            today = datetime.now(IST).date()
            dt_ist = datetime(
                today.year, today.month, today.day,
                h, m, s, tzinfo=IST
            )
            print(f"Timestamp extracted (IST): {dt_ist.strftime('%H:%M:%S IST')}")
            return dt_ist

        print("OCR failed - using 25 min fallback")
        return None
    except Exception as e:
        print(f"OCR failed: {e} - using fallback")
        return None


def get_radar_lag_mins(latest_timestamp=None):
    """
    Computes radar lag (minutes old) using the OCR-extracted timestamp.

    If `latest_timestamp` is not provided or is invalid, falls back to a conservative 25 min.
    """
    from datetime import datetime, timezone, timedelta

    IST = timezone(timedelta(hours=5, minutes=30))

    radar_time = latest_timestamp
    if radar_time is None:
        # Fallback: OCR just the last frame
        radar_time = extract_timestamp_from_gif()

    if radar_time:
        now = datetime.now(IST)
        lag = (now - radar_time).total_seconds() / 60.0

        # Sanity check: reject clearly impossible values (negative or >24h old)
        if 0 <= lag <= 1440:
            if lag < 30:
                freshness = "fresh"
            elif lag < 60:
                freshness = "stale"
            elif lag < 75:
                freshness = "very_stale"
            else:
                freshness = "down"

            return {
                "lag_mins": round(lag, 1),
                "freshness": freshness,
                "method": "ocr_timestamp",
                "message": f"Radar data is {lag:.0f} mins old",
                "radar_time": radar_time.strftime('%H:%M IST')
            }

    # Fallback if OCR fails
    return {
        "lag_mins": 25.0,
        "freshness": "stale",
        "method": "fallback_estimate",
        "message": "Radar ~25 mins old (estimate)",
        "radar_time": "Unknown"
    }


if __name__ == "__main__":
    print("=" * 45)
    print("RADAR LAG AWARENESS (OCR TIMESTAMP)")
    print("=" * 45)
    
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))

    # Download fresh GIF first
    frame_data = get_recent_frames(n=6)
    latest_ts = frame_data[-1][1] if frame_data else None
    lag_info = get_radar_lag_mins(latest_ts)
    now = datetime.now(IST)

    print(f"Current time  : {now.strftime('%H:%M:%S IST')}")
    print(f"Radar time    : {lag_info['radar_time']}")
    print(f"Lag           : {lag_info['lag_mins']} mins")
    print(f"Freshness     : {lag_info['freshness']}")
    print(f"Method        : {lag_info['method']}")
    print(f"Message       : {lag_info['message']}")
