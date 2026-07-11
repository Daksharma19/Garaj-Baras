# Garaj Baras - radar.py

import os
import re
import threading
import time
import requests
from PIL import Image, ImageSequence
from datetime import datetime, timezone, timedelta

# DELHI_MAXZ and DLH_MAXZ are BOTH products of DWRDELHI(PALAM); DELHI_MAXZ is
# the actively maintained one (18 frames, fresh), DLH_MAXZ rebuilds lazily and
# was observed lagging it by 60+ minutes.
GIF_URL = "https://mausam.imd.gov.in/Radar/animation/Converted/DELHI_MAXZ.gif"
GIF_SAVE_PATH = os.path.join(os.path.dirname(__file__), "delhi_radar.gif")
FRAMES_FOLDER = os.path.join(os.path.dirname(__file__), "frames")

# IMD's single "current radar" image (same 880x720 MAX-Z product) updates
# every ~10 min, while the animation GIF is rebuilt lazily and can lag it by
# an hour. After extracting GIF frames we fetch this and append it as the
# newest frame when its OCR timestamp is strictly newer (never duplicated).
CURRENT_IMG_URL = "https://mausam.imd.gov.in/Radar/caz_delhi.gif"
CURRENT_IMG_SAVE_PATH = os.path.join(os.path.dirname(__file__), "delhi_current.gif")

IST = timezone(timedelta(hours=5, minutes=30))

# HH:MM:SS tolerant of Tesseract artifacts: stray spaces around separators
# ("08: 22:24Z") and colon misread as ';' or '.'
_TIME_TOKEN = r'(\d{1,2})\s*[:;.]\s*(\d{2})\s*[:;.]\s*(\d{2})'


def parse_radar_timestamp_text(text):
    """
    Parse OCR'd radar info-panel text into a tz-aware IST datetime, or None.

    Tolerates the misreads we've actually seen from the IMD panel:
      - stray whitespace around colons ('al 08: 22:24Z')
      - ':' misread as ';' or '.'
      - zero misread as letter 'O' (safe to normalize: Z/IST/UTC contain no O)
      - trailing 'Z' of the UTC line misread as '2' or 'S'

    Priority: UTC line (HH:MM:SSZ) > explicit IST line > any time token
    (timezone chosen by presence of the word UTC).
    """
    if not text:
        return None
    up = text.upper().replace('O', '0')

    def _hms(match):
        h, m, s = (int(g) for g in match.groups())
        if 0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60:
            return h, m, s
        return None

    def _to_ist(h, m, s, tz):
        today = datetime.now(tz).date()
        dt = datetime(today.year, today.month, today.day, h, m, s, tzinfo=tz)
        return dt.astimezone(IST)

    utc_match = re.search(_TIME_TOKEN + r'\s*[Z2S]', up)
    if utc_match:
        v = _hms(utc_match)
        if v:
            return _to_ist(*v, tz=timezone.utc)

    ist_match = re.search(_TIME_TOKEN + r'\s*IST', up)
    if ist_match:
        v = _hms(ist_match)
        if v:
            return _to_ist(*v, tz=IST)

    any_match = re.search(_TIME_TOKEN, up)
    if any_match:
        v = _hms(any_match)
        if v:
            return _to_ist(*v, tz=timezone.utc if 'UTC' in up else IST)

    return None


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


def ocr_timestamp_from_image(full_rgb_img, ocr_crop=None):
    """
    Read the info-panel timestamp from a FULL (uncropped) radar image.
    Returns a tz-aware IST datetime or None. Never raises.

    Primary: deterministic digit template matching (timestamp_match) — exact
    on the fixed IMD font, no external binary, works on Render. Fallback:
    Tesseract OCR where available (validated 58/58 agreement; the matcher
    additionally read 6 frames Tesseract garbled).
    """
    box = ocr_crop if ocr_crop is not None else (635, 215, 875, 355)

    try:
        from timestamp_match import match_timestamp
        ts = match_timestamp(full_rgb_img, box)
        if ts is not None:
            return ts
    except Exception:
        pass

    try:
        import pytesseract
        from PIL import ImageEnhance
        if os.name == "nt":
            pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

        ts_crop = full_rgb_img.crop(box)
        w, h = ts_crop.size
        ts_ready = ImageEnhance.Contrast(
            ts_crop.resize((w * 3, h * 3), Image.LANCZOS).convert('L')
        ).enhance(2.0)
        text = pytesseract.image_to_string(ts_ready, config='--psm 6').strip()
        return parse_radar_timestamp_text(text)
    except Exception:
        return None


def augment_with_current_image(frame_data, output_folder,
                               current_url=CURRENT_IMG_URL,
                               current_save_path=CURRENT_IMG_SAVE_PATH,
                               ocr_crop=None,
                               crop_box=(0, CROP_TOP, CROP_RIGHT, CROP_BOTTOM)):
    """
    IMD's "current radar" image often updates before the animation GIF.
    If its OCR timestamp is STRICTLY newer than the last GIF frame, crop and
    append it as the newest frame. Duplicates (same timestamp or identical
    pixels) are skipped; an unreadable timestamp skips augmentation entirely —
    we never guess ordering.

    Returns possibly-extended frame_data.
    """
    if not frame_data:
        return frame_data
    try:
        ok, _ = download_gif(current_url, current_save_path)
        if not ok:
            return frame_data

        cur_full = Image.open(current_save_path).convert('RGB')
        cur_crop = cur_full.crop(crop_box)

        # Duplicate content check against the last extracted frame
        last_path, last_ts = frame_data[-1]
        try:
            last_img = Image.open(last_path).convert('RGB')
            if cur_crop.tobytes() == last_img.tobytes():
                return frame_data
        except Exception:
            pass

        cur_ts = ocr_timestamp_from_image(cur_full, ocr_crop)
        if cur_ts is None:
            # Without a readable timestamp we can't order it honestly — skip.
            print("current-image: timestamp unreadable, skipping augmentation")
            return frame_data

        if last_ts is not None and cur_ts <= last_ts + timedelta(minutes=1):
            # Equal or older than the animation's last frame — GIF wins.
            return frame_data

        idx = len(frame_data)
        frame_path = os.path.join(output_folder, f"frame_{idx:02d}.png")
        cur_crop.save(frame_path)
        gain = (cur_ts - last_ts).total_seconds() / 60.0 if last_ts else float('nan')
        print(f"current-image: appended as frame {idx:02d} @ "
              f"{cur_ts.strftime('%H:%M:%S IST')} (+{gain:.0f} min vs animation)")
        return frame_data + [(frame_path, cur_ts)]
    except Exception as e:
        print(f"current-image augmentation failed: {e}")
        return frame_data


def extract_frames(gif_path, output_folder, ocr_crop=None, crop_box=None):
    """
    Extract all frames from animated GIF.

    For each GIF frame:
    - OCR the timestamp from the exact RIGHT-panel coordinates (before cropping)
    - Crop the radar-circle region and save it as `frame_XX.png`

    `crop_box` is (left, top, right, bottom) for the radar-circle region; it
    defaults to Delhi's box but MUST be overridden for any source GIF whose
    raw frame size/panel layout differs from Delhi's 880x720 (e.g. Lucknow's
    704x594) — reusing Delhi's box on a different layout silently crops in
    the wrong region (legend/RHI panel bleed, black padding past the real
    image bounds) and desyncs every georef_*.py fit from the actual pixels.

    Returns:
        frame_data: list of (frame_path, timestamp_or_None)
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    crop_box = crop_box if crop_box is not None else (0, CROP_TOP, CROP_RIGHT, CROP_BOTTOM)

    frame_data = []
    try:
        # Pass 1: decode every GIF frame and drop byte-identical consecutive duplicates.
        # IMD regularly pads the GIF with repeated frames; feeding them to optical flow
        # produces zero-motion vectors and wastes OCR time.
        unique_frames = []   # list of (full_rgb, radar_crop_image)
        last_crop_bytes = None
        skipped = 0

        with Image.open(gif_path) as im:
            for frame in ImageSequence.Iterator(im):
                full = frame.convert('RGB')
                radar_crop = full.crop(crop_box)
                crop_bytes = radar_crop.tobytes()
                if crop_bytes == last_crop_bytes:
                    skipped += 1
                    continue
                last_crop_bytes = crop_bytes
                unique_frames.append((full, radar_crop))

        if skipped:
            print(f"Dropped {skipped} byte-identical duplicate frame(s) ({len(unique_frames)} unique)")

        # Pass 2: OCR EVERY unique frame. IMD's animation cadence is nominally
        # 10 min but slips badly (observed gaps of 20 and 70 min in one GIF),
        # so back-filling from the last frame at an assumed cadence can
        # mislabel frames by up to an hour. Real per-frame OCR keeps motion
        # speeds honest; ~15 frames costs a few seconds in a background thread.
        ocr_fail = 0
        timestamped = []   # (full, crop, ts)
        for idx, (full, frame_cropped) in enumerate(unique_frames):
            timestamp = ocr_timestamp_from_image(full, ocr_crop)
            if timestamp is None:
                ocr_fail += 1
            timestamped.append((full, frame_cropped, timestamp))
        if ocr_fail:
            print(f"OCR failed on {ocr_fail}/{len(unique_frames)} frame(s)")

        # Reject non-monotonic reads (an OCR misread, not time travel) and
        # drop same-timestamp duplicates (IMD re-renders; keep the later one).
        last_ok = None
        for i, (full, crop, ts) in enumerate(timestamped):
            if ts is None:
                continue
            if last_ok is not None and ts < last_ok:
                timestamped[i] = (full, crop, None)
                continue
            last_ok = ts
        deduped = []
        for full, crop, ts in timestamped:
            if (ts is not None and deduped and deduped[-1][2] is not None
                    and ts == deduped[-1][2]):
                deduped[-1] = (full, crop, ts)   # later render wins
                continue
            deduped.append((full, crop, ts))

        # Fill ONLY frames whose OCR failed, anchored to the nearest valid
        # neighbor at the nominal 10-min step. Valid reads are trusted as-is
        # even when their gaps are irregular.
        assumed_step_mins = 10.0
        ts_list = [ts for (_f, _c, ts) in deduped]
        n = len(ts_list)
        for i in range(n - 2, -1, -1):          # backward from next valid
            if ts_list[i] is None and ts_list[i + 1] is not None:
                ts_list[i] = ts_list[i + 1] - timedelta(minutes=assumed_step_mins)
        for i in range(1, n):                    # forward from prev valid
            if ts_list[i] is None and ts_list[i - 1] is not None:
                ts_list[i] = ts_list[i - 1] + timedelta(minutes=assumed_step_mins)

        for idx, (full, frame_cropped, _ts) in enumerate(deduped):
            frame_path = os.path.join(output_folder, f"frame_{idx:02d}.png")
            frame_cropped.save(frame_path)
            frame_data.append((frame_path, ts_list[idx]))
            if idx == 0:
                print("Radar crop frame size: %s" % (frame_cropped.size,))
    except Exception as e:
        print(f"Error extracting frames: {e}")

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
    Reads the timestamp of the LAST frame of the Delhi GIF on disk.
    Template matching first, Tesseract fallback (see ocr_timestamp_from_image).
    """
    try:
        gif = Image.open(GIF_SAVE_PATH)
        frames = list(ImageSequence.Iterator(gif))
        last_frame = frames[-1].convert('RGB')
        dt_ist = ocr_timestamp_from_image(last_frame)
        if dt_ist is not None:
            print(f"Timestamp extracted: {dt_ist.strftime('%H:%M:%S IST')}")
            return dt_ist
        print("Timestamp read failed - using 25 min fallback")
        return None
    except Exception as e:
        print(f"Timestamp read failed: {e} - using fallback")
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
