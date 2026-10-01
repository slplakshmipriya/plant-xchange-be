"""User profiles (API-010) + account deletion / data export (C6).

- ``uid`` is the Firebase Auth uid from the verified ID token — never
  client-chosen. All writes are owner-scoped (``ensure_owner``).
- Repository pattern: ``UserRepo`` protocol with a Postgres implementation
  and an in-memory one for tests. Route handlers depend on the
  ``get_user_repo`` factory; tests override it via ``dependency_overrides``.
- SEC-010 (PII): ``public_profile()`` and ``owner_profile()`` are the ONLY
  serializers. Neither emits ``phone_hash``. ``home_zip`` is emitted only
  to the owner.
- M17: ``device_fingerprint`` is gone — collected at signup but never used;
  migration 0029 drops the column and ``verify.py`` no longer reads the
  ``X-Device-Fingerprint`` header.
- M18: ``home_zip`` is collected for the PLANNED zip-based listing search
  (not yet implemented by any backend logic). Owner-visible only; see the
  consent-copy follow-up note on ``ProfileIn.home_zip``.
- M20c: age gate — onboarding (``POST /v1/users`` / ``PATCH /v1/users/me``)
  requires an explicit 13+ attestation until ``age_attested_at`` is recorded
  (migration 0029); missing attestation is 422 ``age_attestation_required``.
- L1c: ``PostgresUserRepo.upsert`` whitelists columns — dict keys are never
  interpolated into SQL unchecked.
- C6: ``DELETE /v1/users/me`` removes the caller's account (immediate,
  irreversible — no grace period pre-launch) and ``GET /v1/users/me/export``
  returns a portable dump of everything stored about the caller.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import ensure_owner, get_current_uid
from .cache import CachedUserRepo
from .config import get_settings
from .credits import CreditRepo, ensure_starter_credits, get_credit_repo
from .crypto import GEO_KEY_ENV, MESSAGE_KEY_ENV, decrypt_float, decrypt_text
from .db import get_db_conn
from .images import release_listing_images
router = APIRouter(prefix="/v1/users", tags=["users"])

ZIP_RE = re.compile(r"^\d{5}$")

# Fields that must never leave the server (SEC-010). Asserted by test_pii.py.
# (M17: device_fingerprint was dropped from the schema — nothing to guard.)
_NEVER_EXPOSE = ("phone_hash",)


class ProfileIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    avatar_url: str | None = Field(default=None, max_length=2048)
    # M18: home_zip is collected for the PLANNED zip-based listing search
    # (no backend logic consumes it yet). Owner-visible only.
    # ANDROID FOLLOW-UP: add consent copy on the profile screen explaining
    # that home_zip is used for nearby / zip-based search.
    home_zip: str | None = Field(default=None, max_length=10)
    # M20c: 13+ age attestation. Required at onboarding until recorded.
    age_attestation: bool | None = Field(
        default=None,
        description="Attest the user is 13 or older. Required until recorded.",
    )


class PublicProfile(BaseModel):
    uid: str
    display_name: str | None
    avatar_url: str | None
    idv_status: str = "unverified"


class OwnerProfile(PublicProfile):
    home_zip: str | None
    created_at: str | None = None


def public_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Serializer for profiles seen by other users. No PII, ever."""
    return {
        "uid": row["uid"],
        "display_name": row.get("display_name"),
        "avatar_url": row.get("avatar_url"),
        "idv_status": row.get("idv_status", "unverified"),
    }


def owner_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Serializer for the owner's own profile. Still no phone_hash/fingerprint."""
    out = public_profile(row)
    out["home_zip"] = row.get("home_zip")
    created = row.get("created_at")
    out["created_at"] = created.isoformat() if hasattr(created, "isoformat") else created
    return out


def _check_pii_leak(payload: dict[str, Any]) -> None:
    for field in _NEVER_EXPOSE:
        assert field not in payload, f"PII leak: {field} in response payload"


class PhoneInUseError(Exception):
    """phone_hash already claimed by a different uid (safe 409, no uid leaked)."""


# L1c: column whitelist for upserts — column names are interpolated into SQL
# in the Postgres path, so only these known columns may pass. Never derive
# this from input. Both repos enforce it.
_UPSERTABLE_COLUMNS = frozenset(
    {"phone_hash", "display_name", "avatar_url", "home_zip", "age_attested_at"}
)


class UserRepo(Protocol):
    def upsert(self, uid: str, **fields: Any) -> dict[str, Any]:
        """Insert or update. Only non-None fields are written. Returns the row."""
        ...

    def get(self, uid: str) -> dict[str, Any] | None: ...
    def get_many(self, uids: list[str]) -> dict[str, dict[str, Any]]:
        """Batched read: one query for many uids (M10c). Returns {uid: row}."""
        ...
    def get_by_phone_hash(self, phone_hash: str) -> dict[str, Any] | None: ...
    def set_idv_status(self, uid: str, status: str) -> None: ...
    def delete(self, uid: str) -> bool:
        """Delete the user row. Returns True when a row existed.

        In Postgres, dependent rows cascade via ON DELETE CASCADE FKs
        (migration 0024 adds the two that were missing — C6).
        """
        ...


class PostgresUserRepo:
    def __init__(self, conn):
        self._conn = conn

    def upsert(self, uid: str, **fields: Any) -> dict[str, Any]:
        import psycopg

        clean = {k: v for k, v in fields.items() if v is not None}
        unknown = set(clean) - _UPSERTABLE_COLUMNS
        if unknown:
            raise TypeError(f"upsert() got unknown user columns: {sorted(unknown)}")
        try:
            if clean:
                cols = ["uid", *clean.keys()]
                placeholders = ", ".join(["%s"] * len(cols))
                updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in clean.keys())
                row = self._conn.execute(
                    f"INSERT INTO users ({', '.join(cols)}) VALUES ({placeholders}) "
                    f"ON CONFLICT (uid) DO UPDATE SET {updates} RETURNING *",
                    [uid, *clean.values()],
                ).fetchone()
            else:
                # No fields to write: ensure the row exists, then read it back.
                self._conn.execute(
                    "INSERT INTO users (uid) VALUES (%s) ON CONFLICT (uid) DO NOTHING",
                    (uid,),
                )
                row = self._conn.execute(
                    "SELECT * FROM users WHERE uid = %s", (uid,)
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise PhoneInUseError() from exc
        self._conn.commit()
        return dict(row)

    def get(self, uid: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM users WHERE uid = %s", (uid,)).fetchone()
        return dict(row) if row else None

    def get_many(self, uids: list[str]) -> dict[str, dict[str, Any]]:
        # M10c: one indexed query for a whole page of uids instead of N
        # per-uid round trips.
        if not uids:
            return {}
        rows = self._conn.execute(
            "SELECT * FROM users WHERE uid = ANY(%s)", (list(uids),)
        ).fetchall()
        return {r["uid"]: dict(r) for r in rows}

    def get_by_phone_hash(self, phone_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE phone_hash = %s", (phone_hash,)
        ).fetchone()
        return dict(row) if row else None

    def set_idv_status(self, uid: str, status: str) -> None:
        self._conn.execute("UPDATE users SET idv_status = %s WHERE uid = %s", (status, uid))
        self._conn.commit()

    def delete(self, uid: str) -> bool:
        cur = self._conn.execute("DELETE FROM users WHERE uid = %s", (uid,))
        self._conn.commit()
        return (cur.rowcount or 0) > 0


class MemoryUserRepo:
    """In-memory repo for tests. Enforces the same uniqueness invariants."""

    def __init__(self):
        self._rows: dict[str, dict[str, Any]] = {}

    def upsert(self, uid: str, **fields: Any) -> dict[str, Any]:
        clean = {k: v for k, v in fields.items() if v is not None}
        unknown = set(clean) - _UPSERTABLE_COLUMNS
        if unknown:
            raise TypeError(f"upsert() got unknown user columns: {sorted(unknown)}")
        phash = clean.get("phone_hash")
        if phash:
            for other_uid, other in self._rows.items():
                if other_uid != uid and other.get("phone_hash") == phash:
                    raise PhoneInUseError()
        row = self._rows.setdefault(
            uid, {"uid": uid, "idv_status": "unverified",
                  # Mirror Postgres DEFAULT now() so age-based rules (e.g. the
                  # new-account claim cap) behave the same in memory.
                  "created_at": datetime.now(timezone.utc).isoformat()}
        )
        row.update(clean)
        return dict(row)

    def get(self, uid: str) -> dict[str, Any] | None:
        row = self._rows.get(uid)
        return dict(row) if row else None

    def get_many(self, uids: list[str]) -> dict[str, dict[str, Any]]:
        return {u: dict(self._rows[u]) for u in dict.fromkeys(uids) if u in self._rows}

    def get_by_phone_hash(self, phone_hash: str) -> dict[str, Any] | None:
        for row in self._rows.values():
            if row.get("phone_hash") == phone_hash:
                return dict(row)
        return None

    def set_idv_status(self, uid: str, status: str) -> None:
        if uid in self._rows:
            self._rows[uid]["idv_status"] = status

    def delete(self, uid: str) -> bool:
        return self._rows.pop(uid, None) is not None


def get_user_repo(conn=Depends(get_db_conn)) -> UserRepo:
    return CachedUserRepo(PostgresUserRepo(conn))


# --- Cross-domain repo dependencies (C6) -----------------------------------
# users.py is a leaf module: listings / msg / claims / wantlist import *it*
# at their top level, so importing their repo factories at module scope
# would be circular. These thin wrappers defer the import to request time
# (all modules are loaded by then) and delegate to the canonical factory
# with the request-scoped connection. In tests, override THESE wrappers
# (not the canonical factories) via app.dependency_overrides.
def _listing_repo(conn=Depends(get_db_conn)):
    from .listings import get_listing_repo

    return get_listing_repo(conn=conn)


def _claim_repo(conn=Depends(get_db_conn)):
    from .claims import get_claim_repo

    return get_claim_repo(conn=conn)


def _want_repo(conn=Depends(get_db_conn)):
    from .wantlist import get_want_repo

    return get_want_repo(conn=conn)


def _notification_repo(conn=Depends(get_db_conn)):
    from .notify import get_notification_repo

    return get_notification_repo(conn=conn)


def _message_repo(conn=Depends(get_db_conn)):
    from .msg import get_message_repo

    return get_message_repo(conn=conn)


def _images_repo(conn=Depends(get_db_conn)):
    from .images import get_images_repo

    return get_images_repo(conn=conn)


def _blob_store():
    # No Depends(get_db_conn): the blob store is not a database repo.
    # Returns None (not an error) off the GCS backend so delete_me stays a
    # pure local no-op there.
    from .images import get_blob_store_or_none

    return get_blob_store_or_none()


# Max messages pulled per thread per page when assembling the export.
_EXPORT_MESSAGE_PAGE = 500


def _jsonable(value: Any) -> Any:
    """Recursively normalize DB rows to plain JSON.

    Memory repos store ISO strings already; Postgres returns datetime /
    Decimal objects. FastAPI would coerce these on the way out, but the
    export is also consumed directly, so normalize explicitly.
    """
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _export_listing(row: dict[str, Any]) -> dict[str, Any]:
    """Listing row for the export, with geo decrypted (it's the owner's data).

    Fail closed like every other encrypted-field read — a tampered or
    unkeyed coordinate raises instead of exporting ciphertext.
    """
    out = _jsonable(dict(row))
    for key in ("geo_lat", "geo_lon"):
        if key in out:
            out[key] = decrypt_float(out[key], GEO_KEY_ENV)
    return out


def _export_message(row: dict[str, Any]) -> dict[str, Any]:
    """Message sent by the user, body decrypted (same read boundary as the
    API's own message serializer)."""
    body = row.get("body")
    return {
        "id": str(row["id"]),
        "thread_id": str(row["thread_id"]),
        "kind": row.get("kind", "text"),
        "body": decrypt_text(body, MESSAGE_KEY_ENV) if isinstance(body, str) else body,
        "photo_url": row.get("photo_url"),
        "created_at": _jsonable(row.get("created_at")),
    }


def _claims_for_user(claim_repo: Any, uid: str) -> list[dict[str, Any]]:
    """Partial-quantity claims filed by ``uid``.

    ClaimRepo exposes no list-for-claimer query (claims.py is another
    workstream's file), so read the backing store directly; prefer a
    ``list_for_claimer`` method if one is ever added.
    """
    list_for_claimer = getattr(claim_repo, "list_for_claimer", None)
    if callable(list_for_claimer):
        return [_jsonable(c) for c in list_for_claimer(uid)]
    claims = getattr(claim_repo, "_claims", None)  # MemoryClaimRepo
    if claims is not None:
        rows = [c for c in claims.values() if c.get("claimer_uid") == uid]
        rows.sort(key=lambda c: c.get("created_at") or "")
        return [_jsonable(dict(c)) for c in rows]
    conn = getattr(claim_repo, "_conn", None)  # PostgresClaimRepo
    if conn is not None:
        db_rows = conn.execute(
            "SELECT id, listing_id, claimer_uid, quantity, status, "
            "pickup_start_ms, pickup_end_ms, notes, created_at "
            "FROM claims WHERE claimer_uid = %s ORDER BY created_at",
            (uid,),
        ).fetchall()
        return [_jsonable(dict(r)) for r in db_rows]
    return []


def _mirror_memory_cascade(uid: str, notification_repo: Any, listing_repo: Any) -> None:
    """Mirror the Postgres ON DELETE CASCADE in the in-memory fakes.

    The memory repos have no FK constraints, so without this the delete
    tests would observe orphans that real Postgres removes automatically
    (migration 0024: notification_log.user_uid, harvest_events.recorder_uid).
    Only the two tables named in C6 are mirrored — every other user-owned
    table already cascades in Postgres and is out of scope for the fakes.
    """
    from .listings import MemoryListingRepo
    from .notify import MemoryNotificationRepo

    if isinstance(notification_repo, MemoryNotificationRepo):
        notification_repo._log[:] = [
            e for e in notification_repo._log if e.get("user_uid") != uid
        ]
    if isinstance(listing_repo, MemoryListingRepo):
        listing_repo._harvest_events[:] = [
            e for e in listing_repo._harvest_events if e.get("recorder_uid") != uid
        ]


def _validate_profile(data: ProfileIn) -> None:
    if data.home_zip is not None and not ZIP_RE.match(data.home_zip):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_zip", "message": "home_zip must be a 5-digit ZIP code"},
        )
    if data.avatar_url is not None and not data.avatar_url.startswith(("https://", "http://")):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_avatar_url", "message": "avatar_url must be an http(s) URL"},
        )


def _attestation_gate(data: ProfileIn, row: dict[str, Any] | None) -> str | None:
    """M20c age gate. Returns the ISO timestamp to store when this request
    supplies the user's first 13+ attestation; ``None`` when attestation is
    already recorded. Raises 422 ``age_attestation_required`` when the user
    has not attested and this request doesn't either."""
    if row is not None and row.get("age_attested_at"):
        return None
    if data.age_attestation is not True:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "age_attestation_required",
                "message": (
                    "GardenSwap is for users 13 and older. Confirm with "
                    "age_attestation=true to continue. See Terms."
                ),
            },
        )
    return datetime.now(timezone.utc).isoformat()


@router.post("", response_model=OwnerProfile, status_code=200)
def upsert_profile(
    data: ProfileIn,
    uid: str = Depends(get_current_uid),
    repo: UserRepo = Depends(get_user_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
) -> dict[str, Any]:
    """Create or update the caller's own profile. uid comes from the ID token."""
    _validate_profile(data)
    # M20c: onboarding requires the 13+ attestation until it is recorded.
    attested_at = _attestation_gate(data, repo.get(uid))
    row = repo.upsert(
        uid,
        display_name=data.display_name,
        avatar_url=data.avatar_url,
        home_zip=data.home_zip,
        age_attested_at=attested_at,
    )
    # Idempotent: grants the 3-credit bootstrap exactly once, however the
    # user row was first created (profile upsert or phone verify).
    ensure_starter_credits(uid, credit_repo)
    out = owner_profile(row)
    _check_pii_leak(out)
    return out


@router.get("/me", response_model=OwnerProfile)
def get_me(
    uid: str = Depends(get_current_uid),
    repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    row = repo.get(uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "profile_not_found", "message": "No profile yet — POST /v1/users to create one"},
        )
    out = owner_profile(row)
    _check_pii_leak(out)
    return out


@router.patch("/me", response_model=OwnerProfile)
def patch_me(
    data: ProfileIn,
    uid: str = Depends(get_current_uid),
    repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    # /me is inherently owner-scoped: uid comes from the verified ID token.
    _validate_profile(data)
    # M20c: the gate applies here too until the attestation is recorded
    # (e.g. a user whose row was created by /v1/auth/verify but who never
    # completed onboarding).
    attested_at = _attestation_gate(data, repo.get(uid))
    row = repo.upsert(
        uid,
        display_name=data.display_name,
        avatar_url=data.avatar_url,
        home_zip=data.home_zip,
        age_attested_at=attested_at,
    )
    out = owner_profile(row)
    _check_pii_leak(out)
    return out


@router.get("/{uid}", response_model=PublicProfile)
def get_public_profile(
    uid: str,
    repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Anyone authenticated can see the public profile — no PII, no home_zip."""
    row = repo.get(uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "profile_not_found", "message": "No such user"},
        )
    out = public_profile(row)
    _check_pii_leak(out)
    return out


@router.delete("/me", status_code=204)
def delete_me(
    uid: str = Depends(get_current_uid),
    repo: UserRepo = Depends(get_user_repo),
    notification_repo: Any = Depends(_notification_repo),
    listing_repo: Any = Depends(_listing_repo),
    images_repo: Any = Depends(_images_repo),
    blob_store: Any = Depends(_blob_store),
) -> None:
    """Delete the caller's account and all of their data (C6).

    Deletion policy: immediate and irreversible — no grace period. This is
    acceptable pre-launch (no production data exists); revisit before launch
    if a retention/grace window becomes a compliance requirement.
    Dependent rows cascade in Postgres via ON DELETE CASCADE (migration
    0024 adds the two FKs that were missing); the in-memory repos used in
    tests mirror that cascade. The user's listings' photos are
    refcount-released from GCS *before* the cascade (after the rows are gone
    there is nothing left to read the photo URLs from). Idempotent: deleting
    twice still returns 204.
    """
    for listing in listing_repo.list_by_owner(uid):
        release_listing_images(
            listing.get("photos"),
            images_repo=images_repo,
            blob_store=blob_store,
            bucket=get_settings().gcs_bucket,
        )
    repo.delete(uid)
    _mirror_memory_cascade(uid, notification_repo, listing_repo)
    return None


@router.get("/me/export")
def export_me(
    uid: str = Depends(get_current_uid),
    repo: UserRepo = Depends(get_user_repo),
    listing_repo: Any = Depends(_listing_repo),
    claim_repo: Any = Depends(_claim_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
    want_repo: Any = Depends(_want_repo),
    notification_repo: Any = Depends(_notification_repo),
    message_repo: Any = Depends(_message_repo),
) -> dict[str, Any]:
    """Export everything the backend stores about the caller (C6).

    Shape — every section is scoped to the caller, never anyone else's rows:
    {
      "uid": ..., "exported_at": ...,
      "profile": <owner profile>,
      "listings": [...],        # listing rows incl. trees; geo decrypted
      "claims": [...],          # partial-quantity claims filed by the user
      "credit_ledger": [...],   # full append-only ledger history
      "want_list": [...],
      "harvest_events": [...],  # pick events on the user's listings
      "notification_prefs": {...} | null,
      "threads": [...],         # threads the user participates in
      "messages_sent": [...],    # messages sent by the user, bodies decrypted
    }
    Encrypted fields are decrypted for the owner (fail closed on tamper /
    missing key, like every other read path). Datetimes are ISO strings.
    """
    row = repo.get(uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "profile_not_found",
                    "message": "No profile yet — nothing to export"},
        )
    listings = listing_repo.list_by_owner(uid)
    listing_ids = [str(lst["id"]) for lst in listings]
    harvest_events = [
        event
        for listing_id in listing_ids
        for event in listing_repo.list_harvest_events(listing_id)
    ]
    threads = message_repo.list_threads_for(uid, listing_ids)
    messages_sent: list[dict[str, Any]] = []
    for thread in threads:
        offset = 0
        while True:
            page = message_repo.list_messages(thread["id"], offset, _EXPORT_MESSAGE_PAGE)
            if not page:
                break
            messages_sent.extend(
                _export_message(m) for m in page if m.get("sender_uid") == uid
            )
            if len(page) < _EXPORT_MESSAGE_PAGE:
                break
            offset += _EXPORT_MESSAGE_PAGE
    return {
        "uid": uid,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile": owner_profile(row),
        "listings": [_export_listing(lst) for lst in listings],
        "claims": _claims_for_user(claim_repo, uid),
        "credit_ledger": [_jsonable(e) for e in credit_repo.entries(uid)],
        "want_list": [_jsonable(w) for w in want_repo.list_for_user(uid)],
        "harvest_events": [_jsonable(e) for e in harvest_events],
        "notification_prefs": _jsonable(notification_repo.get_prefs(uid)),
        "threads": [_jsonable(t) for t in threads],
        "messages_sent": messages_sent,
    }
