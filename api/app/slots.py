"""Pick-your-own slot scheduling on tree listings (R2).

- ``POST /v1/trees/{id}/slots``: the tree owner opens a picking slot window.
- ``GET /v1/trees/{id}/slots``: list a tree's slots (exact wire shape).
- ``POST /v1/trees/{id}/slots/{slot_id}/claim``: a picker claims a spot —
  ``credit_cost`` credits move claimer -> owner through the append-only
  credit ledger and ``claimed_count`` increments. Full slots and empty
  wallets are rejected (409/422); the owner cannot claim their own slot;
  a repeat claim by the same picker is rejected (409).

Suspension enforcement is module-local on purpose: ``check_pillar_suspension``
is a stub that the coordinator will rewire to the shared moderation module
after merge. Do NOT create or import from app.moderation (another track owns
it).
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import ensure_owner, get_current_uid
from .claims import serialize_spend
from .credits import CreditRepo, get_credit_repo
from .db import get_db_conn
from .listings import ListingRepo, get_listing_repo
from .moderation import ModerationRepo, get_moderation_repo, get_suspension
from .users import UserRepo, get_user_repo

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
        c = d.get("created_at")
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


class MemorySlotRepo:
    def __init__(self):
        self._rows: dict[str, dict[str, Any]] = {}
        self._claims: set[tuple[str, str]] = set()

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
        self._claims.add(key)
        row["claimed_count"] += 1
        return dict(row)

    def has_claim(self, slot_id: str, claimer_uid: str) -> bool:
        return (slot_id, claimer_uid) in self._claims


def get_slot_repo(conn=Depends(get_db_conn)) -> SlotRepo:
    return PostgresSlotRepo(conn)


# ---------------------------------------------------------------- API models

class SlotIn(BaseModel):
    dayMs: int = Field(ge=0)
    startMs: int = Field(ge=0)
    endMs: int = Field(ge=0)
    maxPickers: int = Field(ge=1)
    creditCost: int = Field(ge=0)
    cashCents: int | None = Field(default=None, ge=0)


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
    listing_repo: ListingRepo = Depends(get_listing_repo),
    slot_repo: SlotRepo = Depends(get_slot_repo),
) -> dict[str, Any]:
    """List a tree's slots. Wire shape: exactly {slots: [...]}."""
    _tree_or_404(tree_id, listing_repo)
    return {"slots": [public_slot(s) for s in slot_repo.list_by_tree(tree_id)]}


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
    # H11: the balance gate and the spend post are serialized per claimer —
    # two concurrent slot claims would otherwise both pass the gate and
    # both post, driving the balance negative. The atomic spot claim runs
    # inside the same critical section so a won spot is always paid for
    # exactly once, and a lost race pays nothing.
    with serialize_spend(uid, credit_repo):
        if credit_repo.balance(uid) < cost:
            raise HTTPException(422, {"code": "insufficient_credits",
                                      "message": "Not enough credits — give before you claim"})
        # Atomic claim: the (slot, claimer) record and the claimed_count
        # increment happen in one transaction. A repeat claim by the same user
        # raises AlreadyClaimed (409) instead of double-counting a spot that
        # the idempotent ledger would never charge twice for.
        try:
            updated = slot_repo.claim_slot(slot_id, uid)
        except AlreadyClaimed:
            raise HTTPException(409, {"code": "already_claimed",
                                      "message": "You already claimed a spot in this slot"})
        if updated is None:
            raise HTTPException(409, {"code": "slot_full",
                                      "message": "This slot just filled up"})
        # Credits move through the append-only ledger (same pattern as
        # exchange.confirm). Idempotency keys include the claimer so each claim
        # is a distinct spot purchase.
        base_key = f"slot:{slot_id}:{uid}"
        credit_repo.add_entry(uid, -cost, "slot_spend", ref_id=slot_id,
                              idempotency_key=f"{base_key}:spend")
        credit_repo.add_entry(slot["owner_uid"], cost, "slot_earn", ref_id=slot_id,
                              idempotency_key=f"{base_key}:earn")
    return {"slot": public_slot(updated)}
