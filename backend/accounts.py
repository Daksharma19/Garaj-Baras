# Garaj Baras — accounts.py
#
# User accounts + saved locations ("Home", "Work", …), tied to Supabase Auth
# user ids (verified by auth.py). Same DB conventions as alerts.py:
# db.connect() (Postgres in prod / SQLite dev), module lock, lazy init,
# best-effort philosophy.
#
# The `users` row is upserted on first authenticated call (/me) — Supabase
# Auth owns identity; this table just anchors foreign data (saved locations,
# alert subscriptions) and records first-seen.

import os
import threading
from datetime import datetime, timezone

import db

DB_PATH = os.path.join(os.path.dirname(__file__), "accounts.db")

MAX_LOCATIONS_PER_USER = 10

_db_lock = threading.Lock()
_db_ready = False


def _conn():
    return db.connect(DB_PATH)


def init_db():
    global _db_ready
    if _db_ready:
        return
    with _db_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,          -- Supabase Auth uid
                email TEXT,
                created_at TEXT NOT NULL
            )
        """)
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS saved_locations (
                id {db.AUTOINC_PK},
                user_id TEXT NOT NULL,
                label TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                alerts_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
    _db_ready = True


def _now():
    return datetime.now(timezone.utc).isoformat()


def upsert_user(uid: str, email: str = None) -> dict:
    init_db()
    with _db_lock, _conn() as c:
        row = c.execute("SELECT id, email, created_at FROM users WHERE id=?",
                        (uid,)).fetchone()
        if row is None:
            c.execute("INSERT INTO users (id, email, created_at) VALUES (?,?,?)",
                      (uid, email, _now()))
            return {"id": uid, "email": email, "created_at": _now()}
        if email and email != row[1]:
            c.execute("UPDATE users SET email=? WHERE id=?", (email, uid))
        return {"id": row[0], "email": email or row[1], "created_at": row[2]}


def _loc_dict(r):
    return {"id": r[0], "label": r[1], "lat": r[2], "lon": r[3],
            "alerts_enabled": bool(r[4]), "created_at": r[5]}


def list_locations(uid: str) -> list:
    init_db()
    with _db_lock, _conn() as c:
        rows = c.execute(
            "SELECT id, label, lat, lon, alerts_enabled, created_at "
            "FROM saved_locations WHERE user_id=? ORDER BY id", (uid,)).fetchall()
    return [_loc_dict(r) for r in rows]


def add_location(uid: str, label: str, lat: float, lon: float,
                 alerts_enabled: bool = False):
    """Returns the new location dict, or None if the per-user cap is hit."""
    init_db()
    with _db_lock, _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM saved_locations WHERE user_id=?",
                      (uid,)).fetchone()[0]
        if n >= MAX_LOCATIONS_PER_USER:
            return None
        c.execute(
            "INSERT INTO saved_locations (user_id, label, lat, lon, alerts_enabled, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (uid, label, float(lat), float(lon), int(alerts_enabled), _now()))
        row = c.execute(
            "SELECT id, label, lat, lon, alerts_enabled, created_at "
            "FROM saved_locations WHERE user_id=? ORDER BY id DESC", (uid,)).fetchone()
    return _loc_dict(row)


def update_location(uid: str, loc_id: int, label=None, alerts_enabled=None) -> bool:
    init_db()
    sets, params = [], []
    if label is not None:
        sets.append("label=?"); params.append(label)
    if alerts_enabled is not None:
        sets.append("alerts_enabled=?"); params.append(int(alerts_enabled))
    if not sets:
        return False
    params += [loc_id, uid]
    with _db_lock, _conn() as c:
        cur = c.execute(
            f"UPDATE saved_locations SET {', '.join(sets)} WHERE id=? AND user_id=?",
            params)
        return cur.rowcount > 0


def delete_location(uid: str, loc_id: int) -> bool:
    init_db()
    with _db_lock, _conn() as c:
        cur = c.execute("DELETE FROM saved_locations WHERE id=? AND user_id=?",
                        (loc_id, uid))
        return cur.rowcount > 0


try:
    init_db()
except Exception as _e:
    print(f"accounts: init_db failed at import (will retry lazily): {_e}")
