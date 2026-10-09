"""Economy switches: economy.credits_enabled / usd_services_enabled.

Both default ON (garden behavior byte-identical). Off positions run a
vertical as free/plain exchange (no credit medium) and/or credits-only
services (no dollar pricing). Enforcement is enveloped at the routes
(422 credits_disabled / usd_services_disabled); the ledger, the earn
cap, and TRANSFER_REASONS are never weakened — the switches simply stop
posting entries.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.vertical import get_vertical, reset_vertical_cache


@pytest.fixture(autouse=True)
def _vertical_env(monkeypatch):
    """Isolate vertical selection per test; config loads without a DB."""
    monkeypatch.delenv("VERTICAL_CONFIG_PATH", raising=False)
    monkeypatch.delenv("VERTICAL_ID", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_vertical_cache()
    yield
    reset_vertical_cache()


@pytest.fixture()
def switches_off(monkeypatch, tmp_path):
    """A vertical with both economy switches off (config-path fixture)."""
    p = tmp_path / "freeville.json"
    p.write_text(json.dumps({
        "vertical_id": "freeville",
        "economy": {"credits_enabled": False, "usd_services_enabled": False},
    }), encoding="utf-8")
    monkeypatch.setenv("VERTICAL_CONFIG_PATH", str(p))
    reset_vertical_cache()
    v = get_vertical()
    assert v.economy.credits_enabled is False
    assert v.economy.usd_services_enabled is False
    return v


ALICE = {"Authorization": "Bearer " + "good-" + "token"}
BOB = {"Authorization": "Bearer " + "bob-" + "token"}


@pytest.fixture()
def mem_switch(client, monkeypatch):
    """Full in-memory stack for switch flows. Returns
    (client, urepo, lrepo, crepo, claim_repo, sit_repo, slot_repo)."""
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
    return client, urepo, lrepo, crepo, claim_repo, sit_repo, slot_repo


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


# ---------------------------------------------------------------------------
# /v1/config advertises the switches
# ---------------------------------------------------------------------------

def test_config_payload_flags_default_on(client):
    body = client.get("/v1/config").json()
    assert body["economy"]["credits_enabled"] is True
    assert body["economy"]["usd_services_enabled"] is True


def test_config_payload_flags_off(client, switches_off):
    body = client.get("/v1/config").json()
    assert body["vertical_id"] == "freeville"
    assert body["economy"]["credits_enabled"] is False
    assert body["economy"]["usd_services_enabled"] is False


# ---------------------------------------------------------------------------
# credits_enabled = False
# ---------------------------------------------------------------------------

def test_credits_off_listing_create_priced_rejected_free_ok(mem_switch, switches_off):
    client, *_ = mem_switch
    _profile(client, ALICE, "Alice")

    r = client.post("/v1/listings", json=_listing_payload(credit_cost=1),
                    headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "credits_disabled"

    r = client.post("/v1/listings", json=_listing_payload(credit_cost=0),
                    headers=ALICE)
    assert r.status_code == 201, r.text


def test_credits_off_listing_patch_to_priced_rejected(mem_switch, switches_off):
    client, *_ = mem_switch
    _profile(client, ALICE, "Alice")
    r = client.post("/v1/listings", json=_listing_payload(credit_cost=0),
                    headers=ALICE)
    lid = r.json()["id"]

    r = client.patch(f"/v1/listings/{lid}", json={"credit_cost": 3},
                     headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "credits_disabled"

    r = client.patch(f"/v1/listings/{lid}", json={"credit_cost": 0},
                     headers=ALICE)
    assert r.status_code == 200, r.text


def test_credits_on_free_listing_still_rejected(mem_switch):
    """The 1-credit floor survives while credits are enabled (the static
    Field is ge=0 now, so this pins the model-validator floor)."""
    client, *_ = mem_switch
    _profile(client, ALICE, "Alice")
    r = client.post("/v1/listings", json=_listing_payload(credit_cost=0),
                    headers=ALICE)
    assert r.status_code == 422, r.text


def test_starter_grant_skipped_when_credits_off(switches_off):
    from app import credits as credits_mod

    repo = credits_mod.MemoryCreditRepo()
    credits_mod.ensure_starter_credits("alice", repo)
    assert repo.entries("alice") == []
    assert repo.balance("alice") == 0


def test_credits_off_claim_accept_moves_nothing(mem_switch, switches_off):
    client, _, _, crepo, _, _, _ = mem_switch
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

    r = client.post(f"/v1/listings/{lid}/claims/accept",
                    json={"claimId": claim_id}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["claim"]["status"] == "accepted"
    # No starter, no spend, no earn — the ledger was never touched.
    assert crepo.entries("alice") == []
    assert crepo.entries("bob") == []
    assert crepo.balance("alice") == 0
    assert crepo.balance("bob") == 0


def test_credits_off_exchange_confirm_moves_nothing(mem_switch, switches_off):
    client, _, _, crepo, _, _, _ = mem_switch
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    r = client.post("/v1/listings", json=_listing_payload(
        credit_cost=0, type="seedling", quantity=4, unit="starts"), headers=ALICE)
    lid = r.json()["id"]

    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 200, r.text

    for headers in (BOB, ALICE):
        r = client.post("/v1/exchange/confirm",
                        json={"listing_id": lid, "idempotency_key": "k1"},
                        headers=headers)
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert crepo.entries("alice") == []
    assert crepo.entries("bob") == []


def test_credits_off_slot_create_and_claim(mem_switch, switches_off):
    client, _, _, crepo, _, _, _ = mem_switch
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    r = client.post("/v1/listings", json=_listing_payload(
        credit_cost=0, type="tree", quantity=40, unit="lbs",
        variety="Honeycrisp apple"), headers=ALICE)
    tid = r.json()["id"]

    slot = {"dayMs": 1760000000000, "startMs": 1760010000000,
            "endMs": 1760020000000, "maxPickers": 2}
    r = client.post(f"/v1/trees/{tid}/slots",
                    json={**slot, "creditCost": 2}, headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "credits_disabled"

    r = client.post(f"/v1/trees/{tid}/slots",
                    json={**slot, "creditCost": 0}, headers=ALICE)
    assert r.status_code == 201, r.text
    sid = r.json()["slot"]["id"]

    r = client.post(f"/v1/trees/{tid}/slots/{sid}/claim", headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["claimedCount"] == 1
    assert crepo.balance("alice") == 0
    assert crepo.balance("bob") == 0
    assert crepo.entries("alice") == []
    assert crepo.entries("bob") == []


# ---------------------------------------------------------------------------
# usd_services_enabled = False
# ---------------------------------------------------------------------------

def test_usd_off_sitter_profile_usd_rejected_credits_ok(mem_switch, switches_off):
    client, *_ = mem_switch
    _profile(client, BOB, "Bob")

    r = client.put("/v1/sitters/me",
                   json={"rate_amount": 25.0, "rate_unit": "usd"}, headers=BOB)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "usd_services_disabled"

    r = client.put("/v1/sitters/me",
                   json={"rate_amount": 3, "rate_unit": "credits"}, headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["rate_unit"] == "credits"


def _seed_usd_sitter(sit_repo, uid="bob"):
    """A sitter priced in USD before the switch flipped (repo-level seed
    bypasses the profile route gate, exactly like a pre-existing row)."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    sit_repo.upsert_profile(uid, {
        "rate_amount": 25.0, "rate_unit": "usd",
        "services": ["watering"], "active": True})
    sit_repo.set_available_dates(uid, [tomorrow])
    return tomorrow


def test_usd_off_booking_existing_usd_sitter_rejected(mem_switch, switches_off):
    client, _, _, _, _, sit_repo, _ = mem_switch
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    tomorrow = _seed_usd_sitter(sit_repo)

    r = client.post("/v1/sitting-requests", json={
        "sitter_uid": "bob", "plant_count": 2, "dates": [tomorrow],
        "services": ["watering"]}, headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "usd_services_disabled"


def test_usd_off_quote_function_gates(switches_off):
    from app import payments as payments_mod

    with pytest.raises(payments_mod.UsdServicesDisabledError):
        payments_mod.quote_booking({"subtotal_cents": 10000, "rate_unit": "usd"})
    # Credit-denominated quotes are unaffected.
    q = payments_mod.quote_booking({"subtotal_cents": 10000, "rate_unit": "credits"})
    assert q["fee_cents"] == 1800
    # Unit-less quotes (the historic call shape) are unaffected.
    assert payments_mod.quote_booking({"subtotal_cents": 10000})["fee_cents"] == 1800


def test_usd_on_quote_function_allows_usd():
    from app import payments as payments_mod

    q = payments_mod.quote_booking({"subtotal_cents": 10000, "rate_unit": "usd"})
    assert q["fee_cents"] == 1800


def _seed_accepted_booking(sit_repo, rate_unit):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    sit_repo.upsert_profile("bob", {
        "rate_amount": 25.0 if rate_unit == "usd" else 3,
        "rate_unit": rate_unit, "services": ["watering"], "active": True})
    req = sit_repo.create_request({
        "owner_uid": "alice", "sitter_uid": "bob", "plant_count": 2,
        "dates": [tomorrow], "services": ["watering"],
        "rate_unit": rate_unit})
    sit_repo.set_request_status(req["id"], "accepted", "requested")
    sit_repo._requests[req["id"]]["subtotal_cents"] = 10000
    return req["id"]


def test_usd_off_payment_intent_rejected(mem_switch, switches_off):
    client, urepo, _, _, _, sit_repo, _ = mem_switch
    _profile(client, ALICE, "Alice")
    urepo.set_idv_status("alice", "verified")
    booking_id = _seed_accepted_booking(sit_repo, "usd")

    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": booking_id}, headers=ALICE)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "usd_services_disabled"


def test_usd_off_payment_intent_credits_booking_ok(mem_switch, switches_off):
    client, urepo, _, _, _, sit_repo, _ = mem_switch
    _profile(client, ALICE, "Alice")
    urepo.set_idv_status("alice", "verified")
    booking_id = _seed_accepted_booking(sit_repo, "credits")

    r = client.post("/v1/payments/sitting-intent",
                    json={"bookingId": booking_id}, headers=ALICE)
    assert r.status_code == 200, r.text
