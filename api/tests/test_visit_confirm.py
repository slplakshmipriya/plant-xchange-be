"""Visit confirmation for pick-your-own slot claims (migration 0045).

POST /v1/trees/{id}/slots/{slot_id}/confirm-visit records what the
claimer actually picked on their claim row. Credits settled at claim
time — confirming moves nothing. Only the claimer may confirm; the
PICKUP suspension gate applies exactly as on claim.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import slots as slots_mod
from app.vertical import reset_vertical_cache
from conftest import wire_credit_repo

SLOT_KEYS = {"id", "treeId", "dayMs", "startMs", "endMs", "maxPickers",
             "claimedCount", "creditCost", "cashCents"}


@pytest.fixture(autouse=True)
def _vertical_env(monkeypatch):
    """Isolate vertical selection per test; config loads without a DB."""
    monkeypatch.delenv("VERTICAL_CONFIG_PATH", raising=False)
    monkeypatch.delenv("VERTICAL_ID", raising=False)
    reset_vertical_cache()
    yield
    reset_vertical_cache()


@pytest.fixture()
def mem_slots(client):
    from app import listings as listings_mod
    from app import moderation as moderation_mod
    from app import notify as notify_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod

    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    srepo = slots_mod.MemorySlotRepo()
    wrepo = wantlist_mod.MemoryWantRepo()
    nrepo = notify_mod.MemoryNotificationRepo()
    mrepo = moderation_mod.MemoryModerationRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[slots_mod.get_slot_repo] = lambda: srepo
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wrepo
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: nrepo
    client.app.dependency_overrides[moderation_mod.get_moderation_repo] = lambda: mrepo
    crepo = wire_credit_repo(client)
    return client, urepo, lrepo, srepo, crepo, mrepo


def login_as(monkeypatch, uid: str):
    """Re-point the mock verifier at uid (tests default to alice)."""
    import app.auth as auth_mod

    orig = auth_mod.verify_id_token

    def fake(token: str):
        if token == "good-token":
            return {"uid": uid}
        return orig(token)

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)


def _tree_payload(**kw):
    base = {
        "type": "tree",
        "photos": ["https://example.com/apple.jpg"],
        "variety": "Honeycrisp apple",
        "quantity": 40,
        "unit": "lbs",
        "credit_cost": 1,
        "spray_disclosure": "unsprayed",
        "status": "live",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }
    base.update(kw)
    return base


def _slot_payload(**kw):
    base = {
        "dayMs": 1760000000000,
        "startMs": 1760010000000,
        "endMs": 1760020000000,
        "maxPickers": 2,
        "creditCost": 2,
    }
    base.update(kw)
    return base


def _make_tree(client, auth_headers):
    r = client.post("/v1/listings", json=_tree_payload(), headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _claimed_setup(mem_slots, auth_headers, monkeypatch, credit_cost=2):
    """Alice's tree+slot, bob claimed a spot. Returns (client, tid, slot_id, crepo)."""
    client, urepo, _, _, crepo, _ = mem_slots
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    tid = _make_tree(client, auth_headers)
    r = client.post(f"/v1/trees/{tid}/slots",
                    json=_slot_payload(creditCost=credit_cost), headers=auth_headers)
    assert r.status_code == 201, r.text
    slot_id = r.json()["slot"]["id"]
    crepo.add_entry("bob", 5, "seed", ref_id="t")
    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text
    return client, tid, slot_id, crepo


# ---------------------------------------------------------------- confirm

def test_claimer_confirms_visit(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, crepo = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    srepo = mem_slots[3]
    bob_entries_before = len(crepo.entries("bob"))
    alice_entries_before = len(crepo.entries("alice"))
    bob_balance_before = crepo.balance("bob")
    alice_balance_before = crepo.balance("alice")

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 12.5}, headers=auth_headers)
    assert r.status_code == 200, r.text
    slot = r.json()["slot"]
    assert SLOT_KEYS <= set(slot.keys())
    assert slot["claimed_by_me"] is True
    assert slot["visit_confirmed"] is True
    assert slot["lbs_picked"] == 12.5

    claim = srepo.get_claim(slot_id, "bob")
    assert claim["lbs_picked"] == 12.5
    assert isinstance(claim["visited_at"], int) and claim["visited_at"] > 0

    # Confirming moves NO credits: balances and ledger counts unchanged.
    assert crepo.balance("bob") == bob_balance_before
    assert crepo.balance("alice") == alice_balance_before
    assert len(crepo.entries("bob")) == bob_entries_before
    assert len(crepo.entries("alice")) == alice_entries_before


def test_second_confirm_updates_pick(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    srepo = mem_slots[3]

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 3}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["lbs_picked"] == 3

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 7.25}, headers=auth_headers)
    assert r.status_code == 200, r.text
    slot = r.json()["slot"]
    assert slot["lbs_picked"] == 7.25
    assert slot["visit_confirmed"] is True
    assert srepo.get_claim(slot_id, "bob")["lbs_picked"] == 7.25


def test_non_claimer_confirm_403(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    mem_slots[1].upsert("carol", display_name="Carol")
    login_as(monkeypatch, "carol")

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 5}, headers=auth_headers)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "not_claimed"


def test_owner_without_claim_confirm_403(mem_slots, mock_verify, auth_headers,
                                         monkeypatch):
    client, tid, slot_id, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    login_as(monkeypatch, "alice")  # owner holds no claim on her own slot

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 5}, headers=auth_headers)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "not_claimed"


@pytest.mark.parametrize("body", [
    {"lbs_picked": 0},
    {"lbs_picked": -2},
    {"lbs_picked": 1000.5},
    {"lbs_picked": "lots"},
    {"lbs_picked": None},
    {},
])
def test_confirm_bad_lbs_422(mem_slots, mock_verify, auth_headers, monkeypatch, body):
    client, tid, slot_id, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json=body, headers=auth_headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_lbs"


def test_confirm_huge_lbs_422_not_500(mem_slots, mock_verify, auth_headers,
                                      monkeypatch):
    # A 400-digit JSON number parses as a Python bigint; float() on it
    # raises OverflowError. Must surface as 422 invalid_lbs, not a 500.
    client, tid, slot_id, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 10**400}, headers=auth_headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_lbs"


def test_confirm_lbs_boundary_1000_ok(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 1000}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["lbs_picked"] == 1000


def test_confirm_unknown_slot_404(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, _, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    r = client.post(f"/v1/trees/{tid}/slots/nope/confirm-visit",
                    json={"lbs_picked": 5}, headers=auth_headers)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "slot_not_found"


def test_confirm_slot_under_other_tree_404(mem_slots, mock_verify, auth_headers,
                                           monkeypatch):
    client, tid, slot_id, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    login_as(monkeypatch, "alice")
    tid2 = _make_tree(client, auth_headers)
    login_as(monkeypatch, "bob")

    r = client.post(f"/v1/trees/{tid2}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 5}, headers=auth_headers)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "slot_not_found"


def test_confirm_suspended_claimer_403(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    mrepo = mem_slots[5]
    mrepo.add_enforcement(
        "bob", slots_mod.PICKUP_PILLAR, "suspension",
        datetime.now(timezone.utc) + timedelta(days=30), "no-show abuse")

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 5}, headers=auth_headers)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "suspended"


# ---------------------------------------------------------------- list view

def test_list_slots_annotates_only_callers_claim(mem_slots, mock_verify,
                                                 auth_headers, monkeypatch):
    client, tid, slot_id, _ = _claimed_setup(mem_slots, auth_headers, monkeypatch)
    mem_slots[1].upsert("carol", display_name="Carol")

    # Claimer, before confirming: annotations present, not yet visited.
    r = client.get(f"/v1/trees/{tid}/slots", headers=auth_headers)
    assert r.status_code == 200, r.text
    slot = next(s for s in r.json()["slots"] if s["id"] == slot_id)
    assert slot["claimed_by_me"] is True
    assert slot["visit_confirmed"] is False
    assert "lbs_picked" not in slot

    # After confirming: visited + lbs visible to the claimer.
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 9}, headers=auth_headers)
    assert r.status_code == 200, r.text
    r = client.get(f"/v1/trees/{tid}/slots", headers=auth_headers)
    slot = next(s for s in r.json()["slots"] if s["id"] == slot_id)
    assert slot["visit_confirmed"] is True
    assert slot["lbs_picked"] == 9

    # Third party and owner see the untouched public shape — no claim info.
    for uid in ("carol", "alice"):
        login_as(monkeypatch, uid)
        r = client.get(f"/v1/trees/{tid}/slots", headers=auth_headers)
        assert r.status_code == 200, r.text
        slot = next(s for s in r.json()["slots"] if s["id"] == slot_id)
        assert set(slot.keys()) == SLOT_KEYS


# ---------------------------------------------------------------- credits off

@pytest.fixture()
def credits_off(mem_slots, monkeypatch, tmp_path):
    p = tmp_path / "freeville.json"
    p.write_text(json.dumps({
        "vertical_id": "freeville",
        "economy": {"credits_enabled": False},
    }), encoding="utf-8")
    monkeypatch.setenv("VERTICAL_CONFIG_PATH", str(p))
    reset_vertical_cache()
    return mem_slots


def test_confirm_works_with_credits_disabled(credits_off, mock_verify,
                                             auth_headers, monkeypatch):
    client, urepo, _, srepo, crepo, _ = credits_off
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    r = client.post("/v1/listings", json=_tree_payload(credit_cost=0),
                    headers=auth_headers)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = client.post(f"/v1/trees/{tid}/slots",
                    json=_slot_payload(creditCost=0), headers=auth_headers)
    assert r.status_code == 201, r.text
    slot_id = r.json()["slot"]["id"]

    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/confirm-visit",
                    json={"lbs_picked": 4.5}, headers=auth_headers)
    assert r.status_code == 200, r.text
    slot = r.json()["slot"]
    assert slot["visit_confirmed"] is True
    assert slot["lbs_picked"] == 4.5
    # Nothing ever entered the ledger in this vertical.
    assert crepo.entries("bob") == []
    assert crepo.entries("alice") == []
