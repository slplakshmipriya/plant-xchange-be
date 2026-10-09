"""Claim + two-party exchange confirmation + wallet (API-060).

- ``POST /v1/listings/{id}/claim``: any profile-holding non-owner claims a live
  listing; needs ``balance >= credit_cost`` (negative balances are not allowed).
  Sets ``claimer_uid``, transitions ``live -> claimed``.
- ``POST /v1/exchange/confirm``: giver and claimer each confirm; credits move
  exactly once when both confirmations are in (claimer ``-cost``,
  owner ``+cost``), transitioning ``claimed -> completed``.
- Idempotency: client ``idempotency_key`` on confirm; repeats return the prior
  result without moving credits twice. The ``(listing_id, uid)`` PK makes
  double confirmation a no-op, and the status gate makes double completion
  impossible. Spend/earn/flip commit atomically (a single DB transaction on
  the Postgres path). A retry with the same key after a mid-flight crash
  does NOT short-circuit on the spend leg alone: it resumes the idempotent
  spend/earn/flip steps until the flip lands, so "recoverable by retry" is
  actually true.
- ``GET /v1/wallet``: derived balance + paginated append-only history.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import get_current_uid
from .claims import ClaimRepo, _enforce_claim_eligibility, get_claim_repo, serialize_spend
from .config import get_settings
from .credits import CreditRepo, get_credit_repo
from .images import (
    GCSBlobStore,
    StoredImagesRepo,
    get_blob_store_or_none,
    get_images_repo,
    release_listing_images,
)
from .listings import ListingRepo, batch_owners, can_transition, get_listing_repo, public_listing
from .moderation import ModerationRepo, get_moderation_repo
from .txn import atomic as _atomic
from .vertical import get_vertical
from .users import UserRepo, get_user_repo

router = APIRouter(prefix="/v1", tags=["credits"])


def _serialize_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "delta": row["delta"],
        "reason": row["reason"],
        "ref_id": row.get("ref_id"),
        "created_at": row.get("created_at"),
    }


class ConfirmIn(BaseModel):
    listing_id: str = Field(min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=128)


@router.post("/listings/{listing_id}/claim", status_code=200, tags=["listings"])
def claim_listing(
    listing_id: str,
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    user_repo: UserRepo = Depends(get_user_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
    claim_repo: ClaimRepo = Depends(get_claim_repo),
    mod_repo: ModerationRepo = Depends(get_moderation_repo),
) -> dict[str, Any]:
    """Claim a live listing. Not the owner; needs balance >= credit_cost."""
    row = repo.get(listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    if row["owner_uid"] == uid:
        raise HTTPException(422, {"code": "cannot_claim_own",
                                  "message": "You cannot claim your own listing"})
    if row["status"] != "live":
        raise HTTPException(422, {"code": "listing_not_live",
                                  "message": f"Cannot claim a '{row['status']}' listing"})
    if user_repo.get(uid) is None:
        raise HTTPException(400, {"code": "profile_required",
                                  "message": "Create a profile (POST /v1/users) before claiming"})
    # H6: the whole-listing claim must enforce the same suspension
    # rules as the partial-claim endpoint — otherwise a no-show-suspended
    # user blocked on POST /v1/listings/{id}/claims can simply claim
    # here instead.
    _enforce_claim_eligibility(uid, claim_repo, mod_repo)
    # credits_enabled=False: free exchange — no balance gate (confirm
    # moves nothing either; see confirm_exchange).
    if (get_vertical().economy.credits_enabled
            and credit_repo.balance(uid) < row["credit_cost"]):
        raise HTTPException(422, {"code": "insufficient_credits",
                                  "message": "Not enough credits — give before you claim"})
    # Atomic: concurrent claimants cannot both win; the loser gets None.
    updated = repo.claim(listing_id, uid)
    if updated is None:
        raise HTTPException(422, {"code": "listing_not_live",
                                  "message": "Someone just claimed this listing"})
    return public_listing(updated, viewer_uid=None,
                          owners=batch_owners(user_repo, [updated]))


@router.post("/exchange/confirm", tags=["credits"])
def confirm_exchange(
    data: ConfirmIn,
    uid: str = Depends(get_current_uid),
    repo: ListingRepo = Depends(get_listing_repo),
    credit_repo: CreditRepo = Depends(get_credit_repo),
    images_repo: StoredImagesRepo = Depends(get_images_repo),
    blob_store: GCSBlobStore | None = Depends(get_blob_store_or_none),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Both parties confirm; credits move exactly once when both are in."""
    if data.idempotency_key:
        # C3: never short-circuit on the spend leg alone — a crash after
        # spend but before earn/flip would otherwise strand the exchange
        # (claimer charged, owner unpaid, listing stuck claimed) while the
        # retry reports success. Return early only when the earn leg AND
        # the status flip both completed; otherwise fall through and resume
        # the idempotent steps below.
        row0 = repo.get(data.listing_id)
        spend = credit_repo.find_by_idempotency_key(f"{data.idempotency_key}:spend")
        earn = credit_repo.find_by_idempotency_key(f"{data.idempotency_key}:earn")
        fully_done = (spend is not None and earn is not None
                      and row0 is not None and row0["status"] == "completed")
        if fully_done or (spend is not None and row0 is None):
            owners0 = batch_owners(user_repo, [row0]) if row0 else {}
            return {"status": "already_confirmed",
                    "listing": public_listing(row0, viewer_uid=None, owners=owners0) if row0 else None}
    row = repo.get(data.listing_id)
    if row is None:
        raise HTTPException(404, {"code": "listing_not_found", "message": "No such listing"})
    if row["status"] == "completed" and row.get("claimer_uid"):
        # Repeat confirm after completion: safe no-op, not an error.
        return {"status": "completed",
                "confirmed_by": sorted(credit_repo.confirmations(data.listing_id)),
                "listing": public_listing(row, viewer_uid=None,
                                           owners=batch_owners(user_repo, [row]))}
    if row["status"] != "claimed" or not row.get("claimer_uid"):
        raise HTTPException(422, {"code": "not_claimed",
                                  "message": "Nothing to confirm — listing is not claimed"})
    if uid not in (row["owner_uid"], row["claimer_uid"]):
        raise HTTPException(403, {"code": "not_a_party",
                                  "message": "Only the giver and claimer can confirm"})
    credit_repo.add_confirmation(data.listing_id, uid)
    confirmed = set(credit_repo.confirmations(data.listing_id))
    if {row["owner_uid"], row["claimer_uid"]} <= confirmed and can_transition("claimed", "completed"):
        cost = row["credit_cost"]
        # credits_enabled=False: the exchange still completes (status
        # flip + image release) but no credits move and no balance
        # recheck runs — the ledger legs below are skipped entirely.
        credits_on = get_vertical().economy.credits_enabled
        # Deterministic server-side keys: exactly-once credit moves even when
        # the client sends no idempotency key and two confirms race.
        base_key = data.idempotency_key or f"exchange:{data.listing_id}"
        spend_key = f"{base_key}:spend"
        # Balance can change between claim and confirm — recheck so a confirm
        # can never drive a balance negative. Skipped when the spend leg
        # already posted under this key: it passed the check when first
        # posted and a re-post is a no-op, so a crash-recovery retry must not
        # be rejected (the claimer's balance already reflects the spend).
        # The recheck and the spend post are serialized per claimer (H11):
        # two concurrent confirms would otherwise both pass the gate and
        # both spend, driving the balance negative.
        with serialize_spend(row["claimer_uid"], credit_repo):
            if (credits_on
                    and credit_repo.find_by_idempotency_key(spend_key) is None
                    and credit_repo.balance(row["claimer_uid"]) < cost):
                raise HTTPException(422, {"code": "insufficient_credits",
                                          "message": "Claimer no longer has enough credits"})
            # Spend + earn + flip are one atomic transaction on the Postgres
            # path (see _atomic), so a crash cannot strand partial state. Each
            # leg is idempotent on its own key, and a retry with the same client
            # key resumes here instead of short-circuiting (see the top of this
            # function) — a mid-flight crash is recoverable by retry: re-adds
            # are no-ops, then the flip completes.
            completed = None
            with _atomic(repo, credit_repo):
                if credits_on and cost:
                    # cost == 0 (free listing confirmed after a credits
                    # flip-on) posts NO legs — zero-delta rows are
                    # forbidden by the ledger CHECK and move nothing.
                    credit_repo.add_entry(row["claimer_uid"], -cost, "exchange_spend",
                                          ref_id=data.listing_id,
                                          idempotency_key=spend_key)
                    credit_repo.add_entry(row["owner_uid"], cost, "exchange_earn",
                                          ref_id=data.listing_id,
                                          idempotency_key=f"{base_key}:earn")
                completed = repo.complete_if_claimed(data.listing_id)
                if completed is not None:
                    row = completed
            if completed is not None:
                # The swap is done: its photos no longer back an active
                # listing. Refcounted release — objects shared via dedupe
                # survive until the last referencing listing is released.
                # No-op unless STORAGE_BACKEND=gcs.
                release_listing_images(
                    completed.get("photos"),
                    images_repo=images_repo,
                    blob_store=blob_store,
                    bucket=get_settings().gcs_bucket,
                )
    return {
        "status": row["status"],
        "confirmed_by": sorted(confirmed),
        "listing": public_listing(row, viewer_uid=None,
                                   owners=batch_owners(user_repo, [row])),
    }


@router.get("/wallet", tags=["credits"])
def get_wallet(
    uid: str = Depends(get_current_uid),
    credit_repo: CreditRepo = Depends(get_credit_repo),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Derived balance + paginated append-only history (M16). No PII in entries.

    The ledger is unbounded, so the full history is never returned in one
    response: callers page with ``limit``/``offset`` (default 50 per page).
    """
    all_entries = credit_repo.entries(uid)
    total = len(all_entries)
    page = all_entries[offset:offset + limit]
    return {
        "uid": uid,
        "balance": credit_repo.balance(uid),
        "total": total,
        "limit": limit,
        "offset": offset,
        "entries": [_serialize_entry(e) for e in page],
    }
