# Garaj Baras — alerts.py
#
# Rain alerts for saved locations via Web Push.
#
# Flow: the frontend saves a push subscription + a location. After every
# radar refresh (the same hook verification uses), each saved location inside
# that radar's coverage gets a two-slot nowcast check:
#   slot 0 rainy      -> "Rain right now" alert
#   slot 0 clear but
#   +15 slot rainy    -> "Rain approaching (~15 min)" alert
#
# Cooldown state machine (no spam): an alert fires only on the CLEAR->RAIN
# transition; the location must be observed clear again before it can fire
# again, and never more than once per MIN_RENOTIFY_MINS regardless.
#
# Honesty notes baked into the copy: radar carries 10-30 min of lag, so the
# "+15 min" message says "approaching, ~15 min" — an estimate, not a countdown.

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "alerts.db")
VAPID_KEYS_PATH = os.path.join(os.path.dirname(__file__), "vapid_keys.json")

# Never notify the same subscription twice within this window
MIN_RENOTIFY_MINS = 45.0

VAPID_CLAIMS_SUB = "mailto:prajjwalarya2020@gmail.com"

_db_lock = threading.Lock()
_vapid = None


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with _db_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT UNIQUE NOT NULL,
                sub_json TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                label TEXT,
                created_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'clear',   -- clear | raining
                last_notified_at TEXT
            )
        """)


def load_vapid():
    """VAPID credentials: env vars first (production), local file for dev."""
    global _vapid
    if _vapid is not None:
        return _vapid
    priv = os.environ.get("VAPID_PRIVATE_KEY_PEM")
    pub = os.environ.get("VAPID_PUBLIC_KEY")
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


def process_alerts(radar_name: str, state: dict, is_within_radar, latlon_to_pixel):
    """
    Run after a radar refresh. `state` is the freshly-built radar state dict.
    Checks each saved location in this radar's coverage and pushes on
    clear->rain transitions. Best-effort: never raises.
    """
    try:
        init_db()
        with _db_lock, _conn() as c:
            subs = c.execute(
                "SELECT id, endpoint, sub_json, lat, lon, label, state, last_notified_at "
                "FROM subscriptions").fetchall()
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

        for sid, endpoint, sub_json, lat, lon, label, sub_state, last_at in covered:
            try:
                px, py = latlon_to_pixel(lat, lon)
                slots = compute_nowcast_slots(
                    user_px=float(px), user_py=float(py),
                    dx=float(dx), dy=float(dy),
                    rain_mask=rain_mask, rgb_arr=rgb_arr,
                    patch_tracks=state.get("decay_tracks") or [],
                    lag_mins=lag,
                    num_slots=2, slot_interval=15,   # slot 0 (now) and +15 only
                    patches_motion=state.get("patches") or [],
                )
                now_rain = bool(slots[0]["has_rain"])
                soon_rain = bool(slots[1]["has_rain"]) if len(slots) > 1 else False

                place = label or f"{lat:.3f}, {lon:.3f}"
                new_state, title, body = sub_state, None, None

                if now_rain:
                    new_state = "raining"
                    title = f"⛈ Rain right now at {place}"
                    body = (f"{slots[0]['intensity']} observed by radar. "
                            f"(Radar data ~{lag:.0f} min old.)")
                elif soon_rain:
                    new_state = "raining"
                    title = f"🌧 Rain approaching {place}"
                    body = (f"{slots[1]['intensity']} expected in roughly 15 minutes "
                            f"(radar-based estimate, ±10 min).")
                else:
                    new_state = "clear"

                should_notify = (
                    title is not None
                    and sub_state == "clear"           # only on clear->rain transition
                    and (last_at is None or _mins_since(last_at) >= MIN_RENOTIFY_MINS)
                )

                with _db_lock, _conn() as c:
                    if should_notify:
                        alive = _send_push(sub_json, title, body)
                        if not alive:
                            c.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
                            print(f"alerts: pruned dead subscription {sid}")
                            continue
                        c.execute(
                            "UPDATE subscriptions SET state=?, last_notified_at=? WHERE id=?",
                            (new_state, datetime.now(timezone.utc).isoformat(), sid))
                        print(f"alerts: notified sub {sid} ({place}): {title}")
                    elif new_state != sub_state:
                        c.execute("UPDATE subscriptions SET state=? WHERE id=?",
                                  (new_state, sid))
            except Exception as e:
                print(f"alerts: check failed for sub {sid}: {e}")
    except Exception as e:
        print(f"alerts: process_alerts failed: {e}")


init_db()
