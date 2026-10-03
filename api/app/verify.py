"""Phone verification (API-011).

Firebase Auth is the verifier: the middleware has already validated the ID
token, so the ``phone_number`` claim is trustworthy. This endpoint binds that
verified phone number to the caller's uid:

- phone numbers are stored only as a keyed hash: HMAC-SHA-256 under
  ``PHONE_HASH_SECRET`` (domain-separated). Rows written before the key
  existed hold the legacy unsalted SHA-256 — verification dual-reads the
  legacy hash for uniqueness and rewrites the row to the keyed hash, so
  the table migrates lazily with no downtime. Deployed environments
  refuse to boot without the secret (``validate_phone_config``).
- one account per phone_hash: a hash claimed by another uid is a safe 409
  (the response never reveals the other uid).

M17 decision: ``device_fingerprint`` is NO LONGER collected. It was stored at
signup but nothing ever read it (no anomaly detection, no enforcement), and
the Android client never sent the ``X-Device-Fingerprint`` header. The
column is dropped by migration 0029. The header is deliberately ignored if
sent — collection without a documented use is data accumulation, not a
trust signal.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import get_current_uid
from .config import get_settings
from .credits import CreditRepo, ensure_starter_credits, get_credit_repo
from .users import PhoneInUseError, UserRepo, get_user_repo

router = APIRouter(tags=["auth"])

LEGACY_PHONE_HASH_DOMAIN = "gs-phone-v1:"
PHONE_HASH_DOMAIN = "gs-phone-v2:"


def legacy_phone_hash(phone_number: str) -> str:
    """The pre-HMAC hash: domain-separated, unsalted SHA-256. Kept only so
    verification can recognize (and upgrade) rows written before
    ``PHONE_HASH_SECRET`` existed — never store new rows with it outside
    local dev (where no secret is configured)."""
    return hashlib.sha256(
        (LEGACY_PHONE_HASH_DOMAIN + phone_number).encode("utf-8")).hexdigest()


def phone_hash(phone_number: str) -> str:
    """HMAC-SHA-256 of the E.164 phone number, keyed by
    ``PHONE_HASH_SECRET``. Without the key the hash is a fast unsalted
    digest over a ~1e10-value space — reversible from any DB leak — so
    deployed environments refuse to boot keyless; keyless local dev falls
    back to the legacy hash (dual-read keeps those rows verifiable)."""
    secret = get_settings().phone_hash_secret
    if not secret:
        return legacy_phone_hash(phone_number)
    return hmac_mod.new(
        secret.encode("utf-8"),
        (PHONE_HASH_DOMAIN + phone_number).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@router.post("/v1/auth/verify")
def verify_phone(
    request: Request,
    uid: str = Depends(get_current_uid),
    repo: UserRepo = Depends(get_user_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
) -> dict:
    claims = getattr(request.state, "claims", None) or {}
    phone = claims.get("phone_number")
    if not phone:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "phone_verification_required",
                "message": "This account has no verified phone number. Sign in with phone auth first.",
            },
        )
    # Dual-read (M2): a number bound under the LEGACY hash by another uid
    # must still 409, even though new rows are written with the keyed hash.
    # When the legacy row belongs to the caller, the upsert below rewrites
    # it to the keyed hash — rows upgrade lazily at next verify.
    legacy_owner = repo.get_by_phone_hash(legacy_phone_hash(phone))
    if legacy_owner is not None and legacy_owner["uid"] != uid:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "phone_in_use",
                "message": "This phone number is already registered to another account.",
            },
        )
    try:
        repo.upsert(uid, phone_hash=phone_hash(phone))
    except PhoneInUseError:
        # Safe error: do not reveal which uid holds the number.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "phone_in_use",
                "message": "This phone number is already registered to another account.",
            },
        )
    # Idempotent starter-credit bootstrap (a user row may first be created here).
    ensure_starter_credits(uid, credit_repo)
    return {"uid": uid, "verified": True}
