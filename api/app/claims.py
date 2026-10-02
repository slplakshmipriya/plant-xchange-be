"""Partial-quantity claims lifecycle (R2 claims track).

Distinct from the whole-listing ``POST /v1/listings/{id}/claim`` (API-060),
which flips ``live -> claimed`` for the entire listing: this track models
per-quantity claims (e.g. 2kg out of a 10kg harvest) with their own claim
records and a pending/accepted/declined lifecycle:

- ``POST /v1/listings/{id}/claims`` — create a pending claim. Atomically
  decrements the listing's available quantity; the listing stays live until
  the quantity hits 0 (then it closes) or the listing expires. The
  ``insufficient_credits`` gate checks the claimer can afford the listing's
  ``credit_cost``; no credits move yet.
- ``POST /v1/listings/{id}/claims/cancel`` — claimer OR giver cancels before
  completion; quantity is restored. Cancelling a pending claim moves no
  credits; cancelling an accepted claim reverses the accept-time money leg
  (``claim_reversal``) so the claim nets to zero.
- ``POST /v1/listings/{id}/claims/accept`` — giver only; pending -> accepted.
  The money leg posts here: the claimer spends the listing's ``credit_cost``
  (``claim_spend``) and the giver earns it (``claim_earn``), idempotent per
  claim via ``claim:<id>:spend`` / ``claim:<id>:earn`` — the same pattern as
  ``exchange.confirm`` and ``slots.claim``.
- ``POST /v1/listings/{id}/claims/decline`` — giver only; pending -> declined;
  quantity restored.
- ``POST /v1/exchanges/{id}/no-show`` — record a no-show on one side
  (claimer|giver) of an exchange. 2 no-shows -> 30-day claim suspension,
  enforced on the claim endpoint (403).
- New accounts (< 14 days old) are capped at 5 claims per rolling 7 days.

Suspension lookup lives in the module-local ``check_pillar_suspension()``
helper — deliberately NOT in ``app/moderation.py`` (owned by another track,
do not create or import it). The coordinator rewires the helper to the shared
moderation module after merge.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import get_current_uid
from .credits import CreditRepo, get_credit_repo
from .db import get_db_conn
from .listings import ListingRepo, batch_owners, get_listing_repo, public_listing, utcnow
from .moderation import ModerationRepo, get_moderation_repo, get_suspension
from .notify import NotificationRepo, get_notification_repo, send_notification
from .users import UserRepo, get_user_repo

router = APIRouter(prefix="/v1", tags=["claims"])

CLAIM_STATUSES = ("pending", "accepted", "declined", "cancelled")
CLAIM_CATEGORY = "claim"

# Pillar suspension policy for the claims pillar.
NO_SHOW_SUSPENSION_THRESHOLD = 2
NO_SHOW_SUSPENSION_DAYS = 30
NEW_ACCOUNT_AGE_DAYS = 14
NEW_ACCOUNT_CLAIM_CAP = 5
NEW_ACCOUNT_CLAIM_WINDOW_DAYS = 7

# Tolerance for float dust in quantity comparisons (M5b): remaining_qty is
# NUMERIC on the Postgres path but the decrement binds a Python float, so
# the subtraction evaluates in float8 and can leave dust (e.g. 2.8e-17).
_EPSILON = 1e-9


# ---------------------------------------------------------------- repository

class ClaimRepo(Protocol):
    def create(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def get(self, claim_id: str) -> dict[str, Any] | None: ...
    def set_status(self, claim_id: str, status: str) -> dict[str, Any] | None: ...
    def active_claim_for(self, listing_id: str, claimer_uid: str) -> dict[str, Any] | None:
        """Newest pending/accepted claim by this claimer on this listing."""
        ...
    def latest_active_for_listing(self, listing_id: str) -> dict[str, Any] | None:
        """Newest pending/accepted claim on this listing (any claimer)."""
        ...
    def count_recent_claims(self, claimer_uid: str, since: datetime) -> int:
        """Claims created by this claimer at or after ``since``."""
        ...
    def record_no_show(self, uid: str) -> dict[str, Any]:
        """Increment the user's no-show strikes; suspend when the threshold
        is hit. Returns the updated strike row."""
        ...
    def get_strikes(self, uid: str) -> dict[str, Any] | None: ...


class PostgresClaimRepo:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        d["id"] = str(d["id"])
        d["listing_id"] = str(d["listing_id"])
        d["quantity"] = float(d["quantity"])
        c = d.get("created_at")
        d["created_at"] = c.isoformat() if hasattr(c, "isoformat") else c
        return d

    @staticmethod
    def _strike(row) -> dict:
        d = dict(row)
        for k in ("suspended_until", "banned_until"):
            v = d.get(k)
            d[k] = v.isoformat() if hasattr(v, "isoformat") else v
        return d

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        row = self._conn.execute(
            "INSERT INTO claims (id, listing_id, claimer_uid, quantity, status, "
            "pickup_start_ms, pickup_end_ms, notes) "
            "VALUES (%s,%s,%s,%s,'pending',%s,%s,%s) RETURNING *",
            (
                data["id"], data["listing_id"], data["claimer_uid"], data["quantity"],
                data.get("pickup_start_ms"), data.get("pickup_end_ms"), data.get("notes"),
            ),
        ).fetchone()
        self._conn.commit()
        return self._row(row)

    def get(self, claim_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM claims WHERE id = %s", (claim_id,)).fetchone()
        return self._row(row) if row else None

    def set_status(self, claim_id: str, status: str) -> dict[str, Any] | None:
        # Conditional flip: only pending/accepted claims may move. Two
        # concurrent cancels (or accept+decline racing) serialize here —
        # the loser gets rowcount 0 -> None -> a 409 at the route layer,
        # instead of both succeeding and restoring the quantity twice (H8).
        cur = self._conn.execute(
            "UPDATE claims SET status = %s WHERE id = %s "
            "AND status IN ('pending','accepted')",
            (status, claim_id),
        )
        self._conn.commit()
        return self.get(claim_id) if (cur.rowcount or 0) > 0 else None

    def active_claim_for(self, listing_id: str, claimer_uid: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM claims WHERE listing_id = %s AND claimer_uid = %s "
            "AND status IN ('pending','accepted') "
            "ORDER BY created_at DESC LIMIT 1",
            (listing_id, claimer_uid),
        ).fetchone()
        return self._row(row) if row else None

    def latest_active_for_listing(self, listing_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM claims WHERE listing_id = %s "
            "AND status IN ('pending','accepted') "
            "ORDER BY created_at DESC LIMIT 1",
            (listing_id,),
        ).fetchone()
        return self._row(row) if row else None

    def count_recent_claims(self, claimer_uid: str, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM claims WHERE claimer_uid = %s AND created_at >= %s",
            (claimer_uid, since),
        ).fetchone()
        return int(row["n"])

    def record_no_show(self, uid: str) -> dict[str, Any]:
        row = self._conn.execute(
            "INSERT INTO claim_no_show_strikes (uid, no_shows) VALUES (%s, 1) "
            "ON CONFLICT (uid) DO UPDATE SET no_shows = claim_no_show_strikes.no_shows + 1 "
            "RETURNING *",
            (uid,),
        ).fetchone()
        if int(row["no_shows"]) >= NO_SHOW_SUSPENSION_THRESHOLD:
            suspended_until = utcnow() + timedelta(days=NO_SHOW_SUSPENSION_DAYS)
            row = self._conn.execute(
                "UPDATE claim_no_show_strikes SET suspended_until = %s WHERE uid = %s "
                "RETURNING *",
                (suspended_until, uid),
            ).fetchone()
        self._conn.commit()
        return self._strike(row)

    def get_strikes(self, uid: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM claim_no_show_strikes WHERE uid = %s", (uid,)
        ).fetchone()
        return self._strike(row) if row else None


class MemoryClaimRepo:
    def __init__(self):
        self._claims: dict[str, dict[str, Any]] = {}
        self._strikes: dict[str, dict[str, Any]] = {}

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        row = {
            "id": data["id"],
            "listing_id": data["listing_id"],
            "claimer_uid": data["claimer_uid"],
            "quantity": float(data["quantity"]),
            "status": "pending",
            "pickup_start_ms": data.get("pickup_start_ms"),
            "pickup_end_ms": data.get("pickup_end_ms"),
            "notes": data.get("notes"),
            "created_at": utcnow().isoformat(),
        }
        self._claims[row["id"]] = row
        return dict(row)

    def get(self, claim_id: str) -> dict[str, Any] | None:
        row = self._claims.get(claim_id)
        return dict(row) if row else None

    def set_status(self, claim_id: str, status: str) -> dict[str, Any] | None:
        # Memory-path equivalent of the conditional Postgres flip (H8):
        # check-and-set under a lock so a concurrent transition can't
        # slip between the read and the write.
        with _memory_claim_lock:
            row = self._claims.get(claim_id)
            if row is None or row["status"] not in ("pending", "accepted"):
                return None
            row["status"] = status
            return dict(row)

    def active_claim_for(self, listing_id: str, claimer_uid: str) -> dict[str, Any] | None:
        cands = [c for c in self._claims.values()
                 if c["listing_id"] == listing_id
                 and c["claimer_uid"] == claimer_uid
                 and c["status"] in ("pending", "accepted")]
        cands.sort(key=lambda c: c["created_at"], reverse=True)
        return dict(cands[0]) if cands else None

    def latest_active_for_listing(self, listing_id: str) -> dict[str, Any] | None:
        cands = [c for c in self._claims.values()
                 if c["listing_id"] == listing_id and c["status"] in ("pending", "accepted")]
        cands.sort(key=lambda c: c["created_at"], reverse=True)
        return dict(cands[0]) if cands else None

    def count_recent_claims(self, claimer_uid: str, since: datetime) -> int:
        n = 0
        for c in self._claims.values():
            if c["claimer_uid"] != claimer_uid:
                continue
            created = c.get("created_at")
            created_dt = (datetime.fromisoformat(created) if isinstance(created, str)
                          else created)
            if created_dt is not None and created_dt >= since:
                n += 1
        return n

    def record_no_show(self, uid: str) -> dict[str, Any]:
        strike = self._strikes.setdefault(
            uid, {"uid": uid, "no_shows": 0, "suspended_until": None, "banned_until": None}
        )
        strike["no_shows"] += 1
        if strike["no_shows"] >= NO_SHOW_SUSPENSION_THRESHOLD:
            strike["suspended_until"] = (
                utcnow() + timedelta(days=NO_SHOW_SUSPENSION_DAYS)).isoformat()
        return dict(strike)

    def get_strikes(self, uid: str) -> dict[str, Any] | None:
        strike = self._strikes.get(uid)
        return dict(strike) if strike else None


def get_claim_repo(conn=Depends(get_db_conn)) -> ClaimRepo:
    return PostgresClaimRepo(conn)


# ---------------------------------------------------------------- suspension

# Guards for the memory-path equivalents of the atomic Postgres operations
# (single process only — the Postgres path serializes in the database).
_memory_claim_lock = threading.Lock()
_restore_lock = threading.Lock()
_spend_locks: dict[str, threading.Lock] = {}
_spend_locks_guard = threading.Lock()


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _memory_spend_lock(uid: str) -> threading.Lock:
    with _spend_locks_guard:
        return _spend_locks.setdefault(uid, threading.Lock())


@contextmanager
def serialize_spend(uid: str, credit_repo: Any) -> Iterator[None]:
    """Serialize a balance-check + spend-post sequence per uid (H11).

    Without this, two concurrent spends by one user both pass the
    ``balance < cost`` gate and both post, driving the derived balance
    negative — nothing in the DB prevents it because the balance is
    computed in Python.

    Postgres path: ``pg_advisory_lock(hashtext(uid))`` held across the
    check and the spend post (session-level lock; released in ``finally``).
    Memory path (used by tests): a per-uid ``threading.Lock`` — the
    single-process equivalent.
    """
    conn = getattr(credit_repo, "_conn", None)
    if conn is None:
        with _memory_spend_lock(uid):
            yield
        return
    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (uid,))
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (uid,))


def check_pillar_suspension(
    uid: str, pillar: str, repo: ClaimRepo | None = None
) -> dict[str, Any] | None:
    """Module-local suspension lookup for the claims pillar.

    Reads the no-show strikes / suspensions state owned by this track.
    Returns None when the user is clear, otherwise a suspension dict with
    ``pillar``, ``reason``, and the active ``until`` timestamp.

    ``repo`` is required — there is deliberately no module-global fallback:
    reusing another request's connection would be unsafe (psycopg
    connections aren't thread-safe), so a missing repo fails loudly (L2).
    The coordinator rewires this helper to the shared moderation module
    after merge — keep the ``(uid, pillar)`` call shape stable.
    """
    if pillar != "claims":
        return None
    if repo is None:
        raise RuntimeError(
            "check_pillar_suspension requires an explicit repo (no global fallback)"
        )
    strikes = repo.get_strikes(uid)
    if not strikes:
        return None
    now = utcnow()
    banned_until = _parse_dt(strikes.get("banned_until"))
    if banned_until and banned_until > now:
        return {"pillar": pillar, "reason": "banned", "banned": True,
                "until": strikes["banned_until"]}
    suspended_until = _parse_dt(strikes.get("suspended_until"))
    if suspended_until and suspended_until > now:
        return {"pillar": pillar, "reason": "no_show_strikes",
                "no_shows": strikes.get("no_shows", 0),
                "until": strikes["suspended_until"]}
    return None


def _suspension_error(suspension: dict[str, Any]) -> HTTPException:
    if suspension.get("banned"):
        message = "Your account is banned from claiming"
    else:
        message = (
            f"Claiming suspended until {suspension['until']}: "
            f"{suspension.get('no_shows', 0)} no-shows recorded"
        )
    return HTTPException(
        status_code=403,
        detail={"code": "claim_suspended", "message": message},
    )


def _enforce_claim_eligibility(
    uid: str, claim_repo: ClaimRepo, user_repo: UserRepo, mod_repo: ModerationRepo
) -> None:
    """403 when the caller is pillar-suspended/banned (no-show strikes or
    moderation strikes), or when a new account has exhausted its rolling
    claim cap."""
    suspension = check_pillar_suspension(uid, "claims", claim_repo)
    if suspension is not None:
        raise _suspension_error(suspension)
    mod_susp = get_suspension(mod_repo, uid, "claims")
    if mod_susp is not None:
        raise HTTPException(
            status_code=403,
            detail={"code": "claim_suspended",
                    "message": f"Claiming suspended ({mod_susp['type']}): {mod_susp['reason']}"},
        )
    user = user_repo.get(uid)
    created = _parse_dt(user.get("created_at")) if user else None
    if created is not None and utcnow() - created < timedelta(days=NEW_ACCOUNT_AGE_DAYS):
        since = utcnow() - timedelta(days=NEW_ACCOUNT_CLAIM_WINDOW_DAYS)
        if claim_repo.count_recent_claims(uid, since) >= NEW_ACCOUNT_CLAIM_CAP:
            raise HTTPException(
                status_code=403,
                detail={"code": "new_account_claim_cap",
                        "message": (f"Accounts under {NEW_ACCOUNT_AGE_DAYS} days old are "
                                    f"capped at {NEW_ACCOUNT_CLAIM_CAP} claims per "
                                    f"{NEW_ACCOUNT_CLAIM_WINDOW_DAYS} days")},
            )


# ---------------------------------------------------------------- API models

class ClaimIn(BaseModel):
    # gt=0 (not ge=1): harvest listings can be fractional (e.g. 0.5 kg),
    # and such a listing could never be claimed with a minimum of 1 (L3).
    quantity: float = Field(gt=0)
    pickupStartMs: int | None = Field(default=None, ge=1)
    pickupEndMs: int | None = Field(default=None, ge=1)
    notes: str | None = Field(default=None, max_length=1000)


class ClaimDecisionIn(BaseModel):
    claimId: str = Field(min_length=1)


class NoShowIn(BaseModel):
    side: str = Field(pattern="^(claimer|giver)$")


def _get_live_listing(
    listing_id: str, uid: str, listing_repo: ListingRepo
) -> dict[str, Any]:
    row = listing_repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    if row["owner_uid"] == uid:
        raise HTTPException(422, {"code": "cannot_claim_own",
                                  "message": "You cannot claim your own listing"})
    if row["status"] != "live":
        raise HTTPException(422, {"code": "listing_not_live",
                                  "message": f"Cannot claim a '{row['status']}' listing"})
    return row


def _available_quantity(row: dict[str, Any]) -> float | None:
    avail = row.get("remaining_qty")
    if avail is None:
        avail = row.get("quantity")
    return float(avail) if avail is not None else None


@router.post("/listings/{listing_id}/claims", tags=["claims"])
def create_claim(
    listing_id: str,
    data: ClaimIn,
    uid: str = Depends(get_current_uid),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    claim_repo: ClaimRepo = Depends(get_claim_repo),
    user_repo: UserRepo = Depends(get_user_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
    notify_repo: NotificationRepo = Depends(get_notification_repo),
    mod_repo: ModerationRepo = Depends(get_moderation_repo),
) -> dict[str, Any]:
    """Claim part of a listing's quantity. The claim starts pending; the
    available quantity drops atomically; the listing stays live until the
    quantity hits 0 (then it closes) or the listing expires."""
    row = _get_live_listing(listing_id, uid, listing_repo)
    if (data.pickupStartMs is not None and data.pickupEndMs is not None
            and data.pickupEndMs <= data.pickupStartMs):
        raise HTTPException(422, {"code": "invalid_window",
                                  "message": "pickupEndMs must be after pickupStartMs"})
    if user_repo.get(uid) is None:
        raise HTTPException(400, {"code": "profile_required",
                                  "message": "Create a profile (POST /v1/users) before claiming"})
    _enforce_claim_eligibility(uid, claim_repo, user_repo, mod_repo)
    if credit_repo.balance(uid) < row["credit_cost"]:
        raise HTTPException(422, {"code": "insufficient_credits",
                                  "message": "Not enough credits — give before you claim"})
    available = _available_quantity(row)
    if available is None:
        raise HTTPException(422, {"code": "quantity_not_tracked",
                                  "message": "This listing has no quantity to claim from"})
    if data.quantity > available + _EPSILON:
        raise HTTPException(409, {"code": "insufficient_quantity",
                                  "message": f"Only {available:g} available to claim"})
    # Clamp float dust: claiming the last of the pool when only dust
    # remains decrements what is actually there (M5b) instead of
    # spuriously 409ing inside decrement_remaining.
    delta = min(data.quantity, available)
    # Atomic: concurrent claimants cannot oversell; the loser gets None.
    updated = listing_repo.decrement_remaining(listing_id, delta)
    if updated is None:
        raise HTTPException(409, {"code": "insufficient_quantity",
                                  "message": "Someone just claimed the remaining quantity"})
    claim = claim_repo.create({
        "id": str(uuid.uuid4()),
        "listing_id": listing_id,
        "claimer_uid": uid,
        "quantity": data.quantity,
        "pickup_start_ms": data.pickupStartMs,
        "pickup_end_ms": data.pickupEndMs,
        "notes": data.notes.strip() if data.notes else None,
    })
    remaining = float(updated.get("remaining_qty") or 0)
    # Epsilon compare (M5b): remaining_qty is NUMERIC on the Postgres path
    # but the decrement binds a Python float, so the subtraction evaluates
    # in float8 and a final exact-quantity claim can leave float dust
    # (e.g. 2.8e-17) instead of exactly 0. ``== 0`` would then keep the
    # listing live with ~0 quantity forever; the epsilon treats dust as
    # fully picked.
    if abs(remaining) < _EPSILON:
        # Fully claimed: walk the legal transitions, no state-machine bypass.
        listing_repo.set_status(listing_id, "claimed")
        updated = listing_repo.set_status(listing_id, "completed")
    # Tell the giver a claim is waiting on them (accept/decline).
    send_notification(
        row["owner_uid"],
        CLAIM_CATEGORY,
        "New claim on your listing",
        f"{uid} claimed {data.quantity:g} of {row.get('variety') or 'your listing'}.",
        data={"listing_id": listing_id, "claim_id": claim["id"]},
        ref=f"claim:{claim['id']}",
        repo=notify_repo,
    )
    return {"listing": public_listing(updated,
                                       owners=batch_owners(user_repo, [updated])),
            "claim": _public_claim(claim)}


def _public_claim(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(claim["id"]),
        "listing_id": str(claim["listing_id"]),
        "claimer_uid": claim["claimer_uid"],
        "quantity": float(claim["quantity"]),
        "status": claim["status"],
        "pickupStartMs": claim.get("pickup_start_ms"),
        "pickupEndMs": claim.get("pickup_end_ms"),
        "notes": claim.get("notes"),
        "created_at": claim.get("created_at"),
    }


def _restore_quantity(
    listing_id: str, quantity: float, listing_repo: ListingRepo
) -> dict[str, Any]:
    """Give the claimed quantity back to the listing's available pool.

    Single atomic statement on the Postgres path (H7): the old
    read-modify-write (get -> compute base + quantity -> update) lost
    updates under concurrency (two concurrent +2 restores on base 5 ended
    at 7.0, not 9.0). The delta is cast to ``::numeric`` so the addition
    stays in the NUMERIC domain instead of float8 (M5b). The memory path
    holds a module lock for the read-compute-write sequence — the
    single-process equivalent.
    """
    conn = getattr(listing_repo, "_conn", None)
    if conn is not None:
        row = listing_repo.get(listing_id)
        if row is None:
            raise HTTPException(404, {"code": "listing_not_found",
                                      "message": "No such listing"})
        conn.execute(
            "UPDATE listings SET remaining_qty = LEAST(COALESCE(quantity,0), "
            "COALESCE(remaining_qty, quantity) + %s::numeric) WHERE id = %s",
            (quantity, listing_id),
        )
        conn.commit()
        return listing_repo.get(listing_id)
    with _restore_lock:
        row = listing_repo.get(listing_id)
        if row is None:
            raise HTTPException(404, {"code": "listing_not_found",
                                      "message": "No such listing"})
        base = row.get("remaining_qty")
        if base is None:
            base = row.get("quantity") or 0
        new_remaining = float(base) + quantity
        cap = float(row["quantity"]) if row.get("quantity") is not None else None
        if cap is not None and new_remaining > cap:
            new_remaining = cap
        return listing_repo.update(listing_id, {"remaining_qty": new_remaining})


@router.post("/listings/{listing_id}/claims/cancel", tags=["claims"])
def cancel_claim(
    listing_id: str,
    uid: str = Depends(get_current_uid),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    claim_repo: ClaimRepo = Depends(get_claim_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Cancel a claim before completion. Claimer or giver; the quantity is
    restored; no credits move."""
    row = listing_repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    if uid == row["owner_uid"]:
        # Giver: cancel the newest active claim on the listing.
        claim = claim_repo.latest_active_for_listing(listing_id)
    else:
        claim = claim_repo.active_claim_for(listing_id, uid)
    if claim is None:
        raise HTTPException(404, {"code": "no_active_claim",
                                  "message": "You have no active claim on this listing"})
    if claim["status"] not in ("pending", "accepted"):
        raise HTTPException(409, {"code": "claim_not_active",
                                  "message": f"Cannot cancel a '{claim['status']}' claim"})
    if claim["status"] == "accepted":
        # Unwind the accept-time money leg (same idempotency pattern as the
        # forward leg, so a retried cancel cannot double-refund). Posted
        # before the status flip, mirroring exchange.confirm.
        cost = row["credit_cost"]
        credit_repo.add_entry(claim["claimer_uid"], cost, "claim_reversal",
                              ref_id=claim["id"],
                              idempotency_key=f"claim:{claim['id']}:reversal:claimer")
        credit_repo.add_entry(row["owner_uid"], -cost, "claim_reversal",
                              ref_id=claim["id"],
                              idempotency_key=f"claim:{claim['id']}:reversal:giver")
    claim = claim_repo.set_status(claim["id"], "cancelled")
    if claim is None:
        # Lost a race with a concurrent accept/decline (H8): the
        # conditional flip in set_status refused the transition.
        raise HTTPException(409, {"code": "claim_not_active",
                                  "message": "Claim was already resolved by a concurrent action"})
    updated = _restore_quantity(listing_id, float(claim["quantity"]), listing_repo)
    return {"listing": public_listing(updated,
                                       owners=batch_owners(user_repo, [updated])),
            "claim": _public_claim(claim)}


@router.post("/listings/{listing_id}/claims/accept", tags=["claims"])
def accept_claim(
    listing_id: str,
    data: ClaimDecisionIn,
    uid: str = Depends(get_current_uid),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    claim_repo: ClaimRepo = Depends(get_claim_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Giver accepts a pending claim."""
    row = listing_repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    if row["owner_uid"] != uid:
        raise HTTPException(403, {"code": "not_giver",
                                  "message": "Only the giver can accept claims"})
    claim = claim_repo.get(data.claimId)
    if claim is None or str(claim["listing_id"]) != listing_id:
        raise HTTPException(404, {"code": "claim_not_found", "message": "No such claim"})
    if claim["status"] != "pending":
        raise HTTPException(409, {"code": "claim_not_pending",
                                  "message": f"Cannot accept a '{claim['status']}' claim"})
    # C2 money leg: the claimer spends the listing's credit_cost and the
    # giver earns it, idempotent per claim (claim:<id>:spend / claim:<id>:earn)
    # — the same pattern as exchange.confirm and slots.claim.
    cost = row["credit_cost"]
    claimer_uid = claim["claimer_uid"]
    # Balance can change between claim creation and accept — recheck so an
    # accept can never drive a balance negative (same as exchange.confirm).
    # The recheck and the spend post are serialized per claimer (H11): two
    # concurrent accepts would otherwise both pass the gate and both spend.
    with serialize_spend(claimer_uid, credit_repo):
        if credit_repo.balance(claimer_uid) < cost:
            raise HTTPException(422, {"code": "insufficient_credits",
                                      "message": "Claimer no longer has enough credits"})
        # Entries land before the status flip so a crash mid-flight is
        # recoverable by retry: re-adds are idempotent no-ops, then the flip
        # completes.
        credit_repo.add_entry(claimer_uid, -cost, "claim_spend",
                              ref_id=claim["id"],
                              idempotency_key=f"claim:{claim['id']}:spend")
    credit_repo.add_entry(row["owner_uid"], cost, "claim_earn",
                          ref_id=claim["id"],
                          idempotency_key=f"claim:{claim['id']}:earn")
    claim = claim_repo.set_status(claim["id"], "accepted")
    if claim is None:
        # Lost a race with a concurrent cancel/decline (H8).
        raise HTTPException(409, {"code": "claim_not_pending",
                                  "message": "Claim is no longer pending"})
    accepted_listing = listing_repo.get(listing_id)
    return {"listing": public_listing(accepted_listing,
                                       owners=batch_owners(user_repo, [accepted_listing])),
            "claim": _public_claim(claim)}


@router.post("/listings/{listing_id}/claims/decline", tags=["claims"])
def decline_claim(
    listing_id: str,
    data: ClaimDecisionIn,
    uid: str = Depends(get_current_uid),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    claim_repo: ClaimRepo = Depends(get_claim_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Giver declines a pending claim; the quantity is restored."""
    row = listing_repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    if row["owner_uid"] != uid:
        raise HTTPException(403, {"code": "not_giver",
                                  "message": "Only the giver can decline claims"})
    claim = claim_repo.get(data.claimId)
    if claim is None or str(claim["listing_id"]) != listing_id:
        raise HTTPException(404, {"code": "claim_not_found", "message": "No such claim"})
    if claim["status"] != "pending":
        raise HTTPException(409, {"code": "claim_not_pending",
                                  "message": f"Cannot decline a '{claim['status']}' claim"})
    claim = claim_repo.set_status(claim["id"], "declined")
    if claim is None:
        # Lost a race with a concurrent cancel/accept (H8).
        raise HTTPException(409, {"code": "claim_not_pending",
                                  "message": "Claim is no longer pending"})
    updated = _restore_quantity(listing_id, float(claim["quantity"]), listing_repo)
    return {"listing": public_listing(updated,
                                       owners=batch_owners(user_repo, [updated])),
            "claim": _public_claim(claim)}


@router.post("/exchanges/{listing_id}/no-show", tags=["claims"])
def record_no_show(
    listing_id: str,
    data: NoShowIn,
    uid: str = Depends(get_current_uid),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    claim_repo: ClaimRepo = Depends(get_claim_repo),
) -> dict[str, Any]:
    """Record a no-show against one side of an exchange. 2 no-shows -> a
    30-day claim suspension for that user."""
    row = listing_repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    claim = claim_repo.latest_active_for_listing(listing_id)
    if claim is None:
        raise HTTPException(422, {"code": "no_active_claim",
                                  "message": "No active claim to report a no-show on"})
    target_uid = claim["claimer_uid"] if data.side == "claimer" else row["owner_uid"]
    if uid not in (row["owner_uid"], claim["claimer_uid"]):
        raise HTTPException(403, {"code": "not_a_party",
                                  "message": "Only the giver and claimer can report a no-show"})
    strike = claim_repo.record_no_show(target_uid)
    return {
        "uid": target_uid,
        "side": data.side,
        "no_shows": strike["no_shows"],
        "suspended_until": strike.get("suspended_until"),
    }


@router.get("/me/swaps", tags=["claims"])
def my_swaps(
    uid: str = Depends(get_current_uid),
    conn=Depends(get_db_conn),
) -> dict[str, Any]:
    """List the caller's swap history (claims where they are claimer or giver).

    Returns both sides: claims the user made on others' listings (counterparty
    = listing owner) and claims others made on the user's listings (counterparty
    = claimer). Status maps claim lifecycle to the app's BookingStatus.
    """
    status_map = {
        "pending": "REQUESTED",
        "accepted": "CONFIRMED",
        "declined": "CANCELLED",
        "cancelled": "CANCELLED",
    }
    rows = conn.execute(
        """
        SELECT c.id AS swap_id,
               c.status AS claim_status,
               c.created_at,
               l.variety AS listing_title,
               CASE
                 WHEN c.claimer_uid = %s THEN owner.display_name
                 ELSE claimer.display_name
               END AS counterparty,
               CASE
                 WHEN c.claimer_uid = %s THEN 'claimer'
                 ELSE 'giver'
               END AS role
        FROM claims c
        JOIN listings l ON l.id = c.listing_id
        LEFT JOIN users owner ON owner.uid = l.owner_uid
        LEFT JOIN users claimer ON claimer.uid = c.claimer_uid
        WHERE c.claimer_uid = %s OR l.owner_uid = %s
        ORDER BY c.created_at DESC
        LIMIT 100
        """,
        (uid, uid, uid, uid),
    ).fetchall()
    swaps = [
        {
            "swap_id": str(r["swap_id"]),
            "counterparty": r["counterparty"] or "A gardener",
            "listing_title": r["listing_title"] or "A listing",
            "status": status_map.get(r["claim_status"], "REQUESTED"),
            "role": r["role"],
        }
        for r in rows
    ]
    return {"swaps": swaps}
