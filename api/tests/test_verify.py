"""API-011: phone verification binds the Firebase-verified phone claim to uid."""
from __future__ import annotations


def test_verify_requires_auth(client):
    r = client.post("/v1/auth/verify")
    assert r.status_code == 401


def test_verify_rejects_token_without_phone_claim(mem_users, mock_verify):
    client, _ = mem_users
    r = client.post(
        "/v1/auth/verify", headers={"Authorization": "Bearer nophone-token"}
    )
    assert r.status_code == 400
    assert r.json()["code"] == "phone_verification_required"


def test_verify_ignores_device_fingerprint_header(mem_users, mock_verify, auth_headers):
    """M17: the X-Device-Fingerprint header is no longer collected — it is
    deliberately ignored when sent (nothing ever used it)."""
    from app.verify import phone_hash

    client, repo = mem_users
    r = client.post(
        "/v1/auth/verify",
        headers={**auth_headers, "X-Device-Fingerprint": "fp-123"},
    )
    assert r.status_code == 200
    assert r.json() == {"uid": "alice", "verified": True}
    row = repo.get("alice")
    assert row["phone_hash"] == phone_hash("+15551234567")
    assert "device_fingerprint" not in row


def test_verify_without_fingerprint_header_ok(mem_users, mock_verify, auth_headers):
    client, repo = mem_users
    r = client.post("/v1/auth/verify", headers=auth_headers)
    assert r.status_code == 200
    assert "device_fingerprint" not in repo.get("alice")


def test_verify_rejects_duplicate_phone_safely(mem_users, mock_verify, auth_headers):
    from app.verify import phone_hash

    client, repo = mem_users
    # bob already claimed this phone number through the verify flow
    repo.upsert("bob", phone_hash=phone_hash("+15551234567"))
    r = client.post("/v1/auth/verify", headers=auth_headers)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "phone_in_use"
    assert "bob" not in body["message"]  # safe error: no uid leaked


def test_verify_idempotent_for_same_user(mem_users, mock_verify, auth_headers):
    client, repo = mem_users
    assert client.post("/v1/auth/verify", headers=auth_headers).status_code == 200
    assert client.post("/v1/auth/verify", headers=auth_headers).status_code == 200
    assert repo.get("alice")["phone_hash"] is not None


def test_phone_hash_is_stable_and_domain_separated():
    from app.verify import phone_hash

    assert phone_hash("+15551234567") == phone_hash("+15551234567")
    assert phone_hash("+15551234567") != phone_hash("+15551234568")
    assert len(phone_hash("+15551234567")) == 64


def test_phone_hash_keyed_hmac(monkeypatch):
    """M2: with PHONE_HASH_SECRET set the stored hash is HMAC-SHA-256 —
    a DB leak alone no longer suffices to brute-force phone numbers."""
    from app.verify import legacy_phone_hash, phone_hash

    monkeypatch.setenv("PHONE_HASH_SECRET", "unit-test-secret")
    h = phone_hash("+15551234567")
    assert h != legacy_phone_hash("+15551234567")
    assert len(h) == 64
    assert h == phone_hash("+15551234567")  # deterministic
    monkeypatch.setenv("PHONE_HASH_SECRET", "another-secret")
    assert phone_hash("+15551234567") != h  # keyed


def test_verify_upgrades_legacy_row(mem_users, mock_verify, auth_headers, monkeypatch):
    """M2: a row bound under the legacy hash is rewritten to the keyed
    hash when its owner verifies again — lazy, zero-downtime migration."""
    from app.verify import legacy_phone_hash, phone_hash

    client, repo = mem_users
    repo.upsert("alice", phone_hash=legacy_phone_hash("+15551234567"))
    monkeypatch.setenv("PHONE_HASH_SECRET", "unit-test-secret")

    r = client.post("/v1/auth/verify", headers=auth_headers)
    assert r.status_code == 200, r.text
    row = repo.get("alice")
    assert row["phone_hash"] == phone_hash("+15551234567")
    assert row["phone_hash"] != legacy_phone_hash("+15551234567")
    assert repo.get_by_phone_hash(legacy_phone_hash("+15551234567")) is None


def test_verify_duplicate_phone_legacy_row_409(mem_users, mock_verify,
                                               auth_headers, monkeypatch):
    """M2 dual-read: a number claimed under the LEGACY hash by another
    uid still conflicts after the keyed hash ships."""
    from app.verify import legacy_phone_hash

    client, repo = mem_users
    repo.upsert("bob", phone_hash=legacy_phone_hash("+15551234567"))
    monkeypatch.setenv("PHONE_HASH_SECRET", "unit-test-secret")

    r = client.post("/v1/auth/verify", headers=auth_headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "phone_in_use"
    assert repo.get("alice") is None or \
        repo.get("alice").get("phone_hash") != legacy_phone_hash("+15551234567")


def test_validate_phone_config_fails_closed(monkeypatch):
    """M2: deployed environments refuse to boot without the HMAC key."""
    import pytest

    from app.config import get_settings, validate_phone_config

    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.delenv("PHONE_HASH_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        validate_phone_config(get_settings())

    monkeypatch.setenv("PHONE_HASH_SECRET", "unit-test-secret")
    validate_phone_config(get_settings())  # no raise

    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.delenv("PHONE_HASH_SECRET", raising=False)
    validate_phone_config(get_settings())  # dev keeps the legacy fallback
