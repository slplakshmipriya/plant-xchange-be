"""Pick-your-own slot scheduling on tree listings (R2).

- ``POST /v1/trees/{id}/slots``: the tree owner opens a picking slot window.
- ``GET /v1/trees/{id}/slots``: list a tree's slots (exact wire shape).
- ``POST /v1/trees/{id}/slots/{slot_id}/claim``: a picker claims a spot —
  ``credit_cost`` credits move claimer -> owner through the append-only
  credit ledger and ``claimed_count`` increments. Full slots and empty
  wallets are rejected (409/422); the owner cannot claim their own slot;
  a repeat claim by the same picker is rejected (409).
- ``POST /v1/trees/{id}/slots/{slot_id}/confirm-visit``: after the visit,
  the claimer records how much they picked (``lbs_picked``). Lands on
  their ``slot_claims`` row (migration 0045); no credits move.

Suspension enforcement is module-local on purpose: ``check_pillar_suspension``
is a stub that the coordinator will rewire to the shared moderation module
after merge. Do NOT create or import from app.moderation (another track owns
it).
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .auth import ensure_owner, get_current_uid
from .claims import serialize_spend
from .credits import CreditRepo, get_credit_repo
from .db import get_db_conn
from .listings import ListingRepo, get_listing_repo
from .moderation import ModerationRepo, get_moderation_repo, get_suspension
from .txn import atomic
from .users import UserRepo, get_user_repo
from .vertical import get_vertical

router = APIRouter(prefix="/v1", tags=["trees"])

# ---------------------------------------------------------------- suspensions

# Pillar name enforced on slot claims, matching the moderation track's
# strike/suspension records (see app/moderation.py).
PICKUP_PILLAR = "pickup"


class AlreadyClaimed(Exception):
    """Raised when the same user claims the same slot twice.

    Carries (slot_id, claimer_uid). The route maps this to 409
    ``already_claimed`` — a repeat POST must not increment ``claimed_count``
    again while the credit idempotency key suppresses the second charge.
    """


# ---------------------------------------------------------------- serializer

def public_slot(row: dict[str, Any]) -> dict[str, Any]:
    """Wire shape: exactly
    {id, treeId, dayMs, startMs, endMs, maxPickers, claimedCount, creditCost,
    cashCents}.
    """
    return {
        "id": str(row["id"]),
        "treeId": str(row["tree_id"]),
        "dayMs": row["day_ms"],
        "startMs": row["start_ms"],
        "endMs": row["end_ms"],
        "maxPickers": row["max_pickers"],
        "claimedCount": row["claimed_count"],
        "creditCost": row["credit_cost"],
        "cashCents": row.get("cash_cents"),
    }


def with_caller_claim(slot: dict[str, Any],
                      claim: dict[str, Any] | None) -> dict[str, Any]:
    """Annotate a public slot with the CALLER's own claim, if they hold one.

    Adds exactly ``claimed_by_me`` (true) and ``visit_confirmed`` (bool),
    plus ``lbs_picked`` once the claimer has recorded a pick. A caller
    with no claim on the slot gets the untouched public shape — other
    users' claims (and who claimed) are never exposed.
    """
    if claim is None:
        return slot
    out = dict(slot)
    out["claimed_by_me"] = True
    out["visit_confirmed"] = claim.get("visited_at") is not None
    if claim.get("lbs_picked") is not None:
        out["lbs_picked"] = claim["lbs_picked"]
    return out


# ---------------------------------------------------------------- repository

class SlotRepo(Protocol):
    def create(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def get(self, slot_id: str) -> dict[str, Any] | None: ...
    def list_by_tree(self, tree_id: str) -> list[dict[str, Any]]: ...
    def has_claim(self, slot_id: str, claimer_uid: str) -> bool:
        """True when this claimer already holds a spot on this slot."""
        ...
    def claim_slot(self, slot_id: str, claimer_uid: str) -> dict[str, Any] | None:
        """Claim one spot in the slot for ``claimer_uid``, atomically.

        Inserts the (slot, claimer) claim record and increments
        ``claimed_count`` iff ``claimed_count < max_pickers``, in one
        transaction. Returns the updated row, or None when the slot is
        missing or full — concurrent claimants cannot both win the last
        spot. Raises ``AlreadyClaimed`` when this claimer already holds a
        spot on this slot.
        """
        ...
    def get_claim(self, slot_id: str, claimer_uid: str) -> dict[str, Any] | None:
        """The claimer's claim record on this slot, or None."""
        ...
    def confirm_visit(self, slot_id: str, claimer_uid: str,
                      lbs_picked: float, visited_at_ms: int) -> dict[str, Any] | None:
        """Record the visit outcome on the claimer's claim (migration 0045).

        Sets ``lbs_picked``/``visited_at``; idempotent — a repeat confirm
        overwrites both. Returns the updated claim, or None when the
        claimer holds no claim on this slot.
        """
        ...


class PostgresSlotRepo:
    def __init__(self, conn):
        self._conn = conn

    _SELECT = (
        "SELECT id, tree_id, owner_uid, day_ms, start_ms, end_ms, "
        "max_pickers, claimed_count, credit_cost, cash_cents, created_at FROM slots"
    )

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        # psycopg hands back uuid.UUID for the UUID columns (slots.id,
        # slots.tree_id, slot_claims.slot_id; owner_uid/claimer_uid are
        # TEXT). The memory repo stores plain strings, and route handlers
        # compare row fields against string path params
        # (``slot["tree_id"] != tree_id``) — so normalize UUIDs to str
        # here, the one place Postgres slot/claim rows are materialized.
        # Without this, every PG-backed slot route 404s on the tree
        # mismatch even though the row exists.
        for k, v in d.items():
            if isinstance(v, uuid.UUID):
                d[k] = str(v)
        if "created_at" in d:
            c = d["created_at"]
            d["created_at"] = c.isoformat() if hasattr(c, "isoformat") else c
        return d

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        row = self._conn.execute(
            "INSERT INTO slots (id, tree_id, owner_uid, day_ms, start_ms, end_ms, "
            "max_pickers, claimed_count, credit_cost, cash_cents) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                data["id"], data["tree_id"], data["owner_uid"], data["day_ms"],
                data["start_ms"], data["end_ms"], data["max_pickers"],
                data.get("claimed_count", 0), data["credit_cost"],
                data.get("cash_cents"),
            ),
        ).fetchone()
        self._conn.commit()
        return self.get(str(row["id"]))

    def get(self, slot_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(self._SELECT + " WHERE id = %s", (slot_id,)).fetchone()
        return self._row(row) if row else None

    def list_by_tree(self, tree_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            self._SELECT + " WHERE tree_id = %s ORDER BY day_ms, start_ms",
            (tree_id,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def has_claim(self, slot_id: str, claimer_uid: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM slot_claims WHERE slot_id = %s AND claimer_uid = %s",
            (slot_id, claimer_uid),
        ).fetchone()
        return row is not None

    def claim_slot(self, slot_id: str, claimer_uid: str) -> dict[str, Any] | None:
        # One transaction: the claim-row INSERT and the claimed_count
        # increment commit together or not at all. INSERT ... ON
        # CONFLICT DO NOTHING serializes repeat claims of the same
        # (slot, claimer) on the unique index — a duplicate INSERT gets
        # rowcount 0 and rolls back to an AlreadyClaimed 409. The
        # conditional UPDATE keeps concurrent claimants from winning
        # the last spot twice — a lost race rolls back to a slot_full 409.
        try:
            ins = self._conn.execute(
                "INSERT INTO slot_claims (slot_id, claimer_uid) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (slot_id, claimer_uid),
            )
            if (ins.rowcount or 0) == 0:
                self._conn.rollback()
                raise AlreadyClaimed(slot_id, claimer_uid)
            upd = self._conn.execute(
                "UPDATE slots SET claimed_count = claimed_count + 1 "
                "WHERE id = %s AND claimed_count < max_pickers",
                (slot_id,),
            )
            if (upd.rowcount or 0) == 0:
                self._conn.rollback()
                return None
            self._conn.commit()
        except AlreadyClaimed:
            raise
        except Exception:
            self._conn.rollback()
            raise
        return self.get(slot_id)

    _CLAIM_SELECT = (
        "SELECT slot_id, claimer_uid, lbs_picked, visited_at FROM slot_claims"
    )

    def get_claim(self, slot_id: str, claimer_uid: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            self._CLAIM_SELECT + " WHERE slot_id = %s AND claimer_uid = %s",
            (slot_id, claimer_uid),
        ).fetchone()
        # Through _row so slot_id normalizes to str, same as memory rows.
        return self._row(row) if row else None

    def confirm_visit(self, slot_id: str, claimer_uid: str,
                      lbs_picked: float, visited_at_ms: int) -> dict[str, Any] | None:
        # Single UPDATE keyed on the claim's UNIQUE (slot, claimer) pair —
        # atomic on its own and idempotent: re-confirming overwrites the
        # recorded pick. No credits move here (they settled at claim
        # time), so there is nothing to keep atomic with the ledger.
        row = self._conn.execute(
            "UPDATE slot_claims SET lbs_picked = %s, visited_at = %s "
            "WHERE slot_id = %s AND claimer_uid = %s RETURNING slot_id",
            (lbs_picked, visited_at_ms, slot_id, claimer_uid),
        ).fetchone()
        if row is None:
            self._conn.rollback()
            return None
        self._conn.commit()
        return self.get_claim(slot_id, claimer_uid)


class MemorySlotRepo:
    def __init__(self):
        self._rows: dict[str, dict[str, Any]] = {}
        self._claims: dict[tuple[str, str], dict[str, Any]] = {}

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        row = dict(data)
        row.setdefault("claimed_count", 0)
        self._rows[row["id"]] = row
        return dict(row)

    def get(self, slot_id: str) -> dict[str, Any] | None:
        row = self._rows.get(slot_id)
        return dict(row) if row else None

    def list_by_tree(self, tree_id: str) -> list[dict[str, Any]]:
        rows = [r for r in self._rows.values() if r["tree_id"] == tree_id]
        rows.sort(key=lambda r: (r["day_ms"], r["start_ms"]))
        return [dict(r) for r in rows]

    def claim_slot(self, slot_id: str, claimer_uid: str) -> dict[str, Any] | None:
        key = (slot_id, claimer_uid)
        if key in self._claims:
            raise AlreadyClaimed(slot_id, claimer_uid)
        row = self._rows.get(slot_id)
        if row is None or row["claimed_count"] >= row["max_pickers"]:
            return None
        self._claims[key] = {
            "slot_id": slot_id,
            "claimer_uid": claimer_uid,
            "lbs_picked": None,
            "visited_at": None,
        }
        row["claimed_count"] += 1
        return dict(row)

    def has_claim(self, slot_id: str, claimer_uid: str) -> bool:
        return (slot_id, claimer_uid) in self._claims

    def get_claim(self, slot_id: str, claimer_uid: str) -> dict[str, Any] | None:
        claim = self._claims.get((slot_id, claimer_uid))
        return dict(claim) if claim is not None else None

    def confirm_visit(self, slot_id: str, claimer_uid: str,
                      lbs_picked: float, visited_at_ms: int) -> dict[str, Any] | None:
        claim = self._claims.get((slot_id, claimer_uid))
        if claim is None:
            return None
        claim["lbs_picked"] = lbs_picked
        claim["visited_at"] = visited_at_ms
        return dict(claim)


def get_slot_repo(conn=Depends(get_db_conn)) -> SlotRepo:
    return PostgresSlotRepo(conn)


# ---------------------------------------------------------------- API models

class SlotIn(BaseModel):
    dayMs: int = Field(ge=0)
    startMs: int = Field(ge=0)
    endMs: int = Field(ge=0)
    maxPickers: int = Field(ge=1)
    creditCost: int = Field(ge=0, le=100)
    cashCents: int | None = Field(default=None, ge=0)

    @field_validator("creditCost", mode="after")
    @classmethod
    def _credit_cost_within_vertical(cls, v):
        # The ceiling is API-enforced only: the slots table's CHECK
        # (migration 0015) bounds credit_cost >= 0 with NO upper bound,
        # so this validator (and the static Field(le=100)) is the only
        # thing keeping slot prices at or under the vertical's ceiling.
        # The vertical may lower that ceiling but never raise it. The
        # ceiling is model-enforced only while credits are ON: while OFF,
        # a priced cost passes the model (up to the static Field bound)
        # so the route can reject it with the enveloped 422
        # credits_disabled instead of a bare validation error.
        economy = get_vertical().economy
        max_cost = economy.max_listing_cost
        if economy.credits_enabled and v > max_cost:
            raise ValueError(
                f"creditCost must be <= {max_cost} for this marketplace")
        return v


# ---------------------------------------------------------------- routes

def _tree_or_404(tree_id: str, listing_repo: ListingRepo) -> dict[str, Any]:
    row = listing_repo.get(tree_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    if row["type"] != "tree":
        raise HTTPException(422, {"code": "not_tree_listing",
                                  "message": "Slots apply to tree listings only"})
    return row


@router.post("/trees/{tree_id}/slots", status_code=201, tags=["trees"])
def create_slot(
    tree_id: str,
    data: SlotIn,
    uid: str = Depends(get_current_uid),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    slot_repo: SlotRepo = Depends(get_slot_repo),
) -> dict[str, Any]:
    """Open a pick-your-own slot window on a tree. Owner only."""
    tree = _tree_or_404(tree_id, listing_repo)
    ensure_owner(tree["owner_uid"], uid)
    if not get_vertical().economy.credits_enabled and data.creditCost > 0:
        raise HTTPException(422, {"code": "credits_disabled",
                                  "message": "Credits are disabled for this "
                                             "community — slots must be free"})
    if data.startMs >= data.endMs:
        raise HTTPException(422, {"code": "invalid_window",
                                  "message": "startMs must be before endMs"})
    row = slot_repo.create({
        "id": str(uuid.uuid4()),
        "tree_id": tree_id,
        "owner_uid": uid,
        "day_ms": data.dayMs,
        "start_ms": data.startMs,
        "end_ms": data.endMs,
        "max_pickers": data.maxPickers,
        "claimed_count": 0,
        "credit_cost": data.creditCost,
        "cash_cents": data.cashCents,
    })
    return {"slot": public_slot(row)}


@router.get("/trees/{tree_id}/slots", tags=["trees"])
def list_slots(
    tree_id: str,
    uid: str = Depends(get_current_uid),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    slot_repo: SlotRepo = Depends(get_slot_repo),
) -> dict[str, Any]:
    """List a tree's slots. Wire shape: {slots: [...]} — each slot the
    caller has claimed also carries their claim annotations
    (``claimed_by_me``/``visit_confirmed``/``lbs_picked``)."""
    _tree_or_404(tree_id, listing_repo)
    return {"slots": [
        with_caller_claim(public_slot(s), slot_repo.get_claim(s["id"], uid))
        for s in slot_repo.list_by_tree(tree_id)
    ]}


@router.post("/trees/{tree_id}/slots/{slot_id}/claim", tags=["trees"])
def claim_slot(
    tree_id: str,
    slot_id: str,
    uid: str = Depends(get_current_uid),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    slot_repo: SlotRepo = Depends(get_slot_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
    mod_repo: ModerationRepo = Depends(get_moderation_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Claim a spot in a slot. credit_cost credits move claimer -> owner via
    the ledger; claimed_count increments. Not the owner, not when suspended,
    not when full, not when the wallet is short. Solo slots (maxPickers 1)
    additionally require a verified ID (PRD: IDV required for solo
    pick-your-own access)."""
    _tree_or_404(tree_id, listing_repo)
    slot = slot_repo.get(slot_id)
    if slot is None or slot["tree_id"] != tree_id:
        raise HTTPException(404, {"code": "slot_not_found", "message": "No such slot"})
    susp = get_suspension(mod_repo, uid, PICKUP_PILLAR)
    if susp is not None:
        raise HTTPException(403, {"code": "suspended",
                                  "message": f"Suspended ({susp['type']}): {susp['reason']}"})
    if slot["owner_uid"] == uid:
        raise HTTPException(422, {"code": "cannot_claim_own",
                                  "message": "You cannot claim your own slot"})
    if slot_repo.has_claim(slot_id, uid):
        raise HTTPException(409, {"code": "already_claimed",
                                  "message": "You already claimed a spot in this slot"})
    if slot["claimed_count"] >= slot["max_pickers"]:
        raise HTTPException(409, {"code": "slot_full",
                                  "message": "This slot is full"})
    if slot["max_pickers"] == 1:
        # M20b: PRD requires IDV for solo pick-your-own access.
        profile = user_repo.get(uid)
        if profile is None or profile.get("idv_status") != "verified":
            raise HTTPException(
                403, {"code": "idv_required",
                      "message": "Solo pick-your-own requires a verified ID "
                                 "(complete ID verification first)"})
    cost = slot["credit_cost"]
    # credits_enabled=False: the spot is still claimed atomically, but no
    # credits move — no balance gate, no ledger legs. (A slot priced
    # while credits were on keeps its stored cost; it just never moves.)
    credits_on = get_vertical().economy.credits_enabled
    # H11: the balance gate and the spend post are serialized per claimer —
    # two concurrent slot claims would otherwise both pass the gate and
    # both post, driving the balance negative. The atomic spot claim runs
    # inside the same critical section so a won spot is always paid for
    # exactly once, and a lost race pays nothing.
    with serialize_spend(uid, credit_repo):
        if credits_on and credit_repo.balance(uid) < cost:
            raise HTTPException(422, {"code": "insufficient_credits",
                                      "message": "Not enough credits — give before you claim"})
        # Atomic claim: the (slot, claimer) record, the claimed_count
        # increment, and both credit legs land in one transaction. A repeat
        # claim by the same user raises AlreadyClaimed (409) instead of
        # double-counting a spot that the idempotent ledger would never
        # charge twice for; a failed earn can no longer leave the spot
        # consumed and the claimer charged (pre-fix behavior).
        with atomic(slot_repo, credit_repo):
            try:
                updated = slot_repo.claim_slot(slot_id, uid)
            except AlreadyClaimed:
                raise HTTPException(409, {"code": "already_claimed",
                                          "message": "You already claimed a spot in this slot"})
            if updated is None:
                raise HTTPException(409, {"code": "slot_full",
                                          "message": "This slot just filled up"})
            # Credits move through the append-only ledger (same pattern as
            # exchange.confirm). Idempotency keys include the claimer so each
            # claim is a distinct spot purchase.
            if credits_on and cost:
                # cost == 0 (free slot claimed after a credits flip-on)
                # posts NO legs — zero-delta rows are forbidden by the
                # ledger CHECK and move nothing.
                base_key = f"slot:{slot_id}:{uid}"
                credit_repo.add_entry(uid, -cost, "slot_spend", ref_id=slot_id,
                                      idempotency_key=f"{base_key}:spend")
                credit_repo.add_entry(slot["owner_uid"], cost, "slot_earn", ref_id=slot_id,
                                      idempotency_key=f"{base_key}:earn")
    return {"slot": public_slot(updated)}


@router.post("/trees/{tree_id}/slots/{slot_id}/confirm-visit", tags=["trees"])
def confirm_visit(
    tree_id: str,
    slot_id: str,
    payload: dict[str, Any] = Body(...),
    uid: str = Depends(get_current_uid),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    slot_repo: SlotRepo = Depends(get_slot_repo),
    mod_repo: ModerationRepo = Depends(get_moderation_repo),
) -> dict[str, Any]:
    """Confirm a pick-your-own visit after the fact: the claimer records
    how much they picked (``{"lbs_picked": <number>}``, 0 < lbs <= 1000).
    Lands on the caller's claim row (migration 0045); NO credits move —
    they settled at claim time. Idempotent: confirming again updates the
    recorded pick. Only a claimer may confirm, and the PICKUP suspension
    gate applies exactly as on claim."""
    _tree_or_404(tree_id, listing_repo)
    slot = slot_repo.get(slot_id)
    if slot is None or slot["tree_id"] != tree_id:
        raise HTTPException(404, {"code": "slot_not_found", "message": "No such slot"})
    susp = get_suspension(mod_repo, uid, PICKUP_PILLAR)
    if susp is not None:
        raise HTTPException(403, {"code": "suspended",
                                  "message": f"Suspended ({susp['type']}): {susp['reason']}"})
    claim = slot_repo.get_claim(slot_id, uid)
    if claim is None:
        raise HTTPException(403, {"code": "not_claimed",
                                  "message": "You have not claimed a spot in this slot"})
    lbs = payload.get("lbs_picked") if isinstance(payload, dict) else None
    # A huge JSON int (e.g. a 400-digit number) parses as a Python bigint
    # whose float() raises OverflowError — that is an invalid pick
    # amount (422), not a server error, so convert defensively. NaN/inf
    # are rejected by the finiteness check.
    try:
        lbs_val = float(lbs) if isinstance(lbs, (int, float)) \
            and not isinstance(lbs, bool) else None
    except (OverflowError, ValueError):
        lbs_val = None
    if lbs_val is None or not math.isfinite(lbs_val) \
            or not 0 < lbs_val <= 1000:
        raise HTTPException(422, {"code": "invalid_lbs",
                                  "message": "lbs_picked must be greater than 0 "
                                             "and at most 1000"})
    visited_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    updated_claim = slot_repo.confirm_visit(slot_id, uid, lbs_val, visited_at_ms)
    if updated_claim is None:  # claim deleted between check and update
        raise HTTPException(403, {"code": "not_claimed",
                                  "message": "You have not claimed a spot in this slot"})
    return {"slot": with_caller_claim(public_slot(slot), updated_claim)}
