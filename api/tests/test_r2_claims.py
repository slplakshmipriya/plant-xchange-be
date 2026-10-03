"""R2 claims lifecycle: partial-quantity claims, accept/decline/cancel,
no-show strikes -> suspension, and the new-account claim cap."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def mem_claims(client, monkeypatch):
    from app import claims as claims_mod
    from app import listings as listings_mod
    from app import moderation as moderation_mod
    from app import notify as notify_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from conftest import wire_credit_repo
    import app.auth as auth_mod

    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    crepo = wire_credit_repo(client)
    wrepo = wantlist_mod.MemoryWantRepo()
    nrepo = notify_mod.MemoryNotificationRepo()
    claim_repo = claims_mod.MemoryClaimRepo()
    mrepo = moderation_mod.MemoryModerationRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wrepo
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: nrepo
    client.app.dependency_overrides[claims_mod.get_claim_repo] = lambda: claim_repo
    client.app.dependency_overrides[moderation_mod.get_moderation_repo] = lambda: mrepo

    def fake(token: str) -> dict:
        if token == "good-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "bob-token":
            return {"uid": "bob"}
        if token == "mallory-token":
            return {"uid": "mallory"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)
    return client, urepo, lrepo, crepo, claim_repo


ALICE = {"Authorization": "Bearer good-token"}
BOB = {"Authorization": "Bearer bob-token"}
MALLORY = {"Authorization": "Bearer mallory-token"}


def _profile(client, headers, name):
    r = client.post("/v1/users", json={"display_name": name, "age_attestation": True}, headers=headers)
    assert r.status_code == 200, r.text


def _listing_payload(quantity=10, cost=1, **kw):
    base = {
        "type": "harvest",
        "photos": ["https://example.com/t.jpg"],
        "variety": "Cherokee Purple tomato",
        "quantity": quantity,
        "unit": "kg",
        "credit_cost": cost,
        "spray_disclosure": "unsprayed",
        "status": "live",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }
    base.update(kw)
    return base


def _make_listing(client, headers, **kw):
    _profile(client, headers, "Giver")
    r = client.post("/v1/listings", json=_listing_payload(**kw), headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _claim_body(quantity=3, **kw):
    base = {
        "quantity": quantity,
        "pickupStartMs": 1_800_000_000_000,
        "pickupEndMs": 1_800_003_600_000,
        "notes": "will bring my own bag",
    }
    base.update(kw)
    return base


def _claim(client, listing_id, headers, **kw):
    r = client.post(f"/v1/listings/{listing_id}/claims",
                    json=_claim_body(**kw), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_partial_claim_decrements_and_stays_live(mem_claims):
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    body = _claim(client, lid, BOB, quantity=3)
    assert body["listing"]["remaining_qty"] == 7
    assert body["listing"]["status"] == "live"  # partial claim keeps it live
    assert body["claim"]["status"] == "pending"
    assert body["claim"]["quantity"] == 3
    assert body["claim"]["claimer_uid"] == "bob"


def test_claim_to_zero_closes_listing(mem_claims):
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    body = _claim(client, lid, BOB, quantity=10)
    assert body["listing"]["remaining_qty"] == 0
    assert body["listing"]["status"] == "completed"


def test_claim_validation(mem_claims):
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    # quantity < 1 -> 422
    r = client.post(f"/v1/listings/{lid}/claims", json=_claim_body(quantity=0), headers=BOB)
    assert r.status_code == 422, r.text
    # quantity > available -> 409
    r = client.post(f"/v1/listings/{lid}/claims", json=_claim_body(quantity=99), headers=BOB)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "insufficient_quantity"
    # bad pickup window -> 422
    r = client.post(f"/v1/listings/{lid}/claims",
                    json=_claim_body(pickupStartMs=5, pickupEndMs=4), headers=BOB)
    assert r.status_code == 422, r.text
    # cannot claim your own listing
    r = client.post(f"/v1/listings/{lid}/claims", json=_claim_body(), headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "cannot_claim_own"
    # missing listing -> 404
    r = client.post("/v1/listings/does-not-exist/claims", json=_claim_body(), headers=BOB)
    assert r.status_code == 404, r.text


def test_claim_requires_tracked_quantity(mem_claims):
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE, quantity=None)
    _profile(client, BOB, "Bob")

    r = client.post(f"/v1/listings/{lid}/claims", json=_claim_body(), headers=BOB)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "quantity_not_tracked"


def test_cancel_restores_quantity(mem_claims):
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    # Claimer cancels their own claim.
    _claim(client, lid, BOB, quantity=3)
    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["listing"]["remaining_qty"] == 10
    assert r.json()["claim"]["status"] == "cancelled"

    # Giver cancels the claimer's claim.
    _claim(client, lid, BOB, quantity=4)
    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["listing"]["remaining_qty"] == 10
    assert r.json()["claim"]["status"] == "cancelled"

    # Nothing active left to cancel.
    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=BOB)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "no_active_claim"


def test_accept_decline_permissions(mem_claims):
    client, _, _, _, claim_repo = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")
    _profile(client, MALLORY, "Mallory")

    body = _claim(client, lid, BOB, quantity=3)
    claim_id = body["claim"]["id"]

    # Non-giver cannot accept.
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=MALLORY)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "not_giver"
    # Claimer cannot accept their own claim either.
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=BOB)
    assert r.status_code == 403, r.text

    # Giver accepts: pending -> accepted, quantity stays out.
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["claim"]["status"] == "accepted"
    assert r.json()["listing"]["remaining_qty"] == 7

    # Accepting twice is a conflict.
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 409, r.text

    # Unknown claim id -> 404.
    r = client.post(f"/v1/listings/{lid}/claims/decline",
                    json={"claimId": "nope"}, headers=ALICE)
    assert r.status_code == 404, r.text

    # Giver declines a fresh claim: quantity restored.
    body2 = _claim(client, lid, BOB, quantity=2)
    r = client.post(f"/v1/listings/{lid}/claims/decline",
                    json={"claimId": body2["claim"]["id"]}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["claim"]["status"] == "declined"
    assert r.json()["listing"]["remaining_qty"] == 7


def test_no_show_suspension_blocks_claim(mem_claims):
    client, _, _, _, _ = mem_claims
    _profile(client, BOB, "Bob")

    # Strike 1: bob no-shows on alice's listing (pickup window long past).
    lid = _make_listing(client, ALICE)
    body = _claim(client, lid, BOB, quantity=3,
                  pickupStartMs=1_700_000_000_000, pickupEndMs=1_700_003_600_000)
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": body["claim"]["id"]}, headers=ALICE)
    assert r.status_code == 200, r.text

    r = client.post(f"/v1/exchanges/{lid}/no-show",
                    json={"side": "claimer"}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["uid"] == "bob"
    assert r.json()["no_shows"] == 1
    assert r.json()["suspended_until"] is None

    # Strike 2 needs a SECOND claim — re-reporting this one is a 409 (M1).
    lid2 = _make_listing(client, ALICE)
    body = _claim(client, lid2, BOB, quantity=3,
                  pickupStartMs=1_700_000_000_000, pickupEndMs=1_700_003_600_000)
    r = client.post(f"/v1/listings/{lid2}/claims/accept",
                    json={"claimId": body["claim"]["id"]}, headers=ALICE)
    assert r.status_code == 200, r.text
    r = client.post(f"/v1/exchanges/{lid2}/no-show",
                    json={"side": "claimer"}, headers=ALICE)
    assert r.json()["no_shows"] == 2
    assert r.json()["suspended_until"] is not None

    # Bob's next claim is blocked.
    lid3 = _make_listing(client, ALICE)
    r = client.post(f"/v1/listings/{lid3}/claims", json=_claim_body(), headers=BOB)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "claim_suspended"


def test_no_show_report_deduped_per_claim(mem_claims):
    """M1: one strike per (claim, reporter) — double-posting the same
    claim used to suspend a counterparty in two calls."""
    client, _, _, _, claim_repo = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    body = _claim(client, lid, BOB, quantity=3,
                  pickupStartMs=1_700_000_000_000, pickupEndMs=1_700_003_600_000)
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": body["claim"]["id"]}, headers=ALICE)
    assert r.status_code == 200, r.text

    r = client.post(f"/v1/exchanges/{lid}/no-show",
                    json={"side": "claimer"}, headers=ALICE)
    assert r.status_code == 200, r.text
    r = client.post(f"/v1/exchanges/{lid}/no-show",
                    json={"side": "claimer"}, headers=ALICE)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "no_show_already_reported"
    assert claim_repo.get_strikes("bob")["no_shows"] == 1

    # And a party cannot report their own side.
    r = client.post(f"/v1/exchanges/{lid}/no-show",
                    json={"side": "claimer"}, headers=BOB)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "cannot_report_self"


def test_no_show_before_pickup_window_end_rejected(mem_claims):
    """M1: a no-show cannot be reported before the pickup window ends."""
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    # Default _claim_body window is in 2027 — still open.
    body = _claim(client, lid, BOB, quantity=3)
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": body["claim"]["id"]}, headers=ALICE)
    assert r.status_code == 200, r.text

    r = client.post(f"/v1/exchanges/{lid}/no-show",
                    json={"side": "claimer"}, headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "pickup_window_open"


def test_moderation_suspension_blocks_claim(mem_claims):
    """A moderation-track suspension (verified strikes) also blocks claims."""
    from datetime import datetime, timedelta, timezone

    from app import moderation as moderation_mod

    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    mrepo = moderation_mod.MemoryModerationRepo()
    client.app.dependency_overrides[moderation_mod.get_moderation_repo] = lambda: mrepo
    mrepo.add_enforcement(
        "bob", "claims", "suspension",
        datetime.now(timezone.utc) + timedelta(days=90), "verified complaints")

    r = client.post(f"/v1/listings/{lid}/claims", json=_claim_body(), headers=BOB)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "claim_suspended"


def test_no_show_giver_side_suspends_giver(mem_claims):
    client, _, _, _, _ = mem_claims
    _profile(client, BOB, "Bob")

    # Claimer reports the giver as a no-show on two distinct claims.
    for _ in range(2):
        lid = _make_listing(client, ALICE)
        body = _claim(client, lid, BOB, quantity=3,
                      pickupStartMs=1_700_000_000_000, pickupEndMs=1_700_003_600_000)
        r = client.post(f"/v1/listings/{lid}/claims/accept",
                        json={"claimId": body["claim"]["id"]}, headers=ALICE)
        assert r.status_code == 200, r.text
        r = client.post(f"/v1/exchanges/{lid}/no-show",
                        json={"side": "giver"}, headers=BOB)
        assert r.status_code == 200, r.text
    assert r.json()["uid"] == "alice"
    assert r.json()["suspended_until"] is not None

    # Now alice cannot claim on someone else's listing.
    lid2 = _make_listing(client, BOB)
    r = client.post(f"/v1/listings/{lid2}/claims", json=_claim_body(), headers=ALICE)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "claim_suspended"


def test_no_show_requires_active_claim(mem_claims):
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    r = client.post(f"/v1/exchanges/{lid}/no-show",
                    json={"side": "claimer"}, headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "no_active_claim"


def test_new_account_not_claim_capped(mem_claims):
    """Product decision 2026-10-02: no new-account claim cap — claiming
    keeps the ecosystem going, so a fresh account may claim freely."""
    client, _, _, _, _ = mem_claims
    _profile(client, BOB, "Bob")  # fresh account: < 14 days old

    # Well past the old 5-per-rolling-7-days cap: every claim succeeds.
    for _ in range(6):
        lid = _make_listing(client, ALICE)
        r = client.post(f"/v1/listings/{lid}/claims", json=_claim_body(), headers=BOB)
        assert r.status_code == 200, r.text


def test_check_pillar_suspension_helper(mem_claims):
    from app import claims as claims_mod

    _, _, _, _, claim_repo = mem_claims

    assert claims_mod.check_pillar_suspension("bob", "claims", claim_repo) is None
    assert claims_mod.check_pillar_suspension("bob", "other-pillar", claim_repo) is None

    claim_repo.record_no_show_report("claim-1", "alice", "bob")
    assert claims_mod.check_pillar_suspension("bob", "claims", claim_repo) is None
    claim_repo.record_no_show_report("claim-2", "alice", "bob")
    suspension = claims_mod.check_pillar_suspension("bob", "claims", claim_repo)
    assert suspension is not None
    assert suspension["pillar"] == "claims"
    assert suspension["reason"] == "no_show_strikes"
    assert suspension["until"] is not None


# ---------------------------------------------------------------------------
# C2: the partial-claim money leg — ledger entries per lifecycle transition
# ---------------------------------------------------------------------------

def _claim_entries(crepo, uid, claim_id):
    """All ledger entries this claim produced for this user."""
    return [e for e in crepo.entries(uid)
            if e.get("ref_id") == claim_id and e["reason"].startswith("claim")]


def test_claim_lifecycle_money_leg(mem_claims):
    """create -> no charge; accept -> exactly one spend + one earn;
    double-accept -> no duplicate entries (idempotent)."""
    client, _, _, crepo, _ = mem_claims
    lid = _make_listing(client, ALICE)  # credit_cost=1
    _profile(client, BOB, "Bob")        # +3 starter credits each
    assert crepo.balance("bob") == 3
    assert crepo.balance("alice") == 3

    # create: the insufficient_credits gate is kept, but no credits move.
    body = _claim(client, lid, BOB, quantity=3)
    claim_id = body["claim"]["id"]
    assert _claim_entries(crepo, "bob", claim_id) == []
    assert _claim_entries(crepo, "alice", claim_id) == []
    assert crepo.balance("bob") == 3
    assert crepo.balance("alice") == 3

    # accept: exactly one spend (claimer) + one earn (giver).
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 200, r.text
    spends = [e for e in _claim_entries(crepo, "bob", claim_id)
              if e["reason"] == "claim_spend"]
    earns = [e for e in _claim_entries(crepo, "alice", claim_id)
             if e["reason"] == "claim_earn"]
    assert len(spends) == 1 and spends[0]["delta"] == -1
    assert spends[0]["idempotency_key"] == f"claim:{claim_id}:spend"
    assert len(earns) == 1 and earns[0]["delta"] == 1
    assert earns[0]["idempotency_key"] == f"claim:{claim_id}:earn"
    assert crepo.balance("bob") == 2
    assert crepo.balance("alice") == 4

    # double-accept: 409, and the money leg posts nothing new.
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 409, r.text
    assert len(_claim_entries(crepo, "bob", claim_id)) == 1
    assert len(_claim_entries(crepo, "alice", claim_id)) == 1
    assert crepo.balance("bob") == 2
    assert crepo.balance("alice") == 4


def test_accept_high_price_listing_moves_credits_once(mem_claims):
    """C1 regression (code review 2026-10-02): with prices up to 100
    (migration 0039) the giver's claim_earn is a transfer and exempt from
    the 7-day issuance cap — accepting a 20-credit claim must move the
    credits exactly once, never burn the claimer's spend."""
    client, _, _, crepo, _ = mem_claims
    lid = _make_listing(client, ALICE, cost=20)
    _profile(client, BOB, "Bob")
    # Bob holds 28 (3 starter + 25 of transfer earnings, all cap-exempt).
    crepo.add_entry("bob", 25, "exchange_earn", idempotency_key="fund:bob")
    assert crepo.balance("bob") == 28
    # Alice is already at the issuance cap for the window: irrelevant here.
    crepo.add_entry("alice", 10, "welcome_bonus", idempotency_key="fund:alice")

    body = _claim(client, lid, BOB, quantity=3)
    claim_id = body["claim"]["id"]

    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["claim"]["status"] == "accepted"
    spends = [e for e in _claim_entries(crepo, "bob", claim_id)
              if e["reason"] == "claim_spend"]
    earns = [e for e in _claim_entries(crepo, "alice", claim_id)
             if e["reason"] == "claim_earn"]
    assert len(spends) == 1 and spends[0]["delta"] == -20
    assert len(earns) == 1 and earns[0]["delta"] == 20
    assert crepo.balance("bob") == 8
    assert crepo.balance("alice") == 33  # 3 starter + 10 bonus + 20 earn

    # Cancelling the accepted claim unwinds the full 20, not a partial leg.
    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=BOB)
    assert r.status_code == 200, r.text
    assert crepo.balance("bob") == 28
    assert crepo.balance("alice") == 13


def test_accept_rechecks_claimer_balance(mem_claims):
    """The claimer's balance can drop between create and accept: the
    accept-time recheck (same as exchange.confirm) 422s and the claim stays
    pending with no money leg posted."""
    client, _, _, crepo, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    body = _claim(client, lid, BOB, quantity=3)
    claim_id = body["claim"]["id"]

    # Bob spends his credits elsewhere before the giver accepts.
    crepo.add_entry("bob", -3, "exchange_spend", ref_id="elsewhere",
                    idempotency_key="drain:bob")
    assert crepo.balance("bob") == 0

    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "insufficient_credits"

    assert _claim_entries(crepo, "bob", claim_id) == []
    assert _claim_entries(crepo, "alice", claim_id) == []


def test_decline_moves_no_credits(mem_claims):
    """A declined claim never reached accept: no charge persists."""
    client, _, _, crepo, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    body = _claim(client, lid, BOB, quantity=3)
    claim_id = body["claim"]["id"]
    r = client.post(f"/v1/listings/{lid}/claims/decline",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 200, r.text

    assert _claim_entries(crepo, "bob", claim_id) == []
    assert _claim_entries(crepo, "alice", claim_id) == []
    assert crepo.balance("bob") == 3
    assert crepo.balance("alice") == 3


def test_cancel_pending_claim_moves_no_credits(mem_claims):
    """Cancelling a pending claim (claimer or giver path) moves no credits."""
    client, _, _, crepo, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    # Claimer cancels their own pending claim.
    body = _claim(client, lid, BOB, quantity=3)
    claim_id = body["claim"]["id"]
    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=BOB)
    assert r.status_code == 200, r.text
    assert _claim_entries(crepo, "bob", claim_id) == []
    assert _claim_entries(crepo, "alice", claim_id) == []

    # Giver cancels the claimer's pending claim.
    body = _claim(client, lid, BOB, quantity=2)
    claim_id = body["claim"]["id"]
    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=ALICE)
    assert r.status_code == 200, r.text
    assert _claim_entries(crepo, "bob", claim_id) == []
    assert _claim_entries(crepo, "alice", claim_id) == []

    assert crepo.balance("bob") == 3
    assert crepo.balance("alice") == 3


def test_cancel_accepted_claim_reverses_money_leg(mem_claims):
    """Cancelling an accepted claim unwinds the money leg: the quantity is
    restored AND the payment is reversed, so the claim nets to zero."""
    client, _, _, crepo, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")

    body = _claim(client, lid, BOB, quantity=3)
    claim_id = body["claim"]["id"]
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert crepo.balance("bob") == 2
    assert crepo.balance("alice") == 4

    # Claimer cancels the accepted claim: refund + giver debit.
    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["claim"]["status"] == "cancelled"

    refunds = [e for e in _claim_entries(crepo, "bob", claim_id)
               if e["reason"] == "claim_reversal"]
    debits = [e for e in _claim_entries(crepo, "alice", claim_id)
              if e["reason"] == "claim_reversal"]
    assert len(refunds) == 1 and refunds[0]["delta"] == 1
    assert refunds[0]["idempotency_key"] == f"claim:{claim_id}:reversal:claimer"
    assert len(debits) == 1 and debits[0]["delta"] == -1
    assert debits[0]["idempotency_key"] == f"claim:{claim_id}:reversal:giver"

    # Net claim-related delta is zero for both sides.
    assert sum(e["delta"] for e in _claim_entries(crepo, "bob", claim_id)) == 0
    assert sum(e["delta"] for e in _claim_entries(crepo, "alice", claim_id)) == 0
    assert crepo.balance("bob") == 3
    assert crepo.balance("alice") == 3

    # Nothing left to cancel: no double refund.
    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=BOB)
    assert r.status_code == 404, r.text
    assert len(_claim_entries(crepo, "bob", claim_id)) == 2


# ---------------------------------------------------------------------------
# w2-commerce fixes: H7, H8, M5b, L2, L3
# ---------------------------------------------------------------------------

def test_restore_quantity_concurrent_no_lost_update(mem_claims):
    """H7: concurrent restores must not lose updates (memory-path lock)."""
    import threading

    from app import claims as claims_mod

    client, _, lrepo, _, _ = mem_claims
    lid = _make_listing(client, ALICE, quantity=100)
    lrepo.update(lid, {"remaining_qty": 5.0})

    barrier = threading.Barrier(8)

    def restore():
        barrier.wait()
        claims_mod._restore_quantity(lid, 2.0, lrepo)

    threads = [threading.Thread(target=restore) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 5 + 8*2 = 21 exactly; a read-modify-write race would land lower.
    assert lrepo.get(lid)["remaining_qty"] == 21.0


def test_restore_quantity_caps_at_listing_quantity(mem_claims):
    """H7: a restore never inflates remaining_qty past the listing quantity."""
    from app import claims as claims_mod

    client, _, lrepo, _, _ = mem_claims
    lid = _make_listing(client, ALICE, quantity=10)
    lrepo.update(lid, {"remaining_qty": 9.0})
    updated = claims_mod._restore_quantity(lid, 5.0, lrepo)
    assert updated["remaining_qty"] == 10.0


def test_set_status_conditional_flip(mem_claims):
    """H8: set_status only flips pending/accepted claims; anything else
    returns None so the route can 409 instead of double-restoring."""
    _, _, _, _, claim_repo = mem_claims

    row = claim_repo.create({"id": "c1", "listing_id": "l1",
                             "claimer_uid": "bob", "quantity": 1,
                             "pickup_start_ms": 1, "pickup_end_ms": 2,
                             "notes": None})
    assert claim_repo.set_status("c1", "accepted")["status"] == "accepted"
    # accepted -> declined is a legal conditional flip...
    assert claim_repo.set_status("c1", "declined")["status"] == "declined"
    # ...but a second flip from a terminal state is refused (loser 409s).
    assert claim_repo.set_status("c1", "accepted") is None
    assert claim_repo.set_status("c1", "cancelled") is None
    assert claim_repo.set_status("missing", "cancelled") is None
    assert row["status"] == "pending"


def test_accept_after_cancel_409(mem_claims):
    """H8 route-level: once a claim is cancelled, accept is a 409, not a
    silent double transition."""
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE)
    _profile(client, BOB, "Bob")
    claim_id = _claim(client, lid, BOB)["claim"]["id"]

    r = client.post(f"/v1/listings/{lid}/claims/cancel", json={}, headers=BOB)
    assert r.status_code == 200, r.text
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "claim_not_pending"


def test_fractional_quantity_claim(mem_claims):
    """L3: a 0.5 kg listing can be claimed (ClaimIn minimum is now gt=0)."""
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE, quantity=0.5)
    _profile(client, BOB, "Bob")

    body = _claim(client, lid, BOB, quantity=0.5)
    assert body["claim"]["quantity"] == 0.5
    assert body["listing"]["remaining_qty"] == 0
    assert body["listing"]["status"] == "completed"

    # Zero / negative quantities are still rejected.
    lid2 = _make_listing(client, ALICE, quantity=0.5)
    r = client.post(f"/v1/listings/{lid2}/claims",
                    json=_claim_body(quantity=0), headers=BOB)
    assert r.status_code == 422, r.text
    r = client.post(f"/v1/listings/{lid2}/claims",
                    json=_claim_body(quantity=-0.5), headers=BOB)
    assert r.status_code == 422, r.text


def test_fractional_claim_chain_closes_listing(mem_claims):
    """M5b: repeated fractional claims must reach the fully-picked
    transition even if float dust remains (epsilon compare)."""
    client, _, _, _, _ = mem_claims
    lid = _make_listing(client, ALICE, quantity=0.3)
    _profile(client, BOB, "Bob")

    for _ in range(2):
        body = _claim(client, lid, BOB, quantity=0.1)
        assert body["listing"]["status"] == "live"
    body = _claim(client, lid, BOB, quantity=0.1)
    assert body["listing"]["status"] == "completed"


def test_check_pillar_suspension_requires_explicit_repo():
    """L2: no module-global repo fallback — a missing repo raises loudly
    instead of reusing another request's connection."""
    import pytest

    from app import claims as claims_mod

    with pytest.raises(RuntimeError):
        claims_mod.check_pillar_suspension("bob", "claims", None)
    with pytest.raises(RuntimeError):
        claims_mod.check_pillar_suspension("bob", "claims")
