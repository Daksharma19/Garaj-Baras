# Garaj Baras — verification.py
#
# Automated prediction verification ("hit rate").
#
# Every waypoint/nowcast prediction is a falsifiable claim: "at pixel (px,py)
# at time T there will/won't be rain of intensity X". The radar publishes the
# truth for T within ~2 hours. We log the claim at prediction time and grade
# it when a frame near T shows up in the refresh pipeline. Fully automatic —
# no user action, no separate scheduler (verify runs after each radar refresh).
#
# Outcomes:
#   hit           predicted rain,  rain observed
#   false_alarm   predicted rain,  no rain observed
#   miss          predicted clear, rain observed
#   correct_clear predicted clear, no rain observed
#
# Metrics (from outcome counts):
#   POD = hit / (hit + miss)            "of the real rain, how much we caught"
#   FAR = false_alarm / (hit + f.a.)    "of our alarms, how many were wrong"
#   CSI = hit / (hit + miss + f.a.)     balanced single score

import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

import numpy as np

DB_PATH = os.path.join(os.path.dirname(__file__), "verification.db")

# A prediction whose target time slipped out of the radar frame window
# unverified is marked expired (server was asleep / IMD was down).
EXPIRE_AFTER_MINS = 200.0

# Frame timestamp must be within this of the prediction target time
MATCH_TOLERANCE_MINS = 5.0

# Neighborhood radius (px) for ground-truth rain check — mirrors the
# prediction side's tolerance.
TRUTH_RADIUS_PX = 2

ENGINE_VERSION = (os.environ.get("RENDER_GIT_COMMIT") or "dev")[:9]

_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with _db_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,          -- UTC iso
                radar TEXT NOT NULL,
                source TEXT NOT NULL,              -- route | nowcast
                lat REAL, lon REAL,
                px INTEGER, py INTEGER,
                eta_mins REAL,
                target_time TEXT NOT NULL,         -- UTC iso
                predicted_rain INTEGER NOT NULL,
                predicted_label TEXT,
                predicted_dbz REAL,
                confidence TEXT,
                lag_mins REAL,
                engine_version TEXT,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending|verified|expired
                verified_at TEXT,
                frame_time TEXT,
                actual_rain INTEGER,
                actual_dbz REAL,
                outcome TEXT                        -- hit|false_alarm|miss|correct_clear
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status, radar, target_time)")


def log_predictions(radar: str, source: str, items: list, lag_mins=None):
    """
    Record a batch of claims. Each item:
      {lat, lon, px, py, eta_mins, rain_expected, label, dbz, confidence}
    target_time = now + eta_mins. Best-effort: never raises.
    """
    try:
        now = datetime.now(timezone.utc)
        rows = []
        for it in items or []:
            eta = float(it.get("eta_mins") or 0.0)
            rows.append((
                now.isoformat(),
                radar, source,
                it.get("lat"), it.get("lon"),
                int(it.get("px")) if it.get("px") is not None else None,
                int(it.get("py")) if it.get("py") is not None else None,
                eta,
                (now + timedelta(minutes=eta)).isoformat(),
                1 if it.get("rain_expected") else 0,
                it.get("label"),
                float(it.get("dbz")) if it.get("dbz") is not None else None,
                str(it.get("confidence")) if it.get("confidence") is not None else None,
                float(lag_mins) if lag_mins is not None else None,
                ENGINE_VERSION,
            ))
        if not rows:
            return 0
        with _db_lock, _conn() as c:
            c.executemany("""
                INSERT INTO predictions
                (created_at, radar, source, lat, lon, px, py, eta_mins, target_time,
                 predicted_rain, predicted_label, predicted_dbz, confidence, lag_mins,
                 engine_version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
        return len(rows)
    except Exception as e:
        print(f"verification: log_predictions failed: {e}")
        return 0


def _truth_at(rain_mask, rgb_arr, px, py, rgb_to_dbz):
    """Ground truth at a pixel: (rain_bool, max_dbz) in a small neighborhood."""
    h, w = rain_mask.shape[:2]
    x0 = max(0, px - TRUTH_RADIUS_PX); x1 = min(w, px + TRUTH_RADIUS_PX + 1)
    y0 = max(0, py - TRUTH_RADIUS_PX); y1 = min(h, py + TRUTH_RADIUS_PX + 1)
    if x0 >= x1 or y0 >= y1:
        return None, None
    region = rain_mask[y0:y1, x0:x1] > 0
    if not region.any():
        return False, 0.0
    best = 0
    sub = rgb_arr[y0:y1, x0:x1]
    ys, xs = np.nonzero(region)
    for yy, xx in zip(ys, xs):
        r, g, b = sub[yy, xx]
        d = rgb_to_dbz(int(r), int(g), int(b))
        if d > best:
            best = d
    return True, float(best)


def verify_pending(radar: str, frame_data, isolate_fn, clutter_mask=None):
    """
    Grade pending predictions for `radar` against the frames now available.
    frame_data: list of (frame_path, timestamp_or_None), timestamps tz-aware.
    Called at the end of each radar refresh. Best-effort: never raises.
    Returns (n_verified, n_expired).
    """
    try:
        from PIL import Image
        from fuzzy import rgb_to_dbz

        init_db()
        now = datetime.now(timezone.utc)
        tol = timedelta(minutes=MATCH_TOLERANCE_MINS)

        frames = [(fp, ts.astimezone(timezone.utc)) for fp, ts in (frame_data or []) if ts is not None]

        n_verified = 0
        with _db_lock, _conn() as c:
            pend = c.execute(
                "SELECT id, px, py, target_time, predicted_rain FROM predictions "
                "WHERE status='pending' AND radar=?", (radar,),
            ).fetchall()

            # Cache decoded frames lazily
            cache = {}

            def frame_truth_arrays(fp):
                if fp not in cache:
                    mask = isolate_fn(fp, clutter_mask=clutter_mask)
                    rgb = np.array(Image.open(fp).convert("RGB"))
                    cache[fp] = (mask, rgb)
                return cache[fp]

            for pid, px, py, tt_iso, pred_rain in pend:
                try:
                    tt = datetime.fromisoformat(tt_iso)
                except Exception:
                    continue
                if tt > now:
                    continue  # claim is about the future — not gradable yet

                # Nearest frame within tolerance
                best_fp, best_ts, best_gap = None, None, tol
                for fp, ts in frames:
                    gap = abs(ts - tt)
                    if gap <= best_gap:
                        best_fp, best_ts, best_gap = fp, ts, gap

                if best_fp is None:
                    # No frame covers T. Expire only when T is far enough past
                    # that the GIF window can no longer contain it.
                    if (now - tt) > timedelta(minutes=EXPIRE_AFTER_MINS):
                        c.execute("UPDATE predictions SET status='expired' WHERE id=?", (pid,))
                    continue

                if px is None or py is None:
                    c.execute("UPDATE predictions SET status='expired' WHERE id=?", (pid,))
                    continue

                mask, rgb = frame_truth_arrays(best_fp)
                actual_rain, actual_dbz = _truth_at(mask, rgb, int(px), int(py), rgb_to_dbz)
                if actual_rain is None:
                    c.execute("UPDATE predictions SET status='expired' WHERE id=?", (pid,))
                    continue

                if pred_rain and actual_rain:
                    outcome = "hit"
                elif pred_rain and not actual_rain:
                    outcome = "false_alarm"
                elif not pred_rain and actual_rain:
                    outcome = "miss"
                else:
                    outcome = "correct_clear"

                c.execute("""
                    UPDATE predictions
                    SET status='verified', verified_at=?, frame_time=?,
                        actual_rain=?, actual_dbz=?, outcome=?
                    WHERE id=?
                """, (now.isoformat(), best_ts.isoformat(),
                      1 if actual_rain else 0, actual_dbz, outcome, pid))
                n_verified += 1

            n_expired = c.execute(
                "SELECT COUNT(*) FROM predictions WHERE status='expired' AND radar=?",
                (radar,),
            ).fetchone()[0]

        if n_verified:
            print(f"verification: {radar}: graded {n_verified} prediction(s)")
        return n_verified, n_expired
    except Exception as e:
        print(f"verification: verify_pending failed: {e}")
        return 0, 0


def _score(rows):
    counts = {"hit": 0, "false_alarm": 0, "miss": 0, "correct_clear": 0}
    for outcome, n in rows:
        if outcome in counts:
            counts[outcome] = n
    h, fa, m, cc = counts["hit"], counts["false_alarm"], counts["miss"], counts["correct_clear"]
    total = h + fa + m + cc

    def r(x):
        return round(x, 3)

    return {
        "checks": total,
        "hit": h, "false_alarm": fa, "miss": m, "correct_clear": cc,
        "pod": r(h / (h + m)) if (h + m) else None,
        "far": r(fa / (h + fa)) if (h + fa) else None,
        "csi": r(h / (h + m + fa)) if (h + m + fa) else None,
        "accuracy": r((h + cc) / total) if total else None,
    }


def accuracy_stats(days: int = 30):
    """Aggregated verified accuracy: overall, per radar, per lead-time bucket."""
    init_db()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _db_lock, _conn() as c:
        overall = _score(c.execute(
            "SELECT outcome, COUNT(*) FROM predictions "
            "WHERE status='verified' AND created_at>=? GROUP BY outcome", (since,),
        ).fetchall())

        by_radar = {}
        for (radar,) in c.execute(
            "SELECT DISTINCT radar FROM predictions WHERE status='verified' AND created_at>=?",
            (since,),
        ).fetchall():
            by_radar[radar] = _score(c.execute(
                "SELECT outcome, COUNT(*) FROM predictions "
                "WHERE status='verified' AND created_at>=? AND radar=? GROUP BY outcome",
                (since, radar),
            ).fetchall())

        buckets = {"0-30 min": (0, 30), "30-60 min": (30, 60), "60-120 min": (60, 1e9)}
        by_lead = {}
        for name, (lo, hi) in buckets.items():
            by_lead[name] = _score(c.execute(
                "SELECT outcome, COUNT(*) FROM predictions "
                "WHERE status='verified' AND created_at>=? AND eta_mins>=? AND eta_mins<? "
                "GROUP BY outcome", (since, lo, hi),
            ).fetchall())

        pending = c.execute("SELECT COUNT(*) FROM predictions WHERE status='pending'").fetchone()[0]
        expired = c.execute(
            "SELECT COUNT(*) FROM predictions WHERE status='expired' AND created_at>=?",
            (since,),
        ).fetchone()[0]

    return {
        "window_days": days,
        "engine_version": ENGINE_VERSION,
        "overall": overall,
        "by_radar": by_radar,
        "by_lead_time": by_lead,
        "pending_checks": pending,
        "expired_unverifiable": expired,
    }


init_db()
