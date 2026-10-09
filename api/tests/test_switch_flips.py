"""Switch flip-matrix regressions (review findings on the economy switches).

The switches (economy.credits_enabled / usd_services_enabled) can flip
between any two requests because the vertical is deployment config.
These tests pin the money paths at the flip boundaries:

- zero-amount ledger legs never post (the PG ledger CHECK forbids
  delta = 0): free flows completed after a flip ON move nothing;
- a claim cancel unwinds what the forward leg ACTUALLY moved, even if
  the listing was re-priced after the accept;
- a dispute reversal with credits off requires real forward legs;
- starter_credits=0 grants nothing (and posts no zero row);
- above-ceiling priced writes while credits are OFF reach the route's
  enveloped credits_disabled 422 instead of a bare validation error;
- payment intents gate on the booking's denomination SNAPSHOT
  (migration 0044), failing closed when it is unknown.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.vertical import get_vertical, reset_vertical_cache


@pytest.fixture(autouse=True)
def _vertical_env(monkeypatch):
    monkeypatch.delenv("VERTICAL_CONFIG_PATH", raising=False)
    monkeypatch.delenv("VERTICAL_ID", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_vertical_cache()
    yield
    reset_vertical_cache()


@pytest.fixture()
def flip(monkeypatch, tmp_path):
    """Rewrite the active vertical's economy section mid-test."""
    path = tmp_path / "flipville.json"

    def _set(**economy):
        path.write_text(json.dumps({
            "vertical_id": "flipville", "economy": economy,
        }), encoding="utf-8")
        monkeypatch.setenv("VERTICAL_CONFIG_PATH", str(path))
        reset_vertical_cache()
        return get_vertical()

    return _set


@pytest.fixture()
def mem(client, monkeypatch):
    """Full in-memory stack (mirrors test_economy_switches.mem_switch,
    plus the moderation repo for dispute flows)."""
    from app import claims as claims_mod
    from app import listings as listings_mod
    from app import moderation as moderation_mod
    from app import notify as notify_mod
    from app import payments as payments_mod
    from app import sitter as sitter_mod
    from app import slots as slots_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from conftest import wire_credit_repo, wire_images_repo
    import app.auth as auth_mod

    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    claim_repo = claims_mod.MemoryClaimRepo()
    slot_repo = slots_mod.MemorySlotRepo()
    sit_repo = sitter_mod.MemorySitterRepo()
    mrepo = moderation_mod.MemoryModerationRepo()
    nrepo = notify_mod.MemoryNotificationRepo()
    wrepo = wantlist_mod.MemoryWantRepo()
    prepo = payments_mod.MemoryPaymentRepo()
    crepo = wire_credit_repo(client)
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[claims_mod.get_claim_repo] = lambda: claim_repo
    client.app.dependency_overrides[slots_mod.get_slot_repo] = lambda: slot_repo
    client.app.dependency_overrides[sitter_mod.get_sitter_repo] = lambda: sit_repo
    client.app.dependency_overrides[moderation_mod.get_moderation_repo] = lambda: mrepo
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: nrepo
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wrepo
    client.app.dependency_overrides[payments_mod.get_payment_repo] = lambda: prepo
    wire_images_repo(client)

    def fake(token: str) -> dict:
        if token == "good-" + "token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "bob-" + "token":
            return {"uid": "bob"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)
    return client, urepo, lrepo, crepo, claim_repo, sit_repo, slot_repo, mrepo


ALICE = {"Authorization": "Bearer " + "good-" + "token"}
BOB = {"Authorization": "Bearer " + "bob-" + "token"}
OFF = {"credits_enabled": False, "usd_services_enabled": False}
ON = {"credits_enabled": True, "usd_services_enabled": True}


def _profile(client, headers, name):
    r = client.post("/v1/users", json={"display_name": name,
                                       "age_attestation": True}, headers=headers)
    assert r.status_code == 200, r.text


def _listing_payload(**kw):
    base = {
        "type": "harvest",
        "photos": ["https://example.com/t.jpg"],
        "variety": "Cherokee Purple tomato",
        "quantity": 10,
        "unit": "kg",
        "credit_cost": 0,
        "spray_disclosure": "unsprayed",
        "status": "live",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }
    base.update(kw)
    return base


def _completed_free_exchange(client, lid):
    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 200, r.text
    for headers in (BOB, ALICE):
        r = client.post("/v1/exchange/confirm",
                        json={"listing_id": lid}, headers=headers)
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


# ---------------------------------------------------------------------------
# HIGH 1: zero legs after an off -> on flip
# ---------------------------------------------------------------------------

def test_flip_on_free_claim_accept_and_cancel_post_no_legs(mem, flip):
    client, _, _, crepo, *_ = mem
    flip(**OFF)
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    r = client.post("/v1/listings", json=_listing_payload(credit_cost=0),
                    headers=ALICE)
    lid = r.json()["id"]
    r = client.post(f"/v1/listings/{lid}/claims", json={
        "quantity": 2, "pickupStartMs": 1_800_000_000_000,
        "pickupEndMs": 1_800_003_600_000}, headers=BOB)
    assert r.status_code == 200, r.text
    claim_id = r.json()["claim"]["id"]

    flip(**ON)  # credits return AFTER the free claim was made
    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["claim"]["status"] == "accepted"
    # No zero-delta legs, no starter lots (profiles predate the flip).
    assert crepo.entries("alice") == []
    assert crepo.entries("bob") == []
    assert crepo.balance("alice") == 0
    assert crepo.balance("bob") == 0

    # Cancel of that accept: no forward leg exists, so nothing unwinds.
    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=BOB)
    assert r.status_code == 200, r.text
    assert crepo.entries("alice") == []
    assert crepo.entries("bob") == []


def test_flip_on_free_exchange_confirm_completes(mem, flip):
    client, _, _, crepo, *_ = mem
    flip(**OFF)
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    r = client.post("/v1/listings", json=_listing_payload(
        credit_cost=0, type="seedling", quantity=4, unit="starts"), headers=ALICE)
    lid = r.json()["id"]

    flip(**ON)
    _completed_free_exchange(client, lid)
    assert crepo.entries("alice") == []
    assert crepo.entries("bob") == []


def test_flip_on_free_slot_claim_posts_no_legs(mem, flip):
    client, _, _, crepo, *_ = mem
    flip(**OFF)
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    r = client.post("/v1/listings", json=_listing_payload(
        credit_cost=0, type="tree", quantity=40, unit="lbs",
        variety="Honeycrisp apple"), headers=ALICE)
    tid = r.json()["id"]
    slot = {"dayMs": 1760000000000, "startMs": 1760010000000,
            "endMs": 1760020000000, "maxPickers": 2, "creditCost": 0}
    r = client.post(f"/v1/trees/{tid}/slots", json=slot, headers=ALICE)
    assert r.status_code == 201, r.text
    sid = r.json()["slot"]["id"]

    flip(**ON)
    r = client.post(f"/v1/trees/{tid}/slots/{sid}/claim", headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["claimedCount"] == 1
    assert crepo.entries("alice") == []
    assert crepo.entries("bob") == []


def test_zero_delta_add_entry_is_a_noop():
    """The central guard: no caller can post a zero leg, on either repo."""
    from app import credits as credits_mod

    repo = credits_mod.MemoryCreditRepo()
    assert repo.add_entry("alice", 0, "claim_spend",
                          idempotency_key="k:spend") is None
    assert repo.entries("alice") == []
    # Idempotent replay of a real entry still returns the entry.
    real = repo.add_entry("alice", 5, "exchange_earn", idempotency_key="k:earn")
    assert repo.add_entry("alice", 5, "exchange_earn",
                          idempotency_key="k:earn")["id"] == real["id"]
    assert repo.balance("alice") == 5


# ---------------------------------------------------------------------------
# HIGH 2: cancel unwinds the amount the forward leg actually moved
# ---------------------------------------------------------------------------

def test_cancel_after_reprice_restores_exact_balances(mem, flip):
    client, _, _, crepo, *_ = mem
    flip(**ON)
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    crepo.add_entry("bob", 10, "exchange_earn", ref_id="seed")  # fund bob
    r = client.post("/v1/listings", json=_listing_payload(credit_cost=5),
                    headers=ALICE)
    lid = r.json()["id"]
    r = client.post(f"/v1/listings/{lid}/claims", json={
        "quantity": 1, "pickupStartMs": 1_800_000_000_000,
        "pickupEndMs": 1_800_003_600_000}, headers=BOB)
    claim_id = r.json()["claim"]["id"]
    pre_alice, pre_bob = crepo.balance("alice"), crepo.balance("bob")

    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert crepo.balance("bob") == pre_bob - 5
    assert crepo.balance("alice") == pre_alice + 5

    flip(**OFF)  # credits off; giver re-prices the listing to free
    r = client.patch(f"/v1/listings/{lid}", json={"credit_cost": 0},
                     headers=ALICE)
    assert r.status_code == 200, r.text

    r = client.post(f"/v1/listings/{lid}/claims/cancel", headers=BOB)
    assert r.status_code == 200, r.text
    # Exactly the accepted 5 unwind — not the listing's current 0.
    assert crepo.balance("alice") == pre_alice
    assert crepo.balance("bob") == pre_bob
    reversals = [e for e in crepo._entries if e["reason"] == "claim_reversal"]
    assert sorted(e["delta"] for e in reversals) == [-5, 5]


# ---------------------------------------------------------------------------
# HIGH 3: dispute reversal across the flip
# ---------------------------------------------------------------------------

def _open_dispute(client, lid):
    r = client.post("/v1/disputes", headers=BOB, json={
        "exchangeId": lid, "reason": "wrong-item", "details": "bad swap"})
    assert r.status_code == 200, r.text
    return r.json()["dispute"]["id"]


def test_dispute_reversal_blocked_when_no_legs_and_credits_off(mem, flip, monkeypatch):
    client, _, _, crepo, *_ = mem
    flip(**OFF)
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    r = client.post("/v1/listings", json=_listing_payload(
        credit_cost=0, type="seedling", quantity=4, unit="starts"), headers=ALICE)
    lid = r.json()["id"]
    _completed_free_exchange(client, lid)  # completed while OFF: no legs

    did = _open_dispute(client, lid)
    monkeypatch.setenv("SUPPORT_UIDS", "alice")
    r = client.post(f"/v1/disputes/{did}/resolve", headers=ALICE,
                    json={"outcome": "upheld", "reversalCredits": 2})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "credits_disabled"
    # The dispute is still open and resolvable with a zero reversal.
    r = client.post(f"/v1/disputes/{did}/resolve", headers=ALICE,
                    json={"outcome": "upheld", "reversalCredits": 0})
    assert r.status_code == 200, r.text
    assert crepo.entries("alice") == []
    assert crepo.entries("bob") == []


def test_dispute_reversal_posts_when_legs_exist_despite_flip_off(mem, flip, monkeypatch):
    client, _, _, crepo, *_ = mem
    flip(**ON)
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    r = client.post("/v1/listings", json=_listing_payload(
        credit_cost=2, type="seedling", quantity=4, unit="starts"), headers=ALICE)
    lid = r.json()["id"]
    _completed_free_exchange(client, lid)  # legs posted while ON
    assert any(e["reason"] == "exchange_spend" for e in crepo.entries("bob"))

    did = _open_dispute(client, lid)
    flip(**OFF)
    monkeypatch.setenv("SUPPORT_UIDS", "alice")
    r = client.post(f"/v1/disputes/{did}/resolve", headers=ALICE,
                    json={"outcome": "upheld", "reversalCredits": 1})
    assert r.status_code == 200, r.text
    reversals = [e for e in crepo._entries if e["reason"] == "dispute_reversal"]
    assert len(reversals) == 2
    assert sorted(e["delta"] for e in reversals) == [-1, 1]


# ---------------------------------------------------------------------------
# MEDIUM 3: starter_credits = 0
# ---------------------------------------------------------------------------

def test_starter_credits_zero_grants_nothing(flip):
    from app import credits as credits_mod

    flip(credits_enabled=True, starter_credits=0)
    repo = credits_mod.MemoryCreditRepo()
    credits_mod.ensure_starter_credits("alice", repo)
    assert repo.entries("alice") == []
    assert repo.balance("alice") == 0


# ---------------------------------------------------------------------------
# MEDIUM 1: above-ceiling priced writes while OFF get the enveloped 422
# ---------------------------------------------------------------------------

def test_above_ceiling_priced_writes_enveloped_when_off(mem, flip):
    client, *_ = mem
    # Ceiling 50, credits OFF: 60 passes the model (static bound 100) so
    # the route — not Pydantic — produces the enveloped rejection.
    flip(credits_enabled=False, usd_services_enabled=False, max_listing_cost=50)
    _profile(client, ALICE, "Alice")

    r = client.post("/v1/listings", json=_listing_payload(credit_cost=60),
                    headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "credits_disabled"

    r = client.post("/v1/listings", json=_listing_payload(credit_cost=0),
                    headers=ALICE)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    r = client.patch(f"/v1/listings/{lid}", json={"credit_cost": 60},
                     headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "credits_disabled"

    # Slot create on a tree listing: same enveloped treatment.
    r = client.post("/v1/listings", json=_listing_payload(
        credit_cost=0, type="tree", quantity=40, unit="lbs"), headers=ALICE)
    tid = r.json()["id"]
    slot = {"dayMs": 1760000000000, "startMs": 1760010000000,
            "endMs": 1760020000000, "maxPickers": 2, "creditCost": 60}
    r = client.post(f"/v1/trees/{tid}/slots", json=slot, headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "credits_disabled"


def test_above_ceiling_still_model_rejected_when_on(mem, flip):
    """Enabled mode is untouched: the vertical ceiling stays a model 422."""
    client, *_ = mem
    flip(credits_enabled=True, max_listing_cost=50)
    _profile(client, ALICE, "Alice")
    r = client.post("/v1/listings", json=_listing_payload(credit_cost=60),
                    headers=ALICE)
    assert r.status_code == 422, r.text
    assert "code" not in r.json()  # bare validation error, as before


# ---------------------------------------------------------------------------
# MEDIUM 2: denomination snapshot gates payment intents
# ---------------------------------------------------------------------------

def _seed_priced_booking(sit_repo, rate_unit, subtotal=10000):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    sit_repo.upsert_profile("bob", {
        "rate_amount": 25.0, "rate_unit": "usd",
        "services": ["watering"], "active": True})
    row = {"owner_uid": "alice", "sitter_uid": "bob", "plant_count": 2,
           "dates": [tomorrow], "services": ["watering"]}
    if rate_unit is not None:
        row["rate_unit"] = rate_unit
    req = sit_repo.create_request(row)
    if rate_unit is not None:
        # create_request stores the snapshot only when given one; a NULL
        # snapshot (pre-0044 booking) is seeded by omitting the key.
        assert sit_repo.get_request(req["id"]).get("rate_unit") == rate_unit
    sit_repo.set_request_status(req["id"], "accepted", "requested")
    sit_repo._requests[req["id"]]["subtotal_cents"] = subtotal
    return req["id"]


def test_intent_uses_snapshot_not_current_profile(mem, flip):
    client, urepo, _, _, _, sit_repo, _, _ = mem
    flip(**ON)
    _profile(client, ALICE, "Alice")
    urepo.set_idv_status("alice", "verified")
    # Booked under USD; sitter has since re-priced to credits. The hold
    # must still be gated as USD now that the vertical flipped USD off.
    booking_id = _seed_priced_booking(sit_repo, "usd")
    sit_repo.upsert_profile("bob", {
        "rate_amount": 3, "rate_unit": "credits",
        "services": ["watering"], "active": True})
    flip(credits_enabled=True, usd_services_enabled=False)
    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": booking_id}, headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "usd_services_disabled"


def test_intent_credits_snapshot_ok_after_profile_goes_usd(mem, flip):
    client, urepo, _, _, _, sit_repo, _, _ = mem
    flip(credits_enabled=True, usd_services_enabled=False)
    _profile(client, ALICE, "Alice")
    urepo.set_idv_status("alice", "verified")
    booking_id = _seed_priced_booking(sit_repo, "credits")
    sit_repo.upsert_profile("bob", {
        "rate_amount": 25.0, "rate_unit": "usd",
        "services": ["watering"], "active": True})
    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": booking_id}, headers=ALICE)
    assert r.status_code == 200, r.text


def test_intent_missing_snapshot_positive_amount_fails_closed(mem, flip):
    client, urepo, _, _, _, sit_repo, _, _ = mem
    flip(**ON)
    _profile(client, ALICE, "Alice")
    urepo.set_idv_status("alice", "verified")
    booking_id = _seed_priced_booking(sit_repo, None)  # pre-0044 booking
    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": booking_id}, headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "usd_services_disabled"
    assert "denomination is unknown" in r.json()["message"]


def test_sitting_request_stores_snapshot_and_never_exposes_it(mem, flip):
    client, _, _, _, _, sit_repo, _, _ = mem
    flip(**ON)
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    sit_repo.upsert_profile("bob", {
        "rate_amount": 25.0, "rate_unit": "usd",
        "services": ["watering"], "active": True})
    sit_repo.set_available_dates("bob", [tomorrow])
    r = client.post("/v1/sitting-requests", json={
        "sitter_uid": "bob", "plant_count": 2, "dates": [tomorrow],
        "services": ["watering"]}, headers=ALICE)
    assert r.status_code == 201, r.text
    assert "rate_unit" not in r.json()  # internal only; shape unchanged
    stored = sit_repo.get_request(r.json()["id"])
    assert stored["rate_unit"] == "usd"
