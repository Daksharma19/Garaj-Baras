# Garaj Baras — auth.py
#
# Supabase Auth JWT verification. The frontend signs users in with
# supabase-js (Google OAuth / email OTP) and sends the resulting access
# token as `Authorization: Bearer <jwt>`. We verify it locally with the
# project's JWT secret (HS256) — no network call per request, which matters
# on Render's 512 MB / free-CPU box.
#
# Env: SUPABASE_JWT_SECRET  (Supabase dashboard → Settings → API → JWT Secret)
#
# Dev fallback: when SUPABASE_JWT_SECRET is unset (localhost without a
# Supabase project wired up yet), tokens are decoded WITHOUT signature
# verification so the flow can be exercised end-to-end. A loud warning is
# printed; production (Render) must always set the secret.

import os
from typing import Optional

from fastapi import Header, HTTPException  # type: ignore

JWT_SECRET = (os.environ.get("SUPABASE_JWT_SECRET") or "").strip()

if not JWT_SECRET:
    print("auth: WARNING — SUPABASE_JWT_SECRET not set; JWT signatures are "
          "NOT verified (dev mode only, never run production like this).")


def _decode(token: str) -> dict:
    import jwt  # pyjwt
    if JWT_SECRET:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"],
                          audience="authenticated")
    # Dev mode: trust the payload without verifying the signature.
    return jwt.decode(token, options={"verify_signature": False,
                                      "verify_aud": False})


def _user_from_header(authorization: Optional[str]) -> Optional[dict]:
    """Parse+verify a Bearer token. Returns {'id', 'email'} or None if no
    header. Raises HTTPException(401) on a bad/expired token."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Malformed Authorization header.")
    try:
        payload = _decode(parts[1])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    uid = payload.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="Token has no subject.")
    return {"id": uid, "email": payload.get("email")}


def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """FastAPI dependency: requires a valid signed-in user (401 otherwise)."""
    user = _user_from_header(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return user


def get_optional_user(authorization: Optional[str] = Header(None)) -> Optional[dict]:
    """FastAPI dependency: user dict when a valid token is present, else None.
    An invalid token still 401s (better to surface expiry than silently
    treat a signed-in user as anonymous)."""
    return _user_from_header(authorization)
