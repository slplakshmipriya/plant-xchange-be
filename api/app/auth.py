"""Firebase Authentication (ID token) verification.

- Every route except EXEMPT_PATHS requires `Authorization: Bearer <idToken>`.
- Verified uid is stashed on ``request.state.uid``; use the ``get_current_uid``
  dependency in route handlers.
- ``ensure_owner`` is the per-user scoping helper: call it before touching a
  resource owned by someone else. It raises 403 on mismatch.
- firebase-admin is initialized lazily; token verification is the only
  firebase-admin call in the request path, which makes it trivial to mock in
  tests (no real credentials needed).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings
from .errors import error_response

logger = logging.getLogger(__name__)

EXEMPT_PATHS = {
    "/healthz",
    "/openapi.yaml",
    # Webhooks / internal jobs cannot carry a user ID token; they use their
    # own auth (HMAC signature / shared secret) and are exempt here.
    "/v1/idv/webhook",
    "/v1/internal/sweep",
    "/v1/internal/warm",
}

# Path prefixes that are public by design (listing photos served by the stub).
EXEMPT_PREFIXES = ("/v1/uploads/public/",)


def _is_exempt(path: str) -> bool:
    # Normalize a trailing slash so /healthz/ doesn't 401 before routing.
    normalized = path.rstrip("/") or "/"
    if normalized in EXEMPT_PATHS:
        return True
    return path.startswith(EXEMPT_PREFIXES)


def is_exempt_path(path: str) -> bool:
    """Public helper so the OpenAPI contract marks the same paths exempt."""
    return _is_exempt(path)


def init_firebase() -> None:
    """Initialize firebase-admin once. Warns (does not crash) without credentials."""
    try:
        import firebase_admin

        try:
            firebase_admin.get_app()
            return
        except ValueError:
            pass  # not initialized yet
        options = {}
        project_id = get_settings().firebase_project_id
        if project_id:
            options["projectId"] = project_id
        firebase_admin.initialize_app(options=options or None)
        logger.info("firebase-admin initialized")
    except Exception as exc:  # noqa: BLE001 — startup must not die on missing creds
        logger.warning("firebase-admin init skipped: %s", exc)


def verify_id_token(token: str) -> dict:
    """Thin wrapper around firebase-admin so tests can monkeypatch one symbol.

    ``check_revoked=True`` (H14): a disabled/deleted user's ID token is
    rejected immediately instead of lingering for up to ~1h. Costs one extra
    identity-platform lookup per verification — correctness over cost.
    """
    from firebase_admin import auth as fb_auth

    return fb_auth.verify_id_token(token, check_revoked=True)


class FirebaseAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_exempt(request.url.path):
            return await call_next(request)

        authz = request.headers.get("authorization", "")
        scheme, _, token = authz.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return error_response(
                request, 401, "unauthorized", "Missing or invalid Authorization header"
            )
        try:
            decoded = verify_id_token(token.strip())
        except Exception as exc:  # noqa: BLE001 — any verification failure is a 401
            logger.info("token verification failed: %s", type(exc).__name__)
            return error_response(request, 401, "unauthorized", "Invalid or expired ID token")

        uid = decoded.get("uid") if isinstance(decoded, dict) else None
        if not uid:
            return error_response(request, 401, "unauthorized", "ID token has no uid claim")
        request.state.uid = uid
        request.state.claims = decoded
        return await call_next(request)


def get_current_uid(request: Request) -> str:
    """FastAPI dependency: the verified Firebase uid, or 401."""
    uid = getattr(request.state, "uid", None)
    if not uid:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "Authentication required"},
        )
    return uid


def ensure_owner(owner_uid: str, uid: str) -> None:
    """Per-user scoping helper. Raises 403 when uid does not own the resource."""
    if owner_uid != uid:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "You do not own this resource"},
        )
