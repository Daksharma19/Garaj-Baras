# Garaj Baras — db.py
#
# Single switch between the two database backends:
#   - DATABASE_URL set (production, Supabase Postgres) -> psycopg2
#   - DATABASE_URL unset (local dev)                    -> SQLite file, as before
#
# Why: Render's free-tier disk is ephemeral — SQLite files (alerts.db,
# verification.db) are wiped on every deploy/restart, silently killing alert
# subscriptions. Postgres makes them persistent; local dev needs zero setup.
#
# The wrapper keeps the sqlite3 calling style the rest of the codebase uses
# (`with connect(path) as c: c.execute("... ?", params)`), translating `?`
# placeholders to `%s` for Postgres. Callers write one schema string using
# AUTOINC_PK for the primary-key dialect difference.

import os
import sqlite3

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
IS_POSTGRES = DATABASE_URL.startswith("postgres")

AUTOINC_PK = "BIGSERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


class _PgConnection:
    """Context-managed Postgres connection mimicking sqlite3's connection API:
    execute()/executemany() return a cursor; `with` commits on success,
    rolls back on error. Unlike sqlite3, it closes on exit — Supabase's
    pooler should not accumulate idle connections."""

    def __init__(self):
        import psycopg2
        self._raw = psycopg2.connect(DATABASE_URL, connect_timeout=10)

    @staticmethod
    def _tr(sql):
        # None of our SQL contains a literal '?', so a plain replace is safe.
        return sql.replace("?", "%s")

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(self._tr(sql), params)
        return cur

    def executemany(self, sql, rows):
        cur = self._raw.cursor()
        cur.executemany(self._tr(sql), rows)
        return cur

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self._raw.close()
        return False


def connect(sqlite_path: str):
    """Open a DB connection. `sqlite_path` is only used in local/dev mode."""
    if IS_POSTGRES:
        return _PgConnection()
    c = sqlite3.connect(sqlite_path, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c
