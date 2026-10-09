"""Trust & Safety: reports, strikes/suspensions, and dispute resolution (R2 track).

STRIKE / SUSPENSION RULES
-------------------------
- ``record_strike(repo, user_id, pillar, reason, verified)`` records one
  complaint outcome against a user. ``pillar`` is the product area the offense
  maps to (e.g. "listings", "exchange", "messaging", "sitting"). Only *verified*
  complaints count toward enforcement — unverified ones are kept for reviewer
  context but never trigger enforcement on their own.
- Enforcement: 2 verified strikes against the same user in the same pillar ->
  a 90-day suspension from that pillar. Counting is per (user, pillar).
- Fraud is special: a verified strike whose ``reason`` is the report category
  ``"fraud"`` (case-insensitive) -> a permanent ban across all pillars, on the
  first verified offense. Callers passing a report category through to
  ``record_strike`` should use the category string as ``reason``.
- Bans outrank suspensions: ``get_suspension`` returns the ban for every
  pillar while one is on record, regardless of per-pillar suspensions.

SUPPORT GATING
--------------
- Dispute resolution (``POST /v1/disputes/{id}/resolve``) is support-staff
  only. auth.py has no role concept, so the allowed support uids are
  configured via the ``SUPPORT_UIDS`` environment variable (comma-separated
  Firebase uids). The env var is read fresh on every call so tests can
  monkeypatch it. Default (unset or empty): *no one* can resolve disputes —
  fail closed.

STABLE CONTRACT FOR OTHER TRACKS
--------------------------------
- ``get_suspension(repo, uid, pillar) -> None | dict`` is the single call other
  tracks use to enforce trust decisions. Shape::

      None
          -- no active enforcement
      {"type": "suspension", "untilMs": <int epoch ms>, "reason": <str>}
      {"type": "ban", "untilMs": None, "reason": <str>}

  Suspension ``untilMs`` is the UTC epoch-millisecond expiry; bans never
  expire, so ``untilMs`` is None. This signature and shape are stable —
  other tracks stub it locally until this track merges.

- Disputes reference exchanges by listing id (an "exchange" in this codebase
  is the claim/confirm flow on a listing, keyed by listing id — see
  exchange.py). A dispute can only be opened on a *completed* exchange by one
  of its two parties.
- Dispute resolutions that uphold a complaint may credit the claimer and debit
  the giver. Those entries go through the existing credit ledger, which is
  append-only: reversal entries are *added* (``dispute_reversal``) and existing
  entries are never mutated.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import get_current_uid
from .credits import CreditRepo, EarnCapExceededError, get_credit_repo
from .db import get_db_conn
from .listings import ListingRepo, get_listing_repo
from .vertical import get_vertical

router = APIRouter(prefix="/v1", tags=["moderation"])

# A verified fraud complaint bans on first offense; two verified complaints in
# one pillar suspend from that pillar for 90 days.
SUSPENSION_DAYS = 90
FRAUD_CATEGORY = "fraud"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


class TargetType(str, Enum):
    LISTING = "LISTING"
    USER = "USER"
    BOOKING = "BOOKING"


class ReportCategory(str, Enum):
    spam = "spam"
    safety = "safety"
    fraud = "fraud"
    inappropriate = "inappropriate"
    other = "other"


class DisputeOutcome(str, Enum):
    upheld = "upheld"
    rejected = "rejected"


class SupportViewReason(str, Enum):
    """Controlled vocabulary for the support message dashboard's ``reason``.

    H2: the free-text justification was self-reported and unauditable, so it
    is now an enum — an invalid value is a 422, and every stored audit row
    carries one of these exact strings.
    """

    report_investigation = "report_investigation"
    dispute_evidence = "dispute_evidence"
    fraud_investigation = "fraud_investigation"
    safety_review = "safety_review"
    appeal_review = "appeal_review"


class ReportIn(BaseModel):
    targetType: TargetType
    targetId: str = Field(min_length=1, max_length=256)
    category: ReportCategory
    details: str | None = Field(default=None, max_length=2000)


class DisputeIn(BaseModel):
    exchangeId: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=256)
    details: str = Field(min_length=1, max_length=2000)


class ResolveIn(BaseModel):
    outcome: DisputeOutcome
    reversalCredits: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Repo
# ---------------------------------------------------------------------------


class ModerationRepo(Protocol):
    # reports
    def add_report(self, reporter_uid: str, target_type: str, target_id: str,
                   category: str, details: str) -> dict[str, Any]: ...
    # strikes
    def add_strike(self, user_id: str, pillar: str, reason: str,
                   verified: bool) -> dict[str, Any]: ...
    def count_verified_strikes(self, user_id: str, pillar: str) -> int: ...
    # enforcement
    def add_enforcement(self, user_id: str, pillar: str, kind: str,
                        expires_at: datetime | None, reason: str) -> dict[str, Any]: ...
    def active_ban(self, user_id: str) -> dict[str, Any] | None: ...
    def active_enforcement(self, user_id: str, pillar: str,
                           now: datetime) -> dict[str, Any] | None: ...
    # disputes
    def add_dispute(self, exchange_id: str, reporter_uid: str,
                    reason: str, details: str) -> dict[str, Any]: ...
    def get_dispute(self, dispute_id: str) -> dict[str, Any] | None: ...
    def resolve_dispute(self, dispute_id: str, outcome: str, reversal_credits: int,
                        resolved_by: str, resolved_at: datetime) -> dict[str, Any] | None: ...
    # H2: support message reads are scoped to threads with a live case.
    def thread_has_open_case(self, listing_id: str,
                             participant_uids: list[str]) -> bool: ...


class PostgresModerationRepo:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        for key in ("created_at", "resolved_at", "expires_at"):
            value = d.get(key)
            d[key] = value.isoformat() if hasattr(value, "isoformat") else value
        return d

    # -- reports ---------------------------------------------------------
    def add_report(self, reporter_uid, target_type, target_id, category, details):
        row = self._conn.execute(
            "INSERT INTO reports (id, reporter_uid, target_type, target_id, category, details) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            (str(uuid.uuid4()), reporter_uid, target_type, target_id, category, details),
        ).fetchone()
        self._conn.commit()
        return self._row(row)

    # -- strikes ---------------------------------------------------------
    def add_strike(self, user_id, pillar, reason, verified):
        row = self._conn.execute(
            "INSERT INTO strikes (id, user_uid, pillar, reason, verified) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (str(uuid.uuid4()), user_id, pillar, reason, verified),
        ).fetchone()
        self._conn.commit()
        return self._row(row)

    def count_verified_strikes(self, user_id, pillar):
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM strikes "
            "WHERE user_uid = %s AND pillar = %s AND verified",
            (user_id, pillar),
        ).fetchone()
        return int(row["n"])

    # -- enforcement -----------------------------------------------------
    def add_enforcement(self, user_id, pillar, kind, expires_at, reason):
        row = self._conn.execute(
            "INSERT INTO enforcement (id, user_uid, pillar, kind, expires_at, reason) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            (str(uuid.uuid4()), user_id, pillar, kind, expires_at, reason),
        ).fetchone()
        self._conn.commit()
        return self._row(row)

    def active_ban(self, user_id):
        row = self._conn.execute(
            "SELECT * FROM enforcement WHERE user_uid = %s AND kind = 'ban' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return self._row(row) if row else None

    def active_enforcement(self, user_id, pillar, now):
        row = self._conn.execute(
            "SELECT * FROM enforcement WHERE user_uid = %s AND pillar = %s "
            "AND kind = 'suspension' AND expires_at > %s "
            "ORDER BY expires_at DESC LIMIT 1",
            (user_id, pillar, now),
        ).fetchone()
        return self._row(row) if row else None

    # -- disputes --------------------------------------------------------
    def add_dispute(self, exchange_id, reporter_uid, reason, details):
        row = self._conn.execute(
            "INSERT INTO disputes (id, exchange_id, reporter_uid, reason, details) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (str(uuid.uuid4()), exchange_id, reporter_uid, reason, details),
        ).fetchone()
        self._conn.commit()
        return self._row(row)

    def get_dispute(self, dispute_id):
        row = self._conn.execute(
            "SELECT * FROM disputes WHERE id = %s", (dispute_id,)
        ).fetchone()
        return self._row(row) if row else None

    def resolve_dispute(self, dispute_id, outcome, reversal_credits, resolved_by, resolved_at):
        # M11: conditional flip — exactly one concurrent resolve wins; the
        # loser gets zero rows (route maps it to 409), never a silent outcome
        # flip.
        row = self._conn.execute(
            "UPDATE disputes SET status = 'resolved', outcome = %s, "
            "reversal_credits = %s, resolved_by = %s, resolved_at = %s "
            "WHERE id = %s AND status = 'open' RETURNING *",
            (outcome, reversal_credits, resolved_by, resolved_at, dispute_id),
        ).fetchone()
        self._conn.commit()
        return self._row(row) if row else None

    # -- H2: support read scoping ---------------------------------------
    def thread_has_open_case(self, listing_id, participant_uids):
        """A thread is support-readable only when a report names its listing
        or one of its participants, or an open dispute references the
        listing (a dispute's ``exchange_id`` is the listing id). Reports have
        no resolved state in the schema, so any report links; disputes must
        still be ``'open'``."""
        row = self._conn.execute(
            "SELECT (EXISTS ("
            "  SELECT 1 FROM reports"
            "   WHERE (target_type = 'LISTING' AND target_id = %s)"
            "      OR (target_type = 'USER' AND target_id = ANY(%s))"
            ") OR EXISTS ("
            "  SELECT 1 FROM disputes"
            "   WHERE status = 'open' AND exchange_id = %s"
            ")) AS linked",
            (listing_id, list(participant_uids), listing_id),
        ).fetchone()
        return bool(row["linked"])


class MemoryModerationRepo:
    def __init__(self):
        self._reports: list[dict[str, Any]] = []
        self._strikes: list[dict[str, Any]] = []
        self._enforcement: list[dict[str, Any]] = []
        self._disputes: dict[str, dict[str, Any]] = {}

    # -- reports ---------------------------------------------------------
    def add_report(self, reporter_uid, target_type, target_id, category, details):
        row = {"id": str(uuid.uuid4()), "reporter_uid": reporter_uid,
               "target_type": target_type, "target_id": target_id,
               "category": category, "details": details,
               "created_at": _now().isoformat()}
        self._reports.append(row)
        return dict(row)

    # -- strikes ---------------------------------------------------------
    def add_strike(self, user_id, pillar, reason, verified):
        row = {"id": str(uuid.uuid4()), "user_uid": user_id, "pillar": pillar,
               "reason": reason, "verified": verified,
               "created_at": _now().isoformat()}
        self._strikes.append(row)
        return dict(row)

    def count_verified_strikes(self, user_id, pillar):
        return sum(1 for s in self._strikes
                   if s["user_uid"] == user_id and s["pillar"] == pillar and s["verified"])

    # -- enforcement -----------------------------------------------------
    def add_enforcement(self, user_id, pillar, kind, expires_at, reason):
        row = {"id": str(uuid.uuid4()), "user_uid": user_id, "pillar": pillar,
               "kind": kind, "expires_at": expires_at.isoformat() if expires_at else None,
               "reason": reason, "created_at": _now().isoformat()}
        self._enforcement.append(row)
        return dict(row)

    def active_ban(self, user_id):
        matches = [e for e in self._enforcement
                   if e["user_uid"] == user_id and e["kind"] == "ban"]
        return dict(matches[-1]) if matches else None

    def active_enforcement(self, user_id, pillar, now):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        matches = []
        for e in self._enforcement:
            if e["user_uid"] != user_id or e["pillar"] != pillar or e["kind"] != "suspension":
                continue
            until = datetime.fromisoformat(e["expires_at"]) if e["expires_at"] else None
            if until is not None and until > now:
                matches.append(e)
        return dict(matches[-1]) if matches else None

    # -- disputes --------------------------------------------------------
    def add_dispute(self, exchange_id, reporter_uid, reason, details):
        row = {"id": str(uuid.uuid4()), "exchange_id": exchange_id,
               "reporter_uid": reporter_uid, "reason": reason,
               "details": details, "status": "open", "outcome": None,
               "reversal_credits": 0, "resolved_by": None,
               "created_at": _now().isoformat(), "resolved_at": None}
        self._disputes[row["id"]] = row
        return dict(row)

    def get_dispute(self, dispute_id):
        row = self._disputes.get(dispute_id)
        return dict(row) if row else None

    def resolve_dispute(self, dispute_id, outcome, reversal_credits, resolved_by, resolved_at):
        # M11: only an 'open' dispute flips; a concurrent double-resolve
        # loses (returns None -> route 409s), never flips the outcome.
        row = self._disputes.get(dispute_id)
        if row is None or row["status"] != "open":
            return None
        row.update({"status": "resolved", "outcome": outcome,
                    "reversal_credits": reversal_credits,
                    "resolved_by": resolved_by,
                    "resolved_at": resolved_at.isoformat()})
        return dict(row)

    # -- H2: support read scoping ---------------------------------------
    def thread_has_open_case(self, listing_id, participant_uids):
        uids = set(participant_uids)
        for r in self._reports:
            if r["target_type"] == "LISTING" and r["target_id"] == listing_id:
                return True
            if r["target_type"] == "USER" and r["target_id"] in uids:
                return True
        return any(d["status"] == "open" and d["exchange_id"] == listing_id
                   for d in self._disputes.values())


def get_moderation_repo(conn=Depends(get_db_conn)) -> ModerationRepo:
    return PostgresModerationRepo(conn)


# ---------------------------------------------------------------------------
# Strike / suspension domain logic
# ---------------------------------------------------------------------------


def record_strike(repo: ModerationRepo, user_id: str, pillar: str,
                  reason: str, verified: bool) -> dict[str, Any]:
    """Record a complaint outcome and enforce the strike rules.

    - Unverified complaints are stored for context and never enforce.
    - A verified ``reason == "fraud"`` complaint bans the user permanently
      (first offense, all pillars).
    - 2 verified complaints against the same user in the same pillar suspend
      the user from that pillar for 90 days.
    """
    strike = repo.add_strike(user_id=user_id, pillar=pillar, reason=reason,
                             verified=verified)
    if not verified:
        return strike
    if reason.strip().lower() == FRAUD_CATEGORY:
        repo.add_enforcement(user_id=user_id, pillar="*", kind="ban",
                             expires_at=None, reason="verified fraud complaint")
        return strike
    if repo.count_verified_strikes(user_id, pillar) >= 2:
        repo.add_enforcement(
            user_id=user_id, pillar=pillar, kind="suspension",
            expires_at=_now() + timedelta(days=SUSPENSION_DAYS),
            reason="2 verified complaints in this pillar",
        )
    return strike


def get_suspension(repo: ModerationRepo, uid: str, pillar: str) -> dict[str, Any] | None:
    """Active trust enforcement for a user in a pillar.

    Returns ``None`` when nothing is active, otherwise
    ``{"type": "suspension"|"ban", "untilMs": int | None, "reason": str}``.
    A ban applies to every pillar; suspensions are per-pillar and expire.
    """
    ban = repo.active_ban(uid)
    if ban is not None:
        return {"type": "ban", "untilMs": None, "reason": ban["reason"]}
    suspension = repo.active_enforcement(uid, pillar, _now())
    if suspension is None:
        return None
    until = suspension["expires_at"]
    if isinstance(until, str):
        until = datetime.fromisoformat(until)
    return {"type": "suspension", "untilMs": _epoch_ms(until),
            "reason": suspension["reason"]}


# ---------------------------------------------------------------------------
# Support gating
# ---------------------------------------------------------------------------


def _support_uids() -> set[str]:
    """Firebase uids allowed to resolve disputes. SUPPORT_UIDS is read fresh on
    every call so tests can monkeypatch it; empty means nobody (fail closed)."""
    raw = os.environ.get("SUPPORT_UIDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _require_support(uid: str) -> None:
    if uid not in _support_uids():
        raise HTTPException(
            403,
            {"code": "forbidden",
             "message": "Dispute resolution is restricted to support staff"},
        )


def require_support(uid: str) -> None:
    """Public support-staff gate for other tracks (e.g. the support message
    dashboard in msg.py). Same fail-closed semantics as dispute resolution:
    empty SUPPORT_UIDS means nobody passes."""
    _require_support(uid)


def _chain_hash(prev_hash: str | None, viewer_uid: str, thread_id: str,
                message_id: str | None, reason: str) -> str:
    """H3: per-row hash chaining for the audit log. Each row's hash covers the
    previous row's hash, so a tampered row invalidates every later one.
    The UPDATE/DELETE trigger (migration 0026) enforces append-only at the
    DB level; this chain detects row replacement / history rewriting.

    Limitation: under concurrent inserts two rows can briefly share the same
    ``prev_hash`` (both read the tail before either commits). The chain still
    detects after-the-fact edits; it is tamper-evidence, not a total order.
    """
    payload = "\x1f".join(
        [prev_hash or "", viewer_uid, str(thread_id), str(message_id or ""), reason])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ModerationViewRepo(Protocol):
    """Audit log of privileged plaintext views (support dashboard decrypts).

    Every time support staff views decrypted messages, one row is written
    per message viewed, recording who looked at what and why. Rows are
    chained (``prev_hash``/``row_hash``) and append-only: migration 0026
    blocks UPDATE/DELETE at the DB level. Read paths exist so audits can be
    reviewed; there is no update/delete.
    """

    def log_view(self, viewer_uid: str, thread_id: str, message_id: str | None,
                 reason: str) -> dict[str, Any]: ...
    def log_views_batch(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Write one audit row per entry in a single transaction (M2: the
        support dashboard no longer commits per message). Each entry holds
        ``viewer_uid``, ``thread_id``, ``message_id``, ``reason``."""
        ...
    def list_views_for_thread(self, thread_id: str) -> list[dict[str, Any]]: ...


class PostgresModerationViewRepo:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        v = d.get("viewed_at")
        d["viewed_at"] = v.isoformat() if hasattr(v, "isoformat") else v
        return d

    def _last_hash(self) -> str | None:
        row = self._conn.execute(
            "SELECT row_hash FROM moderation_views "
            "ORDER BY viewed_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return row["row_hash"] if row else None

    def log_view(self, viewer_uid, thread_id, message_id, reason):
        return self.log_views_batch([{
            "viewer_uid": viewer_uid, "thread_id": thread_id,
            "message_id": message_id, "reason": reason,
        }])[0]

    def log_views_batch(self, entries):
        # M2: one INSERT per row but a single commit for the whole batch.
        # Chain each row to the previous tail row (H3).
        rows = []
        for e in entries:
            prev_hash = self._last_hash()
            row_hash = _chain_hash(prev_hash, e["viewer_uid"], e["thread_id"],
                                   e.get("message_id"), e["reason"])
            row = self._conn.execute(
                "INSERT INTO moderation_views "
                "(id, viewer_uid, thread_id, message_id, reason, prev_hash, row_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (str(uuid.uuid4()), e["viewer_uid"], e["thread_id"],
                 e.get("message_id"), e["reason"], prev_hash, row_hash),
            ).fetchone()
            rows.append(self._row(row))
        self._conn.commit()
        return rows

    def list_views_for_thread(self, thread_id):
        rows = self._conn.execute(
            "SELECT * FROM moderation_views WHERE thread_id = %s ORDER BY viewed_at",
            (thread_id,)).fetchall()
        return [self._row(r) for r in rows]


class MemoryModerationViewRepo:
    def __init__(self):
        self._views: list[dict[str, Any]] = []

    def log_view(self, viewer_uid, thread_id, message_id, reason):
        return self.log_views_batch([{
            "viewer_uid": viewer_uid, "thread_id": thread_id,
            "message_id": message_id, "reason": reason,
        }])[0]

    def log_views_batch(self, entries):
        rows = []
        for e in entries:
            prev_hash = self._views[-1]["row_hash"] if self._views else None
            row = {"id": str(uuid.uuid4()), "viewer_uid": e["viewer_uid"],
                   "thread_id": e["thread_id"], "message_id": e.get("message_id"),
                   "reason": e["reason"], "prev_hash": prev_hash,
                   "row_hash": _chain_hash(prev_hash, e["viewer_uid"],
                                           e["thread_id"], e.get("message_id"),
                                           e["reason"]),
                   "viewed_at": _now().isoformat()}
            self._views.append(row)
            rows.append(dict(row))
        return rows

    def list_views_for_thread(self, thread_id):
        return [dict(v) for v in self._views if v["thread_id"] == thread_id]


def get_moderation_view_repo(conn=Depends(get_db_conn)) -> ModerationViewRepo:
    return PostgresModerationViewRepo(conn)


def _serialize_dispute(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "exchangeId": row["exchange_id"],
        "reporterUid": row["reporter_uid"],
        "reason": row["reason"],
        "details": row["details"],
        "status": row["status"],
        "outcome": row["outcome"],
        "reversalCredits": row["reversal_credits"],
        "resolvedBy": row["resolved_by"],
        "createdAt": row.get("created_at"),
        "resolvedAt": row.get("resolved_at"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/reports", status_code=201)
def create_report(
    data: ReportIn,
    uid: str = Depends(get_current_uid),
    repo: ModerationRepo = Depends(get_moderation_repo),
) -> dict[str, Any]:
    """File a report against a listing, user, or booking. Returns the id."""
    details = (data.details or "").strip() or None
    report = repo.add_report(reporter_uid=uid, target_type=data.targetType.value,
                             target_id=data.targetId, category=data.category.value,
                             details=details)
    return {"id": str(report["id"])}


def _serialize_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "viewer_uid": row["viewer_uid"],
        "thread_id": str(row["thread_id"]),
        "message_id": str(row["message_id"]) if row.get("message_id") else None,
        "reason": row["reason"],
        "viewed_at": row.get("viewed_at"),
        "prev_hash": row.get("prev_hash"),
        "row_hash": row.get("row_hash"),
    }


@router.get("/support/moderation-views", tags=["support"])
def list_moderation_views(
    thread_id: str = Query(min_length=1, description="Thread to review audit rows for"),
    uid: str = Depends(get_current_uid),
    view_repo: ModerationViewRepo = Depends(get_moderation_view_repo),
) -> dict[str, Any]:
    """H3: review surface for the privileged-view audit log. Support staff
    only (``SUPPORT_UIDS`` — 403 otherwise, fail closed). Rows are
    append-only (migration 0026) and hash-chained."""
    require_support(uid)
    views = view_repo.list_views_for_thread(thread_id)
    return {"thread_id": thread_id, "views": [_serialize_view(v) for v in views]}


@router.post("/disputes")
def create_dispute(
    data: DisputeIn,
    uid: str = Depends(get_current_uid),
    repo: ModerationRepo = Depends(get_moderation_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
) -> dict[str, Any]:
    """Open a dispute on a completed exchange. Only the giver or claimer can."""
    listing = listing_repo.get(data.exchangeId)
    if listing is None:
        raise HTTPException(404, {"code": "exchange_not_found",
                                  "message": "No such exchange"})
    if listing["status"] != "completed":
        raise HTTPException(422, {"code": "exchange_not_completed",
                                  "message": "Disputes can only be opened on completed exchanges"})
    if uid not in (listing.get("owner_uid"), listing.get("claimer_uid")):
        raise HTTPException(403, {"code": "not_a_party",
                                  "message": "Only the giver and claimer can open a dispute"})
    dispute = repo.add_dispute(exchange_id=data.exchangeId, reporter_uid=uid,
                               reason=data.reason, details=data.details)
    return {"dispute": _serialize_dispute(dispute)}


def _exchange_has_forward_credit_legs(credit_repo: CreditRepo,
                                      listing: dict[str, Any]) -> bool:
    """True when the exchange's forward credit legs actually posted.

    The exchange money leg (exchange.py confirm) records its spend/earn
    entries with ``ref_id`` = the listing/exchange id, so a completed
    exchange that ran while credits were off (or at cost 0) has none —
    the exact distinction the dispute-reversal gate needs: unwinding a
    real leg is always legal, inventing one is not."""
    exchange_id = str(listing["id"])
    parties = {listing.get("claimer_uid"), listing.get("owner_uid")} - {None}
    for party in parties:
        for entry in credit_repo.entries(party):
            if (entry.get("ref_id") == exchange_id
                    and entry.get("reason") in ("exchange_spend", "exchange_earn")):
                return True
    return False


@router.post("/disputes/{dispute_id}/resolve")
def resolve_dispute(
    dispute_id: str,
    data: ResolveIn,
    uid: str = Depends(get_current_uid),
    repo: ModerationRepo = Depends(get_moderation_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
) -> dict[str, Any]:
    """Resolve a dispute. Support staff only.

    M11: reversals are posted *before* the status flip, so an earn-cap
    failure leaves the dispute open (409) instead of resolved-but-unreversed
    — the dispute is retryable once the 7-day window frees up. The flip
    itself is conditional (``status='open'``): exactly one concurrent
    resolve wins, the loser gets 409. Reversal entries are idempotency-keyed
    (``dispute:{id}:reversal:{claimer,owner}``), so a retried resolve never
    double-posts."""
    _require_support(uid)
    dispute = repo.get_dispute(dispute_id)
    if dispute is None:
        raise HTTPException(404, {"code": "dispute_not_found",
                                  "message": "No such dispute"})
    if dispute["status"] == "resolved":
        raise HTTPException(409, {"code": "dispute_already_resolved",
                                  "message": "This dispute is already resolved"})
    if data.outcome == DisputeOutcome.upheld and data.reversalCredits > 0:
        listing = listing_repo.get(dispute["exchange_id"])
        if listing is not None:
            if (not get_vertical().economy.credits_enabled
                    and not _exchange_has_forward_credit_legs(credit_repo, listing)):
                # Credits are off NOW and this exchange never moved any
                # (completed while credits were off): there is no credit
                # flow to unwind, so a positive reversal is meaningless
                # rather than a mint. (If forward legs DO exist — posted
                # before a flip-off — the unwind below still posts, same
                # as the claims cancel path.) reversalCredits=0 never
                # reaches this branch and always stays allowed.
                raise HTTPException(
                    422, {"code": "credits_disabled",
                          "message": "Credits are disabled for this "
                                     "community and this exchange moved "
                                     "no credits — there is nothing to "
                                     "reverse. Resolve with "
                                     "reversalCredits=0."})
            # Reverse credit flow: claimer gets credits back, giver is
            # debited. Posted BEFORE the status flip (M11). The legs are
            # ``dispute_reversal`` — a refund, cap-exempt since the earn
            # cap started governing issuance only (code_review_fixes) — so
            # the EarnCapExceededError branch below is defense-in-depth and
            # no longer reachable through these legs.
            claimer = listing.get("claimer_uid")
            owner = listing.get("owner_uid")
            try:
                if claimer:
                    credit_repo.add_entry(
                        claimer, data.reversalCredits, "dispute_reversal",
                        ref_id=dispute_id,
                        idempotency_key=f"dispute:{dispute_id}:reversal:claimer")
                if owner:
                    credit_repo.add_entry(
                        owner, -data.reversalCredits, "dispute_reversal",
                        ref_id=dispute_id,
                        idempotency_key=f"dispute:{dispute_id}:reversal:owner")
            except EarnCapExceededError as exc:
                raise HTTPException(
                    409,
                    {"code": "dispute_reversal_cap_blocked",
                     "message": "Dispute left open: the reversal would exceed "
                                "the claimer's 7-day earn cap. Resolve again "
                                "after the window, or resolve with "
                                "reversalCredits=0."},
                ) from exc
    resolved = repo.resolve_dispute(dispute_id=dispute_id, outcome=data.outcome.value,
                                    reversal_credits=data.reversalCredits,
                                    resolved_by=uid, resolved_at=_now())
    if resolved is None:
        # Lost a concurrent resolve race (conditional UPDATE flipped zero
        # rows because another worker resolved first).
        raise HTTPException(409, {"code": "dispute_already_resolved",
                                  "message": "This dispute is already resolved"})
    return {"dispute": _serialize_dispute(resolved)}
