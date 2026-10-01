"""API-060: credit ledger, claim, two-party exchange confirmation, wallet."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def mem_exchange(client, monkeypatch):
    from app import claims as claims_mod
    from app import exchange as exchange_mod
    from app import listings as listings_mod
    from app import moderation as moderation_mod
    from app import notify as notify_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from conftest import wire_credit_repo
    from conftest import wire_images_repo
    import app.auth as auth_mod

    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    crepo = wire_credit_repo(client)
    wire_images_repo(client)
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
    assert exchange_mod is not None  # router registered on the app
    return client, urepo, lrepo, crepo, claim_repo, mrepo


ALICE = {"Authorization": "Bearer good-token"}
BOB = {"Authorization": "Bearer bob-token"}
MALLORY = {"Authorization": "Bearer mallory-token"}


def _profile(client, headers, name):
    r = client.post("/v1/users", json={"display_name": name, "age_attestation": True}, headers=headers)
    assert r.status_code == 200, r.text


def _listing_payload(cost=2, **kw):
    base = {
        "type": "seedling",
        "photos": ["https://example.com/t.jpg"],
        "variety": "Cherokee Purple tomato",
        "quantity": 4,
        "unit": "starts",
        "credit_cost": cost,
        "spray_disclosure": "unsprayed",
        "status": "live",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }
    base.update(kw)
    return base


def _make_listing(client, cost=2):
    _profile(client, ALICE, "Alice")
    r = client.post("/v1/listings", json=_listing_payload(cost), headers=ALICE)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_starter_credits_on_profile_creation(mem_exchange):
    client, _, _, crepo, _, _ = mem_exchange
    _profile(client, ALICE, "Alice")
    body = client.get("/v1/wallet", headers=ALICE).json()
    assert body["balance"] == 3
    assert [e["reason"] for e in body["entries"]] == ["starter"]
    assert body["entries"][0]["delta"] == 3
    # Idempotent: updating the profile does not re-grant.
    _profile(client, ALICE, "Alice")
    assert client.get("/v1/wallet", headers=ALICE).json()["balance"] == 3


def test_starter_credits_on_phone_verify_path(mem_exchange):
    client, _, _, _, _, _ = mem_exchange
    r = client.post("/v1/auth/verify", headers=ALICE)
    assert r.status_code == 200, r.text
    # Verify-first user still gets exactly one bootstrap grant.
    r = client.post("/v1/auth/verify", headers=ALICE)
    assert client.get("/v1/wallet", headers=ALICE).json()["balance"] == 3


def test_wallet_has_no_pii(mem_exchange):
    client, _, _, _, _, _ = mem_exchange
    _profile(client, ALICE, "Alice")
    body = client.get("/v1/wallet", headers=ALICE).json()
    # M16: paginated ledger — total/limit/offset join the wire shape.
    assert set(body) == {"uid", "balance", "total", "limit", "offset", "entries"}
    for e in body["entries"]:
        assert set(e) == {"id", "delta", "reason", "ref_id", "created_at"}


def test_claim_and_two_party_confirm_moves_credits(mem_exchange):
    client, _, _, _, _, _ = mem_exchange
    lid = _make_listing(client, cost=2)
    _profile(client, BOB, "Bob")

    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "claimed"
    assert r.json()["claimer_uid"] == "bob"

    # First confirmation: no movement yet.
    r = client.post("/v1/exchange/confirm",
                    json={"listing_id": lid, "idempotency_key": "k1"}, headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "claimed"
    assert r.json()["confirmed_by"] == ["bob"]
    assert client.get("/v1/wallet", headers=BOB).json()["balance"] == 3

    # Second confirmation: both in -> credits move, listing completed.
    r = client.post("/v1/exchange/confirm",
                    json={"listing_id": lid, "idempotency_key": "k1"}, headers=ALICE)
    assert r.json()["status"] == "completed"
    assert sorted(r.json()["confirmed_by"]) == ["alice", "bob"]

    assert client.get("/v1/wallet", headers=BOB).json()["balance"] == 1
    assert client.get("/v1/wallet", headers=ALICE).json()["balance"] == 5
    bob_reasons = [e["reason"] for e in client.get("/v1/wallet", headers=BOB).json()["entries"]]
    alice_reasons = [e["reason"] for e in client.get("/v1/wallet", headers=ALICE).json()["entries"]]
    assert bob_reasons == ["starter", "exchange_spend"]
    assert alice_reasons == ["starter", "exchange_earn"]


def test_confirm_idempotent_on_key_and_after_completion(mem_exchange):
    client, _, _, _, _, _ = mem_exchange
    lid = _make_listing(client, cost=1)
    _profile(client, BOB, "Bob")
    client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    client.post("/v1/exchange/confirm",
                json={"listing_id": lid, "idempotency_key": "k9"}, headers=BOB)
    client.post("/v1/exchange/confirm",
                json={"listing_id": lid, "idempotency_key": "k9"}, headers=ALICE)

    # Same key again: already_confirmed, balances untouched.
    r = client.post("/v1/exchange/confirm",
                    json={"listing_id": lid, "idempotency_key": "k9"}, headers=ALICE)
    assert r.json()["status"] == "already_confirmed"
    assert client.get("/v1/wallet", headers=BOB).json()["balance"] == 2
    assert client.get("/v1/wallet", headers=ALICE).json()["balance"] == 4

    # Confirm after completion without a key: safe no-op.
    r = client.post("/v1/exchange/confirm", json={"listing_id": lid}, headers=BOB)
    assert r.json()["status"] == "completed"
    assert client.get("/v1/wallet", headers=BOB).json()["balance"] == 2


def test_claim_rules(mem_exchange):
    client, _, _, _, _, _ = mem_exchange
    lid = _make_listing(client, cost=2)

    # Owner cannot claim their own listing.
    r = client.post(f"/v1/listings/{lid}/claim", headers=ALICE)
    assert r.status_code == 422
    assert r.json()["code"] == "cannot_claim_own"

    # Claimer needs a profile.
    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 400
    assert r.json()["code"] == "profile_required"
    _profile(client, BOB, "Bob")

    # Successful claim by a funded non-owner.
    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 200
    assert r.json()["claimer_uid"] == "bob"

    # Cannot claim a non-live listing (already claimed).
    _profile(client, MALLORY, "Mallory")
    r = client.post(f"/v1/listings/{lid}/claim", headers=MALLORY)
    assert r.status_code == 422
    assert r.json()["code"] == "listing_not_live"

    # Insufficient credits: spend bob's 3 starter credits on a cost-3
    # exchange, then he cannot afford another claim.
    lid3 = _make_listing(client, cost=3)
    client.post(f"/v1/listings/{lid3}/claim", headers=BOB)
    client.post("/v1/exchange/confirm", json={"listing_id": lid3}, headers=BOB)
    client.post("/v1/exchange/confirm", json={"listing_id": lid3}, headers=ALICE)
    assert client.get("/v1/wallet", headers=BOB).json()["balance"] == 0
    lid4 = _make_listing(client, cost=1)
    r = client.post(f"/v1/listings/{lid4}/claim", headers=BOB)
    assert r.status_code == 422
    assert r.json()["code"] == "insufficient_credits"


def test_confirm_rules(mem_exchange):
    client, _, _, _, _, _ = mem_exchange
    lid = _make_listing(client, cost=2)
    _profile(client, BOB, "Bob")

    # Nothing to confirm before a claim exists.
    r = client.post("/v1/exchange/confirm", json={"listing_id": lid}, headers=ALICE)
    assert r.status_code == 422
    assert r.json()["code"] == "not_claimed"

    client.post(f"/v1/listings/{lid}/claim", headers=BOB)

    # A stranger cannot confirm.
    _profile(client, MALLORY, "Mallory")
    r = client.post("/v1/exchange/confirm", json={"listing_id": lid}, headers=MALLORY)
    assert r.status_code == 403
    assert r.json()["code"] == "not_a_party"

    # Unknown listing.
    r = client.post("/v1/exchange/confirm",
                    json={"listing_id": "00000000-0000-0000-0000-000000000000"},
                    headers=ALICE)
    assert r.status_code == 404


def test_atomic_claim_only_one_winner(mem_exchange):
    """Review: two claimants racing must not both win."""
    client, urepo, lrepo, crepo, _, _ = mem_exchange
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    _profile(client, MALLORY, "Mallory")
    listing_id = _make_listing(client, cost=2)
    assert client.post(f"/v1/listings/{listing_id}/claim", headers=BOB).status_code == 200
    r = client.post(f"/v1/listings/{listing_id}/claim", headers=MALLORY)
    assert r.status_code == 422
    assert lrepo.get(listing_id)["claimer_uid"] == "bob"


def test_double_confirm_moves_credits_exactly_once(mem_exchange):
    """Review: both parties confirming twice (no idempotency key) still moves
    credits exactly once — deterministic server-side keys + atomic flip."""
    client, urepo, lrepo, crepo, _, _ = mem_exchange
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    listing_id = _make_listing(client, cost=2)
    client.post(f"/v1/listings/{listing_id}/claim", headers=BOB)
    body = {"listing_id": listing_id}
    client.post("/v1/exchange/confirm", json=body, headers=ALICE)
    client.post("/v1/exchange/confirm", json=body, headers=BOB)
    # repeat the whole exchange — must be safe no-ops
    r1 = client.post("/v1/exchange/confirm", json=body, headers=ALICE)
    r2 = client.post("/v1/exchange/confirm", json=body, headers=BOB)
    assert r1.json()["status"] == "completed"
    assert r2.json()["status"] == "completed"
    assert crepo.balance("bob") == 3 - 2  # starter 3, spent exactly 2
    assert crepo.balance("alice") == 3 + 2


def test_crash_between_spend_and_earn_recovers_on_retry(mem_exchange, monkeypatch):
    """C3: a crash after the spend leg but before earn/flip must be
    recoverable by retrying with the same idempotency key — the retry
    resumes the earn/flip legs instead of short-circuiting, the earn posts
    exactly once, the listing completes, and the claimer is not
    double-charged."""
    client, urepo, lrepo, crepo, _, _ = mem_exchange
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    listing_id = _make_listing(client, cost=2)
    client.post(f"/v1/listings/{listing_id}/claim", headers=BOB)
    # Claimer confirms first: no money moves yet.
    client.post("/v1/exchange/confirm",
                json={"listing_id": listing_id, "idempotency_key": "crash1"},
                headers=BOB)

    # Simulate the crash: the earn leg raises after the spend leg committed.
    real_add_entry = crepo.add_entry

    def crash_before_earn(uid, delta, reason, ref_id=None, idempotency_key=None):
        if reason == "exchange_earn":
            raise RuntimeError("simulated crash between spend and earn")
        return real_add_entry(uid, delta, reason, ref_id=ref_id,
                              idempotency_key=idempotency_key)

    monkeypatch.setattr(crepo, "add_entry", crash_before_earn)
    with pytest.raises(RuntimeError, match="simulated crash"):
        client.post("/v1/exchange/confirm",
                    json={"listing_id": listing_id, "idempotency_key": "crash1"},
                    headers=ALICE)
    # "Process restarts": the earn leg works again.
    monkeypatch.setattr(crepo, "add_entry", real_add_entry)

    # Partial state: spend posted, earn missing, listing stuck claimed.
    assert crepo.find_by_idempotency_key("crash1:spend") is not None
    assert crepo.find_by_idempotency_key("crash1:earn") is None
    assert lrepo.get(listing_id)["status"] == "claimed"
    assert crepo.balance("bob") == 1  # already charged; balance < cost now

    # Retry with the same key: must resume, not short-circuit — and not be
    # rejected by the balance recheck (the spend already landed).
    r = client.post("/v1/exchange/confirm",
                    json={"listing_id": listing_id, "idempotency_key": "crash1"},
                    headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert lrepo.get(listing_id)["status"] == "completed"

    # Earn posted exactly once; claimer charged exactly once.
    spends = [e for e in crepo.entries("bob") if e["reason"] == "exchange_spend"]
    earns = [e for e in crepo.entries("alice") if e["reason"] == "exchange_earn"]
    assert len(spends) == 1
    assert len(earns) == 1
    assert crepo.balance("bob") == 1
    assert crepo.balance("alice") == 5

    # A further retry is the true no-op short-circuit.
    r = client.post("/v1/exchange/confirm",
                    json={"listing_id": listing_id, "idempotency_key": "crash1"},
                    headers=ALICE)
    assert r.json()["status"] == "already_confirmed"
    assert len([e for e in crepo.entries("bob")
                if e["reason"] == "exchange_spend"]) == 1
    assert len([e for e in crepo.entries("alice")
                if e["reason"] == "exchange_earn"]) == 1
    assert crepo.balance("bob") == 1
    assert crepo.balance("alice") == 5


def test_confirm_rechecks_balance_at_confirm_time(mem_exchange):
    """Review: spending between claim and confirm must not drive a balance
    negative — confirm rechecks."""
    client, urepo, lrepo, crepo, _, _ = mem_exchange
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    listing_id = _make_listing(client, cost=3)
    client.post(f"/v1/listings/{listing_id}/claim", headers=BOB)
    # Bob spends his credits elsewhere before confirming.
    crepo.add_entry("bob", -3, "elsewhere")
    assert client.post("/v1/exchange/confirm", json={"listing_id": listing_id},
                       headers=ALICE).status_code == 200  # owner first: no move yet
    r = client.post("/v1/exchange/confirm", json={"listing_id": listing_id}, headers=BOB)
    assert r.status_code == 422
    assert r.json()["code"] == "insufficient_credits"
    assert lrepo.get(listing_id)["status"] == "claimed"  # not completed
    assert crepo.balance("bob") == 0  # never negative


# ---------------------------------------------------------------- w2-commerce fixes

def test_whole_claim_blocked_for_suspended_user(mem_exchange):
    """H6: a no-show-suspended user blocked on the partial-claim endpoint
    must also be blocked on the whole-listing claim."""
    client, _, _, _, claim_repo, _ = mem_exchange
    lid = _make_listing(client, cost=2)
    _profile(client, BOB, "Bob")
    claim_repo.record_no_show("bob")
    claim_repo.record_no_show("bob")  # 2 strikes -> 30-day suspension
    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "claim_suspended"
    assert "no-show" in r.json()["message"] or "no-shows" in r.json()["message"]


def test_whole_claim_blocked_for_new_account_claim_cap(mem_exchange):
    """H6: a new account that exhausted its rolling claim cap on the
    partial-claim endpoint cannot dodge it via the whole-listing claim."""
    client, _, _, _, claim_repo, _ = mem_exchange
    lid = _make_listing(client, cost=2)
    _profile(client, BOB, "Bob")
    for i in range(5):
        claim_repo.create({"id": f"cap-{i}", "listing_id": lid,
                           "claimer_uid": "bob", "quantity": 1,
                           "pickup_start_ms": 1, "pickup_end_ms": 2,
                           "notes": None})
    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "new_account_claim_cap"


def test_whole_claim_allowed_when_eligible(mem_exchange):
    """H6: eligibility enforcement must not block a clean user."""
    client, _, _, _, _, _ = mem_exchange
    lid = _make_listing(client, cost=2)
    _profile(client, BOB, "Bob")
    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 200, r.text


def test_wallet_pagination(mem_exchange):
    """M16: GET /v1/wallet pages the ledger instead of dumping it whole."""
    client, _, _, crepo, _, _ = mem_exchange
    _profile(client, ALICE, "Alice")  # 1 starter entry
    for i in range(60):
        crepo.add_entry("alice", 1, "starter", ref_id=f"seed-{i}")
    body = client.get("/v1/wallet", headers=ALICE).json()
    assert body["total"] == 61
    assert body["limit"] == 50 and body["offset"] == 0
    assert len(body["entries"]) == 50  # default page, not the whole ledger
    page = client.get("/v1/wallet?limit=10&offset=55", headers=ALICE).json()
    assert page["total"] == 61 and page["limit"] == 10 and page["offset"] == 55
    assert len(page["entries"]) == 6
    assert client.get("/v1/wallet?limit=0", headers=ALICE).status_code == 422
    assert client.get("/v1/wallet?limit=501", headers=ALICE).status_code == 422
    assert client.get("/v1/wallet?offset=-1", headers=ALICE).status_code == 422
