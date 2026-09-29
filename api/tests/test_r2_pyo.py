"""R2 pick-your-own slots: tree slot CRUD + credit-moving claims."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import slots as slots_mod
from conftest import wire_credit_repo

SLOT_KEYS = {"id", "treeId", "dayMs", "startMs", "endMs", "maxPickers",
             "claimedCount", "creditCost", "cashCents"}


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


# ---------------------------------------------------------------- create

def test_owner_creates_slot(mem_slots, mock_verify, auth_headers):
    client, urepo, _, _, _, _ = mem_slots
    urepo.upsert("alice", display_name="Alice")
    tid = _make_tree(client, auth_headers)

    r = client.post(f"/v1/trees/{tid}/slots",
                    json={**_slot_payload(), "cashCents": 500}, headers=auth_headers)
    assert r.status_code == 201, r.text
    slot = r.json()["slot"]
    assert set(slot.keys()) == SLOT_KEYS, f"wire shape mismatch: {sorted(slot.keys())}"
    assert slot["dayMs"] == 1760000000000
    assert slot["startMs"] == 1760010000000
    assert slot["endMs"] == 1760020000000
    assert slot["maxPickers"] == 2
    assert slot["claimedCount"] == 0
    assert slot["creditCost"] == 2
    assert slot["cashCents"] == 500


def test_create_slot_defaults_cash_cents_to_null(mem_slots, mock_verify, auth_headers):
    client, urepo, _, _, _, _ = mem_slots
    urepo.upsert("alice", display_name="Alice")
    tid = _make_tree(client, auth_headers)

    r = client.post(f"/v1/trees/{tid}/slots", json=_slot_payload(), headers=auth_headers)
    assert r.status_code == 201, r.text
    assert r.json()["slot"]["cashCents"] is None


def test_create_slot_non_owner_403(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, urepo, _, _, _, _ = mem_slots
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    tid = _make_tree(client, auth_headers)

    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots", json=_slot_payload(), headers=auth_headers)
    assert r.status_code == 403, r.text


def test_create_slot_missing_tree_404(mem_slots, mock_verify, auth_headers):
    client, _, _, _, _, _ = mem_slots
    r = client.post("/v1/trees/nope/slots", json=_slot_payload(), headers=auth_headers)
    assert r.status_code == 404, r.text


def test_create_slot_non_tree_listing_422(mem_slots, mock_verify, auth_headers):
    client, urepo, _, _, _, _ = mem_slots
    urepo.upsert("alice", display_name="Alice")
    r = client.post("/v1/listings",
                    json={**_tree_payload(), "type": "seedling", "variety": "tomato"},
                    headers=auth_headers)
    sid = r.json()["id"]
    r = client.post(f"/v1/trees/{sid}/slots", json=_slot_payload(), headers=auth_headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "not_tree_listing"


def test_create_slot_validation(mem_slots, mock_verify, auth_headers):
    client, urepo, _, _, _, _ = mem_slots
    urepo.upsert("alice", display_name="Alice")
    tid = _make_tree(client, auth_headers)

    # startMs >= endMs
    r = client.post(f"/v1/trees/{tid}/slots",
                    json=_slot_payload(startMs=1760020000000, endMs=1760010000000),
                    headers=auth_headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_window"

    # maxPickers < 1
    r = client.post(f"/v1/trees/{tid}/slots",
                    json=_slot_payload(maxPickers=0), headers=auth_headers)
    assert r.status_code == 422, r.text

    # creditCost < 0
    r = client.post(f"/v1/trees/{tid}/slots",
                    json=_slot_payload(creditCost=-1), headers=auth_headers)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- list

def test_list_slots_wire_shape(mem_slots, mock_verify, auth_headers):
    client, urepo, _, _, _, _ = mem_slots
    urepo.upsert("alice", display_name="Alice")
    tid = _make_tree(client, auth_headers)

    r = client.get(f"/v1/trees/{tid}/slots", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"slots": []}

    for cost in (1, 3):
        r = client.post(f"/v1/trees/{tid}/slots",
                        json=_slot_payload(creditCost=cost), headers=auth_headers)
        assert r.status_code == 201, r.text

    r = client.get(f"/v1/trees/{tid}/slots", headers=auth_headers)
    body = r.json()
    assert set(body.keys()) == {"slots"}, f"wire shape mismatch: {sorted(body.keys())}"
    assert len(body["slots"]) == 2
    for slot in body["slots"]:
        assert set(slot.keys()) == SLOT_KEYS, f"slot shape mismatch: {sorted(slot.keys())}"
    assert sorted(s["creditCost"] for s in body["slots"]) == [1, 3]


def test_list_slots_missing_tree_404(mem_slots, mock_verify, auth_headers):
    client, _, _, _, _, _ = mem_slots
    r = client.get("/v1/trees/nope/slots", headers=auth_headers)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------- claim

def _claim_setup(mem_slots, mock_verify, auth_headers, credit_cost=2, max_pickers=2):
    """Alice's tree with one slot; bob seeded with credits. Returns ids."""
    client, urepo, _, _, crepo, _ = mem_slots
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    tid = _make_tree(client, auth_headers)
    r = client.post(f"/v1/trees/{tid}/slots",
                    json=_slot_payload(creditCost=credit_cost, maxPickers=max_pickers),
                    headers=auth_headers)
    assert r.status_code == 201, r.text
    slot_id = r.json()["slot"]["id"]
    crepo.add_entry("bob", 5, "seed", ref_id="t")
    return client, tid, slot_id, crepo


def test_claim_moves_credits_ownerward(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, crepo = _claim_setup(mem_slots, mock_verify, auth_headers)
    login_as(monkeypatch, "bob")

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text
    slot = r.json()["slot"]
    assert set(slot.keys()) == SLOT_KEYS
    assert slot["claimedCount"] == 1

    assert crepo.balance("bob") == 5 - 2
    assert crepo.balance("alice") == 2
    reasons = [e["reason"] for e in crepo.entries("bob")]
    assert "slot_spend" in reasons
    reasons = [e["reason"] for e in crepo.entries("alice")]
    assert "slot_earn" in reasons


def test_claim_owner_cannot_claim_own_slot(mem_slots, mock_verify, auth_headers):
    client, tid, slot_id, _ = _claim_setup(mem_slots, mock_verify, auth_headers)

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "cannot_claim_own"


def test_claim_full_slot_409(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, crepo = _claim_setup(
        mem_slots, mock_verify, auth_headers, max_pickers=1)
    urepo = mem_slots[1]
    urepo.upsert("carol", display_name="Carol")
    crepo.add_entry("carol", 5, "seed", ref_id="t")
    # M20b: solo slots require verified ID — verify both pickers first.
    urepo.set_idv_status("bob", "verified")
    urepo.set_idv_status("carol", "verified")

    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["claimedCount"] == 1

    login_as(monkeypatch, "carol")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "slot_full"
    # Carol was not charged.
    assert crepo.balance("carol") == 5


def test_claim_insufficient_credits_422(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, crepo = _claim_setup(
        mem_slots, mock_verify, auth_headers, credit_cost=3)
    # Bob has 5 from _claim_setup; drain to 2 < 3.
    crepo.add_entry("bob", -3, "drain", ref_id="t")
    assert crepo.balance("bob") == 2

    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "insufficient_credits"
    assert crepo.balance("bob") == 2  # unchanged


def test_claim_unknown_slot_404(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, _, _ = _claim_setup(mem_slots, mock_verify, auth_headers)
    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots/nope/claim", headers=auth_headers)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "slot_not_found"


def test_claim_missing_tree_404(mem_slots, mock_verify, auth_headers):
    client, _, _, _, _, _ = mem_slots
    r = client.post("/v1/trees/nope/slots/nope/claim", headers=auth_headers)
    assert r.status_code == 404, r.text


def test_claim_suspended_picker_403(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, _ = _claim_setup(mem_slots, mock_verify, auth_headers)
    _, _, _, _, _, mrepo = mem_slots
    mrepo.add_enforcement(
        "bob", slots_mod.PICKUP_PILLAR, "suspension",
        datetime.now(timezone.utc) + timedelta(days=90), "no-show abuse")
    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim",
                    headers=auth_headers)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "suspended"


def test_slot_under_different_tree_is_404(mem_slots, mock_verify, auth_headers, monkeypatch):
    client, tid, slot_id, _ = _claim_setup(mem_slots, mock_verify, auth_headers)
    tid2 = _make_tree(client, auth_headers)

    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid2}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "slot_not_found"


def test_claim_idempotent_credit_entries_per_claimer(
        mem_slots, mock_verify, auth_headers, monkeypatch):
    """Retrying the ledger add for the same claimer+slot must not double-post."""
    client, tid, slot_id, crepo = _claim_setup(mem_slots, mock_verify, auth_headers)
    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text

    base = f"slot:{slot_id}:bob"
    prior = crepo.find_by_idempotency_key(f"{base}:spend")
    assert prior is not None
    repeat = crepo.add_entry("bob", -2, "slot_spend", ref_id=slot_id,
                             idempotency_key=f"{base}:spend")
    assert repeat["id"] == prior["id"]
    assert crepo.balance("bob") == 3


# ---------------------------------------------------------------- C5: no repeat claims by the same user

def test_same_user_double_claim_second_is_409(mem_slots, mock_verify, auth_headers,
                                              monkeypatch):
    """Sequential repeat claim: second POST is 409, claimed_count unchanged,
    exactly one spend and one earn posted to the ledger."""
    client, tid, slot_id, crepo = _claim_setup(mem_slots, mock_verify, auth_headers)
    login_as(monkeypatch, "bob")

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["claimedCount"] == 1
    bob_after_first = crepo.balance("bob")
    alice_after_first = crepo.balance("alice")

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "already_claimed"

    # No spot counted twice, no extra charge: exactly one spend + one earn.
    r = client.get(f"/v1/trees/{tid}/slots", headers=auth_headers)
    slot = next(s for s in r.json()["slots"] if s["id"] == slot_id)
    assert slot["claimedCount"] == 1
    assert crepo.balance("bob") == bob_after_first
    assert crepo.balance("alice") == alice_after_first
    bob_spends = [e for e in crepo.entries("bob") if e["reason"] == "slot_spend"]
    alice_earns = [e for e in crepo.entries("alice") if e["reason"] == "slot_earn"]
    assert len(bob_spends) == 1
    assert len(alice_earns) == 1


def test_slot_fills_to_max_then_further_claims_409(mem_slots, mock_verify, auth_headers,
                                                  monkeypatch):
    """max_pickers=2: two distinct claimers succeed, a third claimer gets 409
    slot_full and is not charged; a re-claim by an existing claimer is 409
    already_claimed, not a second spot."""
    client, tid, slot_id, crepo = _claim_setup(
        mem_slots, mock_verify, auth_headers, max_pickers=2)
    urepo = mem_slots[1]
    urepo.upsert("carol", display_name="Carol")
    urepo.upsert("dave", display_name="Dave")
    crepo.add_entry("carol", 5, "seed", ref_id="t")
    crepo.add_entry("dave", 5, "seed", ref_id="t")

    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["claimedCount"] == 1

    login_as(monkeypatch, "carol")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["claimedCount"] == 2

    # Full: a new claimer is rejected without being charged.
    login_as(monkeypatch, "dave")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "slot_full"
    assert crepo.balance("dave") == 5

    # Repeat claim by an existing claimer is already_claimed, not a new spot.
    login_as(monkeypatch, "bob")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "already_claimed"

    r = client.get(f"/v1/trees/{tid}/slots", headers=auth_headers)
    slot = next(s for s in r.json()["slots"] if s["id"] == slot_id)
    assert slot["claimedCount"] == 2
    assert crepo.balance("bob") == 3
    assert crepo.balance("carol") == 3
    assert crepo.balance("dave") == 5


# ---------------------------------------------------------------------------
# w2-commerce fixes: H11 (double-spend), M20b (solo-slot IDV)
# ---------------------------------------------------------------------------

def test_solo_slot_requires_verified_idv(mem_slots, mock_verify, auth_headers, monkeypatch):
    """M20b: PRD requires IDV for solo pick-your-own access — an unverified
    picker gets 403 idv_required; a verified one goes through."""
    client, tid, slot_id, crepo = _claim_setup(
        mem_slots, mock_verify, auth_headers, max_pickers=1)
    login_as(monkeypatch, "bob")

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "idv_required"
    assert crepo.balance("bob") == 5  # not charged

    mem_slots[1].set_idv_status("bob", "verified")
    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["slot"]["claimedCount"] == 1


def test_group_slot_needs_no_idv(mem_slots, mock_verify, auth_headers, monkeypatch):
    """M20b: the IDV gate applies to solo slots only, not group slots."""
    client, tid, slot_id, crepo = _claim_setup(
        mem_slots, mock_verify, auth_headers, max_pickers=3)
    login_as(monkeypatch, "bob")

    r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim", headers=auth_headers)
    assert r.status_code == 200, r.text


def test_concurrent_slot_claims_no_double_spend(mem_slots, mock_verify, auth_headers,
                                                monkeypatch):
    """H11: two concurrent slot claims with balance == cost must serialize —
    exactly one wins (200), the other 422s; the balance never goes negative."""
    import threading

    client, urepo, _, _, crepo, _ = mem_slots
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    tid = _make_tree(client, auth_headers)
    slot_ids = []
    for _ in range(2):
        r = client.post(f"/v1/trees/{tid}/slots",
                        json=_slot_payload(creditCost=2, maxPickers=5),
                        headers=auth_headers)
        assert r.status_code == 201, r.text
        slot_ids.append(r.json()["slot"]["id"])
    crepo.add_entry("bob", 2, "seed", ref_id="t")  # exactly one slot's cost
    login_as(monkeypatch, "bob")

    barrier = threading.Barrier(2)
    results = []

    def claim(slot_id):
        barrier.wait()
        r = client.post(f"/v1/trees/{tid}/slots/{slot_id}/claim",
                        headers=auth_headers)
        results.append(r.status_code)

    threads = [threading.Thread(target=claim, args=(sid,)) for sid in slot_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 422]
    assert crepo.balance("bob") == 0  # spent exactly once, never negative


def test_serialize_spend_memory_lock():
    """H11 unit: the memory-path guard serializes check-then-act per uid —
    two racers, one winner."""
    import threading

    from app.claims import serialize_spend

    class FakeRepo:  # no _conn -> memory path
        pass

    state = {"balance": 2}
    won = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        with serialize_spend("bob", FakeRepo()):
            if state["balance"] < 2:
                return
            state["balance"] -= 2
            won.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(won) == 1
    assert state["balance"] == 0


def test_serialize_spend_pg_path_uses_advisory_lock():
    """H11 unit: the Postgres path takes pg_advisory_lock(hashtext(uid))
    around the critical section and always unlocks, even on error."""
    import pytest

    from app.claims import serialize_spend

    calls = []

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((sql, params))

    class FakeRepo:
        _conn = FakeConn()

    with serialize_spend("bob", FakeRepo()):
        pass
    assert calls[0][0] == "SELECT pg_advisory_lock(hashtext(%s))"
    assert calls[0][1] == ("bob",)
    assert calls[1][0] == "SELECT pg_advisory_unlock(hashtext(%s))"
    assert calls[1][1] == ("bob",)

    calls.clear()
    with pytest.raises(ValueError):
        with serialize_spend("bob", FakeRepo()):
            raise ValueError("boom")
    assert any("pg_advisory_unlock" in sql for sql, _ in calls)
