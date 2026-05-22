"""Optional-authentication helpers for the Doré web app.

Auth here is *optional* and gated on persistence, never on use. Anonymous
callers get full functional access; the only thing an account unlocks is
durable state (saved analyses, curation votes, a profile).

The flow:
  * The SPA gets a public Supabase client config from ``/api/config`` and
    runs supabase-js, which owns session persistence + token refresh.
  * On every API call the SPA sends ``Authorization: Bearer <jwt>``.
  * ``current_user`` validates that JWT with the Supabase *anon* client
    (``auth.get_user(jwt)`` — no JWT secret needed) and returns the user,
    or ``None`` for an anonymous / invalid-token request. It NEVER raises
    just because a request is anonymous.
  * ``require_user`` is the strict variant — used only for save-type
    (curation) endpoints — and raises 401 for an anonymous caller.

Both depend on Supabase being configured; when it is not, every request is
treated as anonymous and the app stays fully usable in its transient mode.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from fastapi import Header, HTTPException

from sca import config


@lru_cache(maxsize=1)
def _anon_client() -> Any:
    """The public (anon-key) Supabase client — used to validate user JWTs."""
    from supabase import create_client

    return create_client(config.SUPABASE_URL, config.SUPABASE_ANON_KEY)


@lru_cache(maxsize=1)
def _service_client() -> Any:
    """The service-role Supabase client — bypasses RLS, used for profile upsert."""
    from supabase import create_client

    return create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)


def _bearer(authorization: Optional[str]) -> Optional[str]:
    """Extract a bearer token from an Authorization header, if present."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def _ensure_profile(user: dict[str, Any]) -> dict[str, Any]:
    """Ensure a ``profiles`` row exists for ``user``; return that profile.

    Idempotent: inserts the row via the service-role client only when it is
    missing. Profile failures never break the request — auth is best-effort.
    """
    uid = user["id"]
    svc = _service_client()
    try:
        existing = (
            svc.table("profiles").select("*").eq("id", uid).limit(1).execute()
        )
        if existing.data:
            return existing.data[0]
        row = {"id": uid, "email": user.get("email")}
        inserted = svc.table("profiles").insert(row).execute()
        return inserted.data[0] if inserted.data else row
    except Exception:  # noqa: BLE001 - profile is best-effort, never fatal
        return {"id": uid, "email": user.get("email")}


def resolve_user(authorization: Optional[str]) -> Optional[dict[str, Any]]:
    """Validate the bearer JWT and return ``{user, profile}`` or ``None``.

    Returns ``None`` for an anonymous request, a missing/invalid token, or
    when Supabase is not configured. Never raises for an anonymous caller.
    """
    if not config.supabase_configured() or not config.SUPABASE_ANON_KEY:
        return None
    token = _bearer(authorization)
    if not token:
        return None
    try:
        resp = _anon_client().auth.get_user(token)
    except Exception:  # noqa: BLE001 - an invalid token is just "anonymous"
        return None
    user = getattr(resp, "user", None)
    if user is None:
        return None
    user_dict = {
        "id": str(user.id),
        "email": getattr(user, "email", None),
    }
    profile = _ensure_profile(user_dict)
    return {"user": user_dict, "profile": profile}


# ── FastAPI dependencies ──────────────────────────────────────────────
def current_user(
    authorization: Optional[str] = Header(default=None),
) -> Optional[dict[str, Any]]:
    """Soft dependency: the signed-in user, or ``None`` for anonymous.

    Use this everywhere a request *may* be authenticated. It never rejects
    an anonymous caller — anonymous use is a first-class mode.
    """
    return resolve_user(authorization)


def require_user(
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Strict dependency: a signed-in user, else HTTP 401.

    Use this only on save-type (curation) endpoints — persisting a vote is
    the one thing an anonymous caller cannot do.
    """
    user = resolve_user(authorization)
    if user is None:
        raise HTTPException(
            401, "Sign in to save — this action persists a record."
        )
    return user
