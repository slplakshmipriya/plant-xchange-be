"""API-010: user profiles — upsert/get/patch, owner scoping, PII rules."""
from __future__ import annotations

import pytest


def test_unauthenticated_profile_write_rejected(mem_users):
    client, _ = mem_users
    r = client.post("/v1/users", json={"display_name": "Alice", "age_attestation": True})
    assert r.status_code == 401


def test_upsert_creates_profile(mem_users, mock_verify, auth_headers):
    client, repo = mem_users
    r = client.post(
        "/v1/users",
        json={"display_name": "Alice", "home_zip": "85281",
              "age_attestation": True},
        headers=auth_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["uid"] == "alice"
    assert body["display_name"] == "Alice"
    assert body["home_zip"] == "85281"
    assert repo.get("alice")["display_name"] == "Alice"
    assert repo.get("alice")["age_attested_at"]  # M20c: attestation recorded


def test_patch_updates_only_sent_fields(mem_users, mock_verify, auth_headers):
    client, _ = mem_users
    client.post("/v1/users", json={"display_name": "Alice", "home_zip": "85281",
                                       "age_attestation": True},
                headers=auth_headers)
    r = client.patch("/v1/users/me", json={"display_name": "Alicia"}, headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "Alicia"
    assert body["home_zip"] == "85281"  # untouched


def test_get_me_404_before_profile_exists(mem_users, mock_verify, auth_headers):
    client, _ = mem_users
    r = client.get("/v1/users/me", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["code"] == "profile_not_found"


def test_invalid_zip_rejected(mem_users, mock_verify, auth_headers):
    client, _ = mem_users
    r = client.post("/v1/users", json={"home_zip": "not-a-zip"}, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_zip"


def test_public_profile_hides_pii_and_zip(mem_users, mock_verify, auth_headers):
    client, repo = mem_users
    repo.upsert("alice", display_name="Alice", home_zip="85281",
                phone_hash="ph")
    # another user views alice's public profile
    r = client.get("/v1/users/alice", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert "home_zip" not in body
    assert "phone_hash" not in body


def test_owner_profile_hides_phone_hash(mem_users, mock_verify, auth_headers):
    client, repo = mem_users
    repo.upsert("alice", display_name="Alice", home_zip="85281",
                phone_hash="ph")
    r = client.get("/v1/users/me", headers=auth_headers)
    body = r.json()
    assert body["home_zip"] == "85281"  # owner sees own zip
    assert "phone_hash" not in body


def test_duplicate_phone_hash_rejected_in_repo():
    from app.users import MemoryUserRepo, PhoneInUseError

    repo = MemoryUserRepo()
    repo.upsert("alice", phone_hash="ph1")
    with pytest.raises(PhoneInUseError):
        repo.upsert("bob", phone_hash="ph1")
    # same uid re-upserting its own hash is fine
    repo.upsert("alice", phone_hash="ph1", display_name="Alice")
    assert repo.get("alice")["display_name"] == "Alice"


def test_public_profile_404_for_unknown_user(mem_users, mock_verify, auth_headers):
    client, _ = mem_users
    r = client.get("/v1/users/nobody", headers=auth_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# C6: account deletion (DELETE /v1/users/me) + data export (GET /v1/users/me/export)
# ---------------------------------------------------------------------------

from datetime import datetime, timezone


@pytest.fixture()
def mock_verify_ab(monkeypatch):
    """verify_id_token for two users: 'alice-token' -> alice, 'bob-token' -> bob."""
    import app.auth as auth_mod

    def fake(token: str) -> dict:
        if token == "alice-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "bob-token":
            return {"uid": "bob", "phone_number": "+15559876543"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)


@pytest.fixture()
def mem_c6(client, mock_verify_ab):
    """All memory repos wired for the C6 tests.

    Overrides the users-local lazy repo deps (see app/users.py — the
    canonical factories live in modules that import users, so users.py
    resolves them through these wrappers). Returns (client, repos dict).
    """
    from app import claims as claims_mod
    from app import credits as credits_mod
    from app import images as images_mod
    from app import listings as listings_mod
    from app import msg as msg_mod
    from app import notify as notify_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod

    repos = {
        "users": users_mod.MemoryUserRepo(),
        "listings": listings_mod.MemoryListingRepo(),
        "claims": claims_mod.MemoryClaimRepo(),
        "want": wantlist_mod.MemoryWantRepo(),
        "notify": notify_mod.MemoryNotificationRepo(),
        "msg": msg_mod.MemoryMessageRepo(),
        "credits": credits_mod.MemoryCreditRepo(),
        "images": images_mod.MemoryStoredImagesRepo(),
    }
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: repos["users"]
    client.app.dependency_overrides[users_mod._listing_repo] = lambda: repos["listings"]
    client.app.dependency_overrides[users_mod._claim_repo] = lambda: repos["claims"]
    client.app.dependency_overrides[users_mod._want_repo] = lambda: repos["want"]
    client.app.dependency_overrides[users_mod._notification_repo] = lambda: repos["notify"]
    client.app.dependency_overrides[users_mod._message_repo] = lambda: repos["msg"]
    client.app.dependency_overrides[users_mod._images_repo] = lambda: repos["images"]
    client.app.dependency_overrides[users_mod._blob_store] = lambda: None
    client.app.dependency_overrides[credits_mod.get_credit_repo] = lambda: repos["credits"]
    return client, repos


ALICE_HEADERS = {"Authorization": "Bearer alice-token"}
BOB_HEADERS = {"Authorization": "Bearer bob-token"}
_SINCE = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _seed_alice_and_bob(client, repos):
    """Two users with data in every export section. Returns ids dict."""
    users, listings, claims, want, notify, msg, credits = (
        repos["users"], repos["listings"], repos["claims"], repos["want"],
        repos["notify"], repos["msg"], repos["credits"],
    )
    client.post("/v1/users", json={"display_name": "Alice", "age_attestation": True}, headers=ALICE_HEADERS)
    client.post("/v1/users", json={"display_name": "Bob", "age_attestation": True}, headers=BOB_HEADERS)

    listings.create({"id": "a1", "owner_uid": "alice", "type": "harvest",
                     "title": "Tomatoes", "status": "live",
                     "geo_lat": 37.4, "geo_lon": -122.1})
    listings.create({"id": "a2", "owner_uid": "alice", "type": "tree",
                     "title": "Fig tree", "status": "live",
                     "geo_lat": None, "geo_lon": None})
    listings.create({"id": "b1", "owner_uid": "bob", "type": "seedling",
                     "title": "Basil", "status": "live",
                     "geo_lat": 37.5, "geo_lon": -122.0})

    claims.create({"id": "c1", "listing_id": "b1", "claimer_uid": "alice", "quantity": 2})
    claims.create({"id": "c2", "listing_id": "a1", "claimer_uid": "bob", "quantity": 1})

    credits.add_entry("alice", -1, "exchange_spend", ref_id="x1")
    credits.add_entry("bob", 2, "exchange_earn", ref_id="x2")

    want.create({"id": "w1", "user_uid": "alice", "variety": "Brandywine tomato",
                 "types": ["harvest"]})
    want.create({"id": "w2", "user_uid": "bob", "variety": "Genovese basil",
                 "types": ["seedling"]})

    listings.log_harvest_event("a1", "alice", 1.5, 8.5)
    listings.log_harvest_event("b1", "bob", 0.5, 4.5)

    notify.set_prefs("alice", {"harvest_alerts": False}, True)
    notify.set_prefs("bob", {"harvest_alerts": True}, False)
    notify.log("alice", "harvest_alerts", "a1", "sent")
    notify.log("bob", "wantlist_matches", "w2", "sent")

    t1 = msg.get_or_create_thread("b1", "alice")   # alice's thread on bob's listing
    t2 = msg.get_or_create_thread("a1", "bob")    # bob's thread on alice's listing
    msg.add_message(t1["id"], "alice", "hi bob")
    msg.add_message(t1["id"], "bob", "hello alice")
    msg.add_message(t2["id"], "bob", "is the fig tree still available?")
    return {"t1": t1["id"], "t2": t2["id"]}


def test_delete_me_requires_auth(mem_c6):
    client, _ = mem_c6
    assert client.delete("/v1/users/me").status_code == 401


def test_delete_me_removes_profile(mem_c6):
    client, repos = mem_c6
    client.post("/v1/users", json={"display_name": "Alice", "age_attestation": True}, headers=ALICE_HEADERS)
    r = client.delete("/v1/users/me", headers=ALICE_HEADERS)
    assert r.status_code == 204
    assert repos["users"].get("alice") is None
    # profile is gone afterwards
    r = client.get("/v1/users/me", headers=ALICE_HEADERS)
    assert r.status_code == 404


def test_delete_me_is_idempotent(mem_c6):
    client, _ = mem_c6
    client.post("/v1/users", json={"display_name": "Alice", "age_attestation": True}, headers=ALICE_HEADERS)
    assert client.delete("/v1/users/me", headers=ALICE_HEADERS).status_code == 204
    assert client.delete("/v1/users/me", headers=ALICE_HEADERS).status_code == 204


def test_delete_cascades_notification_log_and_harvest_events(mem_c6):
    """Memory repos mirror the ON DELETE CASCADE from migration 0024."""
    client, repos = mem_c6
    _seed_alice_and_bob(client, repos)
    notify, listings = repos["notify"], repos["listings"]

    assert notify.has_recent("alice", "harvest_alerts", "a1", _SINCE)
    assert len(listings.list_harvest_events("a1")) == 1

    assert client.delete("/v1/users/me", headers=ALICE_HEADERS).status_code == 204

    # alice's rows are gone ...
    assert not notify.has_recent("alice", "harvest_alerts", "a1", _SINCE)
    assert listings.list_harvest_events("a1") == []
    # ... but bob's rows survive
    assert notify.has_recent("bob", "wantlist_matches", "w2", _SINCE)
    assert len(listings.list_harvest_events("b1")) == 1
    assert listings.list_harvest_events("b1")[0]["recorder_uid"] == "bob"


def test_delete_does_not_affect_other_user(mem_c6):
    client, repos = mem_c6
    _seed_alice_and_bob(client, repos)

    assert client.delete("/v1/users/me", headers=ALICE_HEADERS).status_code == 204

    # bob's profile, listing, want-list entry, credits, and prefs are untouched
    assert repos["users"].get("bob")["display_name"] == "Bob"
    assert [lst["id"] for lst in repos["listings"].list_by_owner("bob")] == ["b1"]
    assert [w["id"] for w in repos["want"].list_for_user("bob")] == ["w2"]
    assert {e["reason"] for e in repos["credits"].entries("bob")} == {"starter", "exchange_earn"}
    assert repos["notify"].get_prefs("bob")["categories"] == {"harvest_alerts": True}
    r = client.get("/v1/users/me", headers=BOB_HEADERS)
    assert r.status_code == 200
    assert r.json()["display_name"] == "Bob"


def test_export_requires_auth(mem_c6):
    client, _ = mem_c6
    assert client.get("/v1/users/me/export").status_code == 401


def test_export_404_without_profile(mem_c6):
    client, _ = mem_c6
    r = client.get("/v1/users/me/export", headers=ALICE_HEADERS)
    assert r.status_code == 404
    assert r.json()["code"] == "profile_not_found"


def test_export_returns_only_own_data(mem_c6):
    client, repos = mem_c6
    ids = _seed_alice_and_bob(client, repos)

    r = client.get("/v1/users/me/export", headers=ALICE_HEADERS)
    assert r.status_code == 200
    body = r.json()

    assert body["uid"] == "alice"
    assert body["exported_at"]
    assert body["profile"]["uid"] == "alice"
    assert body["profile"]["display_name"] == "Alice"
    assert "phone_hash" not in body["profile"]

    assert {lst["id"] for lst in body["listings"]} == {"a1", "a2"}
    by_id = {lst["id"]: lst for lst in body["listings"]}
    assert by_id["a1"]["geo_lat"] == 37.4  # decrypted, not ciphertext
    assert by_id["a1"]["geo_lon"] == -122.1
    assert by_id["a2"]["geo_lat"] is None

    assert [c["id"] for c in body["claims"]] == ["c1"]
    assert all(c["claimer_uid"] == "alice" for c in body["claims"])

    assert {e["reason"] for e in body["credit_ledger"]} == {"starter", "exchange_spend"}
    assert all(e["uid"] == "alice" for e in body["credit_ledger"])

    assert [w["variety"] for w in body["want_list"]] == ["Brandywine tomato"]

    assert len(body["harvest_events"]) == 1
    assert body["harvest_events"][0]["recorder_uid"] == "alice"
    assert body["harvest_events"][0]["listing_id"] == "a1"

    assert body["notification_prefs"]["categories"] == {"harvest_alerts": False}

    assert {t["id"] for t in body["threads"]} == {ids["t1"], ids["t2"]}

    assert len(body["messages_sent"]) == 1
    assert body["messages_sent"][0]["body"] == "hi bob"  # decrypted
    assert body["messages_sent"][0]["thread_id"] == ids["t1"]


def test_export_after_delete_404(mem_c6):
    client, _ = mem_c6
    client.post("/v1/users", json={"display_name": "Alice", "age_attestation": True}, headers=ALICE_HEADERS)
    assert client.delete("/v1/users/me", headers=ALICE_HEADERS).status_code == 204
    r = client.get("/v1/users/me/export", headers=ALICE_HEADERS)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# M20c: age gate — 13+ attestation at onboarding
# ---------------------------------------------------------------------------


def test_profile_create_requires_age_attestation(mem_users, mock_verify, auth_headers):
    client, repo = mem_users
    r = client.post("/v1/users", json={"display_name": "Alice"}, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "age_attestation_required"
    assert repo.get("alice") is None  # gate fires before anything is written


def test_profile_create_rejects_explicit_denial(mem_users, mock_verify, auth_headers):
    client, _ = mem_users
    r = client.post(
        "/v1/users", json={"age_attestation": False}, headers=auth_headers
    )
    assert r.status_code == 422
    assert r.json()["code"] == "age_attestation_required"


def test_age_gate_applies_once_per_user(mem_users, mock_verify, auth_headers):
    client, repo = mem_users
    # onboarding with attestation
    r = client.post(
        "/v1/users", json={"display_name": "Alice", "age_attestation": True},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert repo.get("alice")["age_attested_at"]
    # subsequent writes no longer need the field
    r = client.post("/v1/users", json={"display_name": "Alicia"}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alicia"
    r = client.patch("/v1/users/me", json={"home_zip": "85281"}, headers=auth_headers)
    assert r.status_code == 200


def test_patch_gates_until_attestation_recorded(mem_users, mock_verify, auth_headers):
    client, repo = mem_users
    # row created by /v1/auth/verify (no profile, no attestation)
    repo.upsert("alice", phone_hash="ph")
    r = client.patch("/v1/users/me", json={"display_name": "Alice"}, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "age_attestation_required"
    r = client.patch(
        "/v1/users/me",
        json={"display_name": "Alice", "age_attestation": True},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert repo.get("alice")["age_attested_at"]


# ---------------------------------------------------------------------------
# L1c: upsert column whitelist
# ---------------------------------------------------------------------------


def test_upsert_rejects_unknown_columns_memory_repo():
    from app.users import MemoryUserRepo

    repo = MemoryUserRepo()
    with pytest.raises(TypeError):
        repo.upsert("alice", display_name="Alice", injected_col="x")


def test_upsert_rejects_unknown_columns_postgres_repo():
    from app.users import PostgresUserRepo

    repo = PostgresUserRepo(conn=None)  # whitelist fires before any SQL
    with pytest.raises(TypeError):
        repo.upsert("alice", display_name="Alice", injected_col="x")
