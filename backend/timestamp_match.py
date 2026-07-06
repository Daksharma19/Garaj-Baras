# Garaj Baras — timestamp_match.py
#
# Deterministic radar-timestamp reader: digit TEMPLATE MATCHING instead of
# OCR. The IMD info-panel time ("HH:MM:SSZ") is machine-rendered in one fixed
# font at a fixed spot, so each character either matches its template almost
# perfectly or the read is rejected — no guessing, no Tesseract dependency.
# This is what lets production (Render, no Tesseract binary) get real frame
# timestamps: per-frame lag, current-image augmentation and hit-rate
# verification all key off it.
#
# Templates in ts_templates/ were harvested from frames whose Tesseract read
# parsed successfully (ground truth), averaged per character.

import os
from datetime import datetime, timezone, timedelta

import numpy as np
from PIL import Image

IST = timezone(timedelta(hours=5, minutes=30))

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "ts_templates")
CANON_W, CANON_H = 16, 24

# Per-glyph agreement below this → the whole read is rejected (fail loudly).
MIN_GLYPH_SCORE = 0.85

_templates = None   # char -> bool array (CANON_H x CANON_W)


def _load_templates():
    global _templates
    if _templates is not None:
        return _templates
    t = {}
    try:
        for fn in os.listdir(TEMPLATE_DIR):
            if not fn.endswith(".png"):
                continue
            ch = ":" if fn[:-4] == "colon" else fn[:-4]
            arr = np.array(Image.open(os.path.join(TEMPLATE_DIR, fn)).convert("L")) > 127
            if arr.shape == (CANON_H, CANON_W):
                t[ch] = arr
    except Exception:
        pass
    _templates = t
    return t


def _panel_bbox(gray):
    """Bounding box of the white info panel inside the crop (echo strips and
    map pixels are colored/dark; the text panel is near-white)."""
    white = gray > 180
    col_ok = white.mean(axis=0) > 0.5
    row_ok = white.mean(axis=1) > 0.5
    xs = np.nonzero(col_ok)[0]
    ys = np.nonzero(row_ok)[0]
    if len(xs) == 0 or len(ys) == 0:
        return None
    return xs[0], ys[0], xs[-1] + 1, ys[-1] + 1


def segment_big_line(crop_rgb):
    """
    Segment the TALLEST text line of the info panel into tight glyph masks
    (left to right). Connected components are clustered into text lines by
    y-overlap; box borders are dropped by aspect; the colon's two dots are
    merged by x-overlap. Returns list of 2D bool arrays, or None.
    """
    import cv2

    gray = np.array(crop_rgb.convert("L"))
    bbox = _panel_bbox(gray)
    if bbox is None:
        return None
    px0, py0, px1, py1 = bbox
    panel_ink = (gray[py0:py1, px0:px1] < 128).astype(np.uint8)
    H, W = panel_ink.shape
    if H < 10 or W < 10:
        return None

    n, labels, stats, _c = cv2.connectedComponentsWithStats(panel_ink, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, w, h = (int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP]),
                      int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT]))
        # borders / rules: very long thin shapes; specks: sub-3px both ways
        if h >= 0.8 * H or w >= 0.8 * W:
            continue
        if h <= 2 and w <= 2:
            continue
        if h <= 2 and w > 10:       # horizontal rule
            continue
        if w <= 2 and h > 10:       # vertical rule
            continue
        comps.append((x, y, w, h, i))
    if not comps:
        return None

    # Cluster into text lines by y-range overlap
    lines = []
    for c in sorted(comps, key=lambda c: c[1]):
        placed = False
        for L in lines:
            ov = min(L["y1"], c[1] + c[3]) - max(L["y0"], c[1])
            if ov > 0.5 * min(L["y1"] - L["y0"], c[3]):
                L["comps"].append(c)
                L["y0"] = min(L["y0"], c[1])
                L["y1"] = max(L["y1"], c[1] + c[3])
                placed = True
                break
        if not placed:
            lines.append({"y0": c[1], "y1": c[1] + c[3], "comps": [c]})

    # The big timestamp = line with the greatest median glyph height,
    # requiring at least 5 glyph groups (HH:MM:SS has 8+)
    def med_h(L):
        return float(np.median([c[3] for c in L["comps"]]))
    cands = [L for L in lines if len(L["comps"]) >= 5]
    if not cands:
        return None
    best = max(cands, key=med_h)

    # Merge x-overlapping components (colon dots stack vertically)
    boxes = []
    for x, y, w, h, _i in sorted(best["comps"], key=lambda c: c[0]):
        if boxes and x < boxes[-1][2] - 1:
            bx0, by0, bx1, by1 = boxes[-1]
            boxes[-1] = (min(bx0, x), min(by0, y), max(bx1, x + w), max(by1, y + h))
        else:
            boxes.append((x, y, x + w, y + h))

    out = []
    for bx0, by0, bx1, by1 in boxes:
        sub = panel_ink[by0:by1, bx0:bx1].astype(bool)
        if sub.any():
            out.append(sub)
    return out


def _canon(glyph):
    img = Image.fromarray((glyph * 255).astype(np.uint8)).resize(
        (CANON_W, CANON_H), Image.LANCZOS)
    return np.array(img) > 127


def match_timestamp(full_rgb_img, ocr_crop):
    """
    Read the big UTC time line from a FULL radar image via template matching.
    Returns tz-aware IST datetime, or None if any glyph is ambiguous.
    """
    templates = _load_templates()
    if not templates:
        return None
    try:
        glyphs = segment_big_line(full_rgb_img.crop(ocr_crop))
        if not glyphs:
            return None

        chars, scores = [], []
        for gl in glyphs:
            c = _canon(gl)
            best_ch, best_sc = None, -1.0
            for ch, tpl in templates.items():
                sc = float((c == tpl).mean())
                if sc > best_sc:
                    best_ch, best_sc = ch, sc
            chars.append(best_ch)
            scores.append(best_sc)

        text = "".join(chars)
        if min(scores) < MIN_GLYPH_SCORE:
            return None
        # Strict shape: HH:MM:SSZ (the big line is always the UTC time)
        if len(text) != 9 or text[8] != "Z" or text[2] != ":" or text[5] != ":":
            return None
        hh, mm, ss = int(text[0:2]), int(text[3:5]), int(text[6:8])
        if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
            return None
        today = datetime.now(timezone.utc).date()
        dt_utc = datetime(today.year, today.month, today.day, hh, mm, ss,
                          tzinfo=timezone.utc)
        return dt_utc.astimezone(IST)
    except Exception:
        return None
