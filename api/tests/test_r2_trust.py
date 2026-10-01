"""R2 track: trust & safety — reports, strikes/suspensions, disputes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def mem_trust(client, monkeypatch):
    """App with in-memory moderation/listing/user repos + memory credit ledger.
    Fake tokens: good-token -> alice, bob-token -> bob, mallory-token -> mallory.
    """
    from app import listings as listings_mod
    from app import moderation as moderation_mod
    from app import notify as notify_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from app import claims as claims_mod
    from conftest import wire_credit_repo
    from conftest import wire_images_repo
    import app.auth as auth_mod

    mrepo = moderation_mod.MemoryModerationRepo()
    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    wrepo = wantlist_mod.MemoryWantRepo()
    nrepo = notify_mod.MemoryNotificationRepo()
    crepo = wire_credit_repo(client)
    wire_images_repo(client)
    claim_repo = claims_mod.MemoryClaimRepo()
    client.app.dependency_overrides[moderation_mod.get_moderation_repo] = lambda: mrepo
    client.app.dependency_overrides[claims_mod.get_claim_repo] = lambda: claim_repo
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wrepo
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: nrepo

    def fake(token: str) -> dict:
        if token == "good-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "bob-token":
            return {"uid": "bob"}
        if token == "mallory-token":
            return {"uid": "mallory"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)
    return client, mrepo, urepo, lrepo, crepo


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


def _completed_exchange(client, cost=2):
    """Drive a listing through claim + dual confirm. Returns the listing id."""
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    r = client.post("/v1/listings", json=_listing_payload(cost), headers=ALICE)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 200, r.text
    for headers in (ALICE, BOB):
        r = client.post("/v1/exchange/confirm", json={"listing_id": lid}, headers=headers)
        assert r.status_code == 200, r.text
    return lid


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_report_bad_target_type_422(mem_trust):
    client, *_ = mem_trust
    r = client.post("/v1/reports", headers=ALICE,
                    json={"targetType": "PLANET", "targetId": "x",
                          "category": "spam", "details": "bad"})
    assert r.status_code == 422, r.text


def test_report_bad_category_422(mem_trust):
    client, *_ = mem_trust
    r = client.post("/v1/reports", headers=ALICE,
                    json={"targetType": "LISTING", "targetId": "x",
                          "category": "vibes", "details": "bad"})
    assert r.status_code == 422, r.text


def test_report_ok_returns_id(mem_trust):
    client, mrepo, *_ = mem_trust
    r = client.post("/v1/reports", headers=ALICE,
                    json={"targetType": "USER", "targetId": "bob",
                          "category": "safety", "details": "threatening messages"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert rid
    assert len(mrepo._reports) == 1
    stored = mrepo._reports[0]
    assert stored["id"] == rid
    assert stored["reporter_uid"] == "alice"
    assert stored["target_type"] == "USER"
    assert stored["category"] == "safety"


# ---------------------------------------------------------------------------
# Strikes / suspension
# ---------------------------------------------------------------------------


def test_two_verified_strikes_suspend_pillar(mem_trust):
    from app import moderation as mod

    _, mrepo, *_ = mem_trust
    assert mod.get_suspension(mrepo, "bob", "listings") is None
    mod.record_strike(mrepo, "bob", "listings", "spam", True)
    assert mod.get_suspension(mrepo, "bob", "listings") is None
    mod.record_strike(mrepo, "bob", "listings", "spam", True)
    suspension = mod.get_suspension(mrepo, "bob", "listings")
    assert suspension is not None
    assert suspension["type"] == "suspension"
    assert isinstance(suspension["reason"], str) and suspension["reason"]
    # ~90 days out, in epoch ms.
    expected_ms = int((datetime.now(timezone.utc)
                       + timedelta(days=90)).timestamp() * 1000)
    assert abs(suspension["untilMs"] - expected_ms) < 5 * 60 * 1000


def test_unverified_strikes_do_not_count(mem_trust):
    from app import moderation as mod

    _, mrepo, *_ = mem_trust
    mod.record_strike(mrepo, "bob", "listings", "spam", False)
    mod.record_strike(mrepo, "bob", "listings", "spam", False)
    assert mod.get_suspension(mrepo, "bob", "listings") is None


def test_suspension_is_pillar_scoped(mem_trust):
    from app import moderation as mod

    _, mrepo, *_ = mem_trust
    mod.record_strike(mrepo, "bob", "messaging", "spam", True)
    mod.record_strike(mrepo, "bob", "messaging", "spam", True)
    assert mod.get_suspension(mrepo, "bob", "messaging") is not None
    assert mod.get_suspension(mrepo, "bob", "listings") is None


def test_verified_fraud_means_permanent_ban(mem_trust):
    from app import moderation as mod

    _, mrepo, *_ = mem_trust
    mod.record_strike(mrepo, "mallory", "exchange", "fraud", True)
    for pillar in ("exchange", "listings", "messaging"):
        ban = mod.get_suspension(mrepo, "mallory", pillar)
        assert ban is not None, pillar
        assert ban["type"] == "ban"
        assert ban["untilMs"] is None
        assert isinstance(ban["reason"], str) and ban["reason"]


def test_unverified_fraud_does_not_ban(mem_trust):
    from app import moderation as mod

    _, mrepo, *_ = mem_trust
    mod.record_strike(mrepo, "mallory", "exchange", "fraud", False)
    assert mod.get_suspension(mrepo, "mallory", "exchange") is None


def test_ban_outranks_suspension(mem_trust):
    from app import moderation as mod

    _, mrepo, *_ = mem_trust
    mod.record_strike(mrepo, "bob", "listings", "spam", True)
    mod.record_strike(mrepo, "bob", "listings", "spam", True)
    assert mod.get_suspension(mrepo, "bob", "listings")["type"] == "suspension"
    mod.record_strike(mrepo, "bob", "exchange", "fraud", True)
    assert mod.get_suspension(mrepo, "bob", "listings")["type"] == "ban"


# ---------------------------------------------------------------------------
# Disputes
# ---------------------------------------------------------------------------


def test_dispute_requires_completed_exchange(mem_trust):
    client, *_ = mem_trust
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    r = client.post("/v1/listings", json=_listing_payload(2), headers=ALICE)
    lid = r.json()["id"]
    # Live (not completed): 422.
    r = client.post("/v1/disputes", headers=BOB,
                    json={"exchangeId": lid, "reason": "no-show",
                          "details": "giver never showed up"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "exchange_not_completed"
    # Unknown exchange: 404.
    r = client.post("/v1/disputes", headers=BOB,
                    json={"exchangeId": "nope", "reason": "no-show",
                          "details": "giver never showed up"})
    assert r.status_code == 404, r.text


def test_dispute_open_and_shape(mem_trust):
    client, mrepo, *_ = mem_trust
    lid = _completed_exchange(client)
    r = client.post("/v1/disputes", headers=BOB,
                    json={"exchangeId": lid, "reason": "wrong-item",
                          "details": "got basil not tomato"})
    assert r.status_code == 200, r.text
    dispute = r.json()["dispute"]
    assert dispute["status"] == "open"
    assert dispute["exchangeId"] == lid
    assert dispute["reporterUid"] == "bob"
    assert dispute["outcome"] is None
    assert dispute["reversalCredits"] == 0
    assert dispute["resolvedAt"] is None
    assert mrepo.get_dispute(dispute["id"]) is not None


def test_dispute_non_party_403(mem_trust):
    client, *_ = mem_trust
    lid = _completed_exchange(client)
    r = client.post("/v1/disputes", headers=MALLORY,
                    json={"exchangeId": lid, "reason": "x",
                          "details": "not my exchange"})
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "not_a_party"


def _resolve(client, dispute_id, payload, headers):
    return client.post(f"/v1/disputes/{dispute_id}/resolve",
                       json=payload, headers=headers)


def test_dispute_resolve_requires_support(mem_trust, monkeypatch):
    client, *_ = mem_trust
    monkeypatch.delenv("SUPPORT_UIDS", raising=False)
    lid = _completed_exchange(client)
    did = client.post("/v1/disputes", headers=BOB,
                      json={"exchangeId": lid, "reason": "x",
                            "details": "y"}).json()["dispute"]["id"]
    # Default (empty SUPPORT_UIDS): fail closed, even for a party.
    r = _resolve(client, did, {"outcome": "upheld", "reversalCredits": 0}, ALICE)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "forbidden"
    # Negative reversal credits are a validation error.
    monkeypatch.setenv("SUPPORT_UIDS", "alice")
    r = _resolve(client, did, {"outcome": "upheld", "reversalCredits": -1}, ALICE)
    assert r.status_code == 422, r.text


def test_dispute_upheld_reversal_appends_ledger_entries(mem_trust, monkeypatch):
    client, mrepo, _, _, crepo = mem_trust
    monkeypatch.setenv("SUPPORT_UIDS", "alice")
    lid = _completed_exchange(client, cost=2)
    # Original ledger state: claimer spent, giver earned.
    before = {e["reason"] for e in crepo.entries("alice")} | {e["reason"] for e in crepo.entries("bob")}
    assert "exchange_spend" in before and "exchange_earn" in before
    n_before = len(crepo._entries)

    did = client.post("/v1/disputes", headers=BOB,
                      json={"exchangeId": lid, "reason": "wrong-item",
                            "details": "got basil not tomato"}).json()["dispute"]["id"]
    r = _resolve(client, did, {"outcome": "upheld", "reversalCredits": 1}, ALICE)
    assert r.status_code == 200, r.text
    dispute = r.json()["dispute"]
    assert dispute["status"] == "resolved"
    assert dispute["outcome"] == "upheld"
    assert dispute["reversalCredits"] == 1
    assert dispute["resolvedBy"] == "alice"
    assert dispute["resolvedAt"] is not None

    # Original entries untouched; exactly two compensating entries appended.
    assert len(crepo._entries) == n_before + 2
    reversals = [e for e in crepo._entries if e["reason"] == "dispute_reversal"]
    assert len(reversals) == 2
    by_uid = {e["uid"]: e for e in reversals}
    assert by_uid["bob"]["delta"] == 1      # claimer refunded
    assert by_uid["alice"]["delta"] == -1   # giver debited
    assert all(e["ref_id"] == did for e in reversals)
    assert by_uid["bob"]["idempotency_key"] == f"dispute:{did}:reversal:claimer"
    assert by_uid["alice"]["idempotency_key"] == f"dispute:{did}:reversal:owner"
    # No original entry was modified (ids of pre-existing entries all survive).
    original_ids = {e["id"] for e in crepo._entries} - {e["id"] for e in reversals}
    assert len(original_ids) == n_before


def test_dispute_rejected_no_reversal(mem_trust, monkeypatch):
    client, _, _, _, crepo = mem_trust
    monkeypatch.setenv("SUPPORT_UIDS", "alice")
    lid = _completed_exchange(client)
    n_before = len(crepo._entries)
    did = client.post("/v1/disputes", headers=BOB,
                      json={"exchangeId": lid, "reason": "x",
                            "details": "y"}).json()["dispute"]["id"]
    r = _resolve(client, did, {"outcome": "rejected", "reversalCredits": 0}, ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["dispute"]["outcome"] == "rejected"
    assert len(crepo._entries) == n_before


def test_dispute_double_resolve_409(mem_trust, monkeypatch):
    client, *_ = mem_trust
    monkeypatch.setenv("SUPPORT_UIDS", "alice")
    lid = _completed_exchange(client)
    did = client.post("/v1/disputes", headers=BOB,
                      json={"exchangeId": lid, "reason": "x",
                            "details": "y"}).json()["dispute"]["id"]
    r = _resolve(client, did, {"outcome": "rejected", "reversalCredits": 0}, ALICE)
    assert r.status_code == 200, r.text
    r = _resolve(client, did, {"outcome": "rejected", "reversalCredits": 0}, ALICE)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "dispute_already_resolved"


def test_dispute_resolve_unknown_id_404(mem_trust, monkeypatch):
    client, *_ = mem_trust
    monkeypatch.setenv("SUPPORT_UIDS", "alice")
    r = _resolve(client, "no-such-dispute",
                 {"outcome": "rejected", "reversalCredits": 0}, ALICE)
    assert r.status_code == 404, r.text


def test_dispute_reversal_cap_blocked_leaves_dispute_open(mem_trust, monkeypatch):
    # M11: reversals post BEFORE the status flip. If the claimer's earn cap
    # (credits.py is another track's file — no exemption there) blocks the
    # reversal, the dispute stays open (409) instead of resolved-but-unreversed.
    client, mrepo, _, _, crepo = mem_trust
    monkeypatch.setenv("SUPPORT_UIDS", "alice")
    lid = _completed_exchange(client, cost=2)
    # bob (claimer) earns 10 in the window -> the 1-credit reversal exceeds the cap.
    crepo.add_entry("bob", 10, "welcome_bonus", idempotency_key="cap-fill")
    did = client.post("/v1/disputes", headers=BOB,
                      json={"exchangeId": lid, "reason": "wrong-item",
                            "details": "got basil not tomato"}).json()["dispute"]["id"]
    r = _resolve(client, did, {"outcome": "upheld", "reversalCredits": 1}, ALICE)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "dispute_reversal_cap_blocked"
    # Dispute is still open and no reversal was posted (retryable).
    assert mrepo.get_dispute(did)["status"] == "open"
    assert not [e for e in crepo._entries if e["reason"] == "dispute_reversal"]
    # And it can still be resolved with reversalCredits=0.
    r = _resolve(client, did, {"outcome": "rejected", "reversalCredits": 0}, ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["dispute"]["status"] == "resolved"


def test_dispute_resolve_race_loser_409(mem_trust, monkeypatch):
    # M11: the conditional UPDATE means a resolve that finds the dispute
    # already flipped (status != open) returns None -> route 409s. Simulate
    # the loser by pre-resolving at the repo layer.
    from datetime import datetime, timezone

    client, mrepo, *_ = mem_trust
    monkeypatch.setenv("SUPPORT_UIDS", "alice")
    lid = _completed_exchange(client)
    did = client.post("/v1/disputes", headers=BOB,
                      json={"exchangeId": lid, "reason": "x",
                            "details": "y"}).json()["dispute"]["id"]
    mrepo.resolve_dispute(did, "rejected", 0, "alice", datetime.now(timezone.utc))
    r = _resolve(client, did, {"outcome": "upheld", "reversalCredits": 1}, ALICE)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "dispute_already_resolved"
