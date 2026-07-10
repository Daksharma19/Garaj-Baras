# Garaj Baras — auth.py
#
# Supabase Auth JWT verification. The frontend signs users in with
# supabase-js (Google OAuth / email OTP) and sends the resulting access
# token as `Authorization: Bearer <jwt>`. We verify it locally so there is no
# network round-trip per request (matters on Render's 512 MB / free-CPU box).
#
# Supabase signs access tokens one of two ways depending on the project:
#   - HS256  — a shared "JWT secret" (legacy default). Verify with that secret.
#   - ES256/RS256 — asymmetric "JWT signing keys" (newer default). Verify with
#     the project's PUBLIC keys, fetched once from the JWKS endpoint and cached.
# We support BOTH and auto-pick based on the token header's `alg`, so the
# project's signing choice doesn't require a code change.
#
# Env (production — set on Render):
#   SUPABASE_JWT_SECRET   — the HS256 shared secret (Settings → JWT Keys).
#                           Needed only if the project still signs with HS256.
#   SUPABASE_URL          — e.g. https://<ref>.supabase.co  (Settings → API).
#                           Used to locate the JWKS for asymmetric verification.
#   (Either/both may be set; whichever matches the incoming token is used.)
#
# Dev fallback: if NEITHER is configured, tokens are decoded WITHOUT signature
# verification so localhost works before anything is wired up. A loud warning
# is printed. Production must always set at least one of the two.

import os
from typing import Optional

from fastapi import Header, HTTPException  # type: ignore

JWT_SECRET = (os.environ.get("SUPABASE_JWT_SECRET") or "").strip()
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if SUPABASE_URL else ""

_VERIFY = bool(JWT_SECRET or JWKS_URL)

if not _VERIFY:
    print("auth: WARNING — neither SUPABASE_JWT_SECRET nor SUPABASE_URL set; "
          "JWT signatures are NOT verified (dev mode only, never in production).")

# Lazily-built JWKS client (PyJWKClient caches keys + refetches on rotation).
_jwks_client = None


def _get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        from jwt import PyJWKClient
        _jwks_client = PyJWKClient(JWKS_URL)
    return _jwks_client


def _decode(token: str) -> dict:
    import jwt  # pyjwt

    if not _VERIFY:
        # Dev mode: trust the payload without verifying the signature.
        return jwt.decode(token, options={"verify_signature": False,
                                          "verify_aud": False})

    alg = (jwt.get_unverified_header(token) or {}).get("alg", "HS256")

    if alg == "HS256":
        if not JWT_SECRET:
            raise ValueError("HS256 token but SUPABASE_JWT_SECRET is not set.")
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"],
                          audience="authenticated")

    # Asymmetric (ES256/RS256): verify against the project's public JWKS.
    if not JWKS_URL:
        raise ValueError(f"{alg} token but SUPABASE_URL is not set for JWKS.")
    signing_key = _get_jwks_client().get_signing_key_from_jwt(token).key
    return jwt.decode(token, signing_key, algorithms=[alg],
                      audience="authenticated")


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
