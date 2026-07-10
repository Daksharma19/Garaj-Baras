# Garaj Baras — alerts.py
#
# Rain alerts for saved locations via Web Push.
#
# Flow: the frontend saves a push subscription + a location. After every
# radar refresh (the same hook verification uses), each saved location inside
# that radar's coverage gets a nowcast check over slots 0/+15/+30/+45 min.
#
# State machine (clear -> approaching -> raining) with two alert moments:
#   clear -> approaching   "Rain approaching (~N min)"   heads-up, earliest slot
#   -> raining             "Rain right now"              arrival / escalation ping
# The arrival ping fires whether rain came straight out of a clear sky OR after
# an earlier heads-up (the approaching->raining escalation) — so a user who was
# told "rain in ~15 min" still gets pinged the moment it actually lands. When it
# is raining now and the +15 slot is clear, the arrival copy adds "likely to
# ease within ~15 min".
#
# Anti-spam: heads-up and arrival each have their own MIN_RENOTIFY_MINS cooldown
# (last_notified_at / last_rain_alert_at), so a rain edge flickering across the
# location can't spam, yet a heads-up never blocks the arrival ping that follows.
#
# Honesty notes baked into the copy: radar carries 10-30 min of lag, so the
# "+N min" message says "approaching" — an estimate, not a countdown.

import json
import os
import threading
from datetime import datetime, timezone

import db

DB_PATH = os.path.join(os.path.dirname(__file__), "alerts.db")
VAPID_KEYS_PATH = os.path.join(os.path.dirname(__file__), "vapid_keys.json")

# Never notify the same subscription twice within this window
MIN_RENOTIFY_MINS = 45.0

VAPID_CLAIMS_SUB = "mailto:prajjwalarya2020@gmail.com"

_db_lock = threading.Lock()
_db_ready = False
_vapid = None


def _conn():
    return db.connect(DB_PATH)


def init_db():
    global _db_ready
    if _db_ready:
        return
    with _db_lock, _conn() as c:
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id {db.AUTOINC_PK},
                endpoint TEXT UNIQUE NOT NULL,
                sub_json TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                label TEXT,
                created_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'clear',   -- clear | approaching | raining
                last_notified_at TEXT,
                last_rain_alert_at TEXT               -- cooldown for the "rain now" ping
            )
        """)
        # Migrate older DBs that predate last_rain_alert_at.
        if db.IS_POSTGRES:
            c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS last_rain_alert_at TEXT")
        else:
            cols = {r[1] for r in c.execute("PRAGMA table_info(subscriptions)")}
            if "last_rain_alert_at" not in cols:
                c.execute("ALTER TABLE subscriptions ADD COLUMN last_rain_alert_at TEXT")
    _db_ready = True


def _clean_env(val):
    """Tolerate common paste mistakes in env vars: surrounding quotes and
    literal \\n sequences instead of real newlines (breaks PEM parsing)."""
    if not val:
        return val
    val = val.strip()
    while len(val) > 1 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1].strip()
    return val.replace("\\n", "\n")


def load_vapid():
    """VAPID credentials: env vars first (production), local file for dev."""
    global _vapid
    if _vapid is not None:
        return _vapid
    priv = _clean_env(os.environ.get("VAPID_PRIVATE_KEY_PEM"))
    pub = _clean_env(os.environ.get("VAPID_PUBLIC_KEY"))
    if not (priv and pub):
        try:
            with open(VAPID_KEYS_PATH) as f:
                d = json.load(f)
            priv, pub = d["private_key_pem"], d["public_key"]
        except Exception:
            priv = pub = None
    _vapid = {"private_key_pem": priv, "public_key": pub}
    return _vapid


def public_key():
    return load_vapid().get("public_key")


def subscribe(sub: dict, lat: float, lon: float, label: str = None) -> bool:
    init_db()
    endpoint = (sub or {}).get("endpoint")
    if not endpoint:
        return False
    with _db_lock, _conn() as c:
        c.execute("""
            INSERT INTO subscriptions (endpoint, sub_json, lat, lon, label, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(endpoint) DO UPDATE SET
                sub_json=excluded.sub_json, lat=excluded.lat, lon=excluded.lon,
                label=excluded.label, state='clear'
        """, (endpoint, json.dumps(sub), float(lat), float(lon), label,
              datetime.now(timezone.utc).isoformat()))
    return True


def get_subscription(endpoint: str):
    """(sub_json, label) for one subscription, or None."""
    init_db()
    with _db_lock, _conn() as c:
        return c.execute(
            "SELECT sub_json, label FROM subscriptions WHERE endpoint=?",
            (endpoint,)).fetchone()


def all_subscription_coords():
    """[(lat, lon)] for every saved subscription — used by the scheduled sweep
    to decide which radars have watchers worth refreshing."""
    init_db()
    with _db_lock, _conn() as c:
        return [(float(r[0]), float(r[1]))
                for r in c.execute("SELECT lat, lon FROM subscriptions").fetchall()]


def unsubscribe(endpoint: str) -> bool:
    init_db()
    with _db_lock, _conn() as c:
        cur = c.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
        return cur.rowcount > 0


def _send_push(sub_json: str, title: str, body: str) -> bool:
    """Send one push. Returns False if the subscription is dead (prune it)."""
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid
    v = load_vapid()
    if not v.get("private_key_pem"):
        print("alerts: no VAPID keys configured — cannot push")
        return True  # don't prune; config problem, not a dead subscription
    try:
        # pywebpush treats a plain string as a raw base64url key, so a PEM
        # string fails to parse — hand it a Vapid instance instead.
        webpush(
            subscription_info=json.loads(sub_json),
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=Vapid.from_pem(v["private_key_pem"].encode()),
            vapid_claims={"sub": VAPID_CLAIMS_SUB},
            # WNS (Edge on Windows) rejects the default TTL of 0 with a 400;
            # an hour also matches how long a rain alert stays relevant.
            ttl=3600,
        )
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return False   # subscription expired/unsubscribed — prune
        print(f"alerts: push failed ({code}): {e}")
        return True
    except Exception as e:
        print(f"alerts: push failed: {e}")
        return True


def _mins_since(iso: str) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 60.0
    except Exception:
        return 1e9


def process_alerts(radar_name: str, state: dict, is_within_radar, latlon_to_pixel,
                   only_endpoint: str = None):
    """
    Run after a radar refresh. `state` is the freshly-built radar state dict.
    Checks each saved location in this radar's coverage and pushes a heads-up
    when rain is approaching and again when it arrives. Best-effort: never raises.

    `only_endpoint`: when set, checks just that one subscription (used by the
    instant check fired the moment a user enables alerts).
    """
    try:
        init_db()
        with _db_lock, _conn() as c:
            subs = c.execute(
                "SELECT id, endpoint, sub_json, lat, lon, label, state, "
                "last_notified_at, last_rain_alert_at "
                "FROM subscriptions").fetchall()
        if only_endpoint is not None:
            subs = [s for s in subs if s[1] == only_endpoint]
        if not subs:
            return

        covered = [s for s in subs if is_within_radar(s[3], s[4])]
        if not covered:
            return

        import numpy as np
        from PIL import Image
        from optical_flow import isolate_rain
        from nowcast import compute_nowcast_slots

        latest_frame = state.get("latest_frame")
        if not latest_frame:
            return
        dx, dy = state["movement"][0], state["movement"][1]
        lag_info = state.get("lag_info") or {}
        lag = float(lag_info.get("lag_mins") or 25.0)
        rain_mask = isolate_rain(latest_frame, clutter_mask=state.get("clutter_mask"))
        rgb_arr = np.array(Image.open(latest_frame).convert("RGB"))

        for sid, endpoint, sub_json, lat, lon, label, sub_state, last_at, last_rain_at in covered:
            try:
                px, py = latlon_to_pixel(lat, lon)
                slots = compute_nowcast_slots(
                    user_px=float(px), user_py=float(py),
                    dx=float(dx), dy=float(dy),
                    rain_mask=rain_mask, rgb_arr=rgb_arr,
                    patch_tracks=state.get("decay_tracks") or [],
                    lag_mins=lag,
                    num_slots=4, slot_interval=15,   # now, +15, +30, +45
                    patches_motion=state.get("patches") or [],
                )
                first_rainy = next((s for s in slots if s["has_rain"]), None)
                place = label or f"{lat:.3f}, {lon:.3f}"

                if first_rainy is None:
                    desired = "clear"
                elif first_rainy["slot_mins"] == 0:
                    desired = "raining"
                else:
                    desired = "approaching"

                now_iso = datetime.now(timezone.utc).isoformat()
                title = body = None

                if desired == "raining" and sub_state != "raining":
                    # Rain has arrived — fire whether it came out of a clear sky
                    # or after an earlier heads-up (approaching->raining escalation).
                    # Own cooldown so a rain edge flickering across the pixel can't
                    # spam; a prior heads-up does NOT count against this cooldown.
                    if last_rain_at is None or _mins_since(last_rain_at) >= MIN_RENOTIFY_MINS:
                        title = f"⛈ Rain right now at {place}"
                        body = f"{first_rainy['intensity']} observed by radar."
                        if len(slots) > 1 and not slots[1]["has_rain"]:
                            body += " Likely to ease within ~15 min."
                        body += f" (Radar data ~{lag:.0f} min old.)"
                elif desired == "approaching" and sub_state == "clear":
                    # First heads-up for incoming rain.
                    if last_at is None or _mins_since(last_at) >= MIN_RENOTIFY_MINS:
                        eta = int(first_rainy["slot_mins"])
                        title = f"🌧 Rain approaching {place}"
                        body = (f"{first_rainy['intensity']} expected in roughly {eta} minutes "
                                f"(radar-based estimate, ±10 min).")

                log_msg = None
                with _db_lock, _conn() as c:
                    if title is not None:
                        alive = _send_push(sub_json, title, body)
                        if not alive:
                            c.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
                            log_msg = f"alerts: pruned dead subscription {sid}"
                        elif desired == "raining":
                            c.execute(
                                "UPDATE subscriptions SET state=?, last_notified_at=?, "
                                "last_rain_alert_at=? WHERE id=?",
                                (desired, now_iso, now_iso, sid))
                            log_msg = f"alerts: notified sub {sid} ({place}): {title}"
                        else:
                            c.execute(
                                "UPDATE subscriptions SET state=?, last_notified_at=? WHERE id=?",
                                (desired, now_iso, sid))
                            log_msg = f"alerts: notified sub {sid} ({place}): {title}"
                    elif desired != sub_state:
                        c.execute("UPDATE subscriptions SET state=? WHERE id=?",
                                  (desired, sid))
                # Log outside the transaction: a console that can't encode the
                # emoji title must not roll back a committed state update.
                if log_msg:
                    try:
                        print(log_msg)
                    except Exception:
                        pass
            except Exception as e:
                print(f"alerts: check failed for sub {sid}: {e}")
    except Exception as e:
        print(f"alerts: process_alerts failed: {e}")


# Best-effort at import: a transient Postgres outage must not crash startup;
# init_db() is retried lazily by every public entry point.
try:
    init_db()
except Exception as _e:
    print(f"alerts: init_db failed at import (will retry lazily): {_e}")
