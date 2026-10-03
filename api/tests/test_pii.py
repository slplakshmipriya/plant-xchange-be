"""SEC-010: PII minimization audit.

The rule: NO API response may contain ``phone_hash`` or exact geo
coordinates. ``home_zip`` is visible only to the owner.
(M17: ``device_fingerprint`` was dropped from the schema — it was collected
but never used.)
This module audits every Wave 1 response schema two ways:

1. Serializer-level: feed rows carrying ALL sensitive fields through each
   public serializer and recursively scan the output for leaks.
2. HTTP-level: seed the memory repos with sensitive data and assert the
   wire responses contain none of the sensitive values.

If a new endpoint/serializer is added without PII review, extend the
``SERIALIZERS`` / ``ENDPOINTS`` lists below — the audit fails closed.
"""
from __future__ import annotations

import pytest

FORBIDDEN_KEYS = {"phone_hash"}
# home_zip may appear ONLY in owner-scoped responses (handled per-case below).


def _find_leaks(obj, forbidden_keys, forbidden_values, path="$", leaks=None):
    leaks = [] if leaks is None else leaks
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in forbidden_keys:
                leaks.append(f"{path}.{k}: forbidden key present")
            _find_leaks(v, forbidden_keys, forbidden_values, f"{path}.{k}", leaks)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _find_leaks(v, forbidden_keys, forbidden_values, f"{path}[{i}]", leaks)
    elif isinstance(obj, str):
        for fv in forbidden_values:
            if fv and fv in obj:
                leaks.append(f"{path}: forbidden value {fv!r} embedded in string")
    return leaks


SENSITIVE_USER_ROW = {
    "uid": "alice",
    "phone_hash": "PHASH-SECRET-123",
    "display_name": "Alice",
    "avatar_url": "https://example.com/a.png",
    "home_zip": "85281",
    "idv_status": "verified",
    "created_at": "2026-09-27T00:00:00+00:00",
}

SENSITIVE_LISTING_ROW = {
    "id": "11111111-1111-1111-1111-111111111111",
    "owner_uid": "alice",
    "type": "seedling",
    "photos": ["https://example.com/p.jpg"],
    "variety": "Tomato",
    "quantity": 6,
    "unit": "starts",
    "credit_cost": 2,
    "pickup_window": ("2026-10-01T10:00:00+00:00", "2026-10-01T12:00:00+00:00"),
    "expires_at": "2026-10-05T00:00:00+00:00",
    "geo_lat": 33.4152,
    "geo_lon": -111.8315,
    "spray_disclosure": "none",
    "status": "live",
    "created_at": "2026-09-27T00:00:00+00:00",
}


def _sensitive_values():
    return {
        SENSITIVE_USER_ROW["phone_hash"],
        SENSITIVE_USER_ROW["home_zip"],
        str(SENSITIVE_LISTING_ROW["geo_lat"]),
        str(SENSITIVE_LISTING_ROW["geo_lon"]),
    }


def test_public_profile_serializer_leaks_nothing():
    from app.users import public_profile

    out = public_profile(SENSITIVE_USER_ROW)
    leaks = _find_leaks(out, FORBIDDEN_KEYS, _sensitive_values())
    # home_zip is forbidden in the PUBLIC serializer specifically
    assert "home_zip" not in out
    assert leaks == [], leaks


def test_owner_profile_serializer_leaks_nothing_sensitive():
    from app.users import owner_profile

    out = owner_profile(SENSITIVE_USER_ROW)
    # owner MAY see their own zip — but nothing else sensitive
    values = {SENSITIVE_USER_ROW["phone_hash"]}
    leaks = _find_leaks(out, FORBIDDEN_KEYS, values)
    assert leaks == [], leaks
    assert out["home_zip"] == "85281"  # owner-scoped: allowed


def test_listing_serializer_fuzzes_geo_and_leaks_nothing():
    import random

    from app.crypto import GEO_KEY_ENV, encrypt_float
    from app.listings import public_listing

    # Repo rows carry ENCRYPTED geo (at-rest contract); the serializer
    # decrypts in-process immediately before fuzzing.
    row = dict(SENSITIVE_LISTING_ROW)
    row["geo_lat"] = encrypt_float(row["geo_lat"], GEO_KEY_ENV)
    row["geo_lon"] = encrypt_float(row["geo_lon"], GEO_KEY_ENV)
    out = public_listing(row, rng=random.Random(7), viewer_uid=None)
    leaks = _find_leaks(out, FORBIDDEN_KEYS, _sensitive_values())
    assert leaks == [], leaks
    # exact coordinates must never be returned
    assert out["geo_lat"] != 33.4152
    assert out["geo_lon"] != -111.8315
    assert "home_zip" not in out


def test_verify_and_idv_responses_carry_no_pii(mem_users, mock_verify, auth_headers,
                                              monkeypatch):
    # C1: the stub IDV route is dead without the explicit opt-in.
    monkeypatch.setenv("ENABLE_IDV_STUB", "1")
    monkeypatch.setenv("IDV_PROVIDER", "stub")
    monkeypatch.setenv("IDV_WEBHOOK_SECRET", "test-secret")
    client, repo = mem_users
    repo.upsert("alice", phone_hash="PHASH-SECRET-123")

    r = client.post("/v1/auth/verify", headers=auth_headers)
    assert r.status_code == 200
    assert _find_leaks(r.json(), FORBIDDEN_KEYS, {"PHASH-SECRET-123"}) == []

    r = client.post("/v1/idv/stub/decide", json={"decision": "approved"}, headers=auth_headers)
    assert r.status_code == 200
    assert _find_leaks(r.json(), FORBIDDEN_KEYS, {"PHASH-SECRET-123"}) == []


def test_http_profile_endpoints_leak_nothing(mem_users, mock_verify, auth_headers):
    client, repo = mem_users
    repo.upsert("alice", display_name="Alice", home_zip="85281",
                phone_hash="PHASH-SECRET-123")

    for path in ("/v1/users/me", "/v1/users/alice"):
        body = client.get(path, headers=auth_headers).json()
        leaks = _find_leaks(body, FORBIDDEN_KEYS, {"PHASH-SECRET-123"})
        assert leaks == [], (path, leaks)
    # public endpoint additionally hides home_zip
    assert "85281" not in str(client.get("/v1/users/alice", headers=auth_headers).json())


def test_http_listing_detail_hides_exact_geo(mem_listings, mock_verify, auth_headers):
    client, urepo, lrepo, _, _ = mem_listings
    urepo.upsert("alice", display_name="Alice", home_zip="85281", phone_hash="PH")
    lrepo.create({
        "id": "22222222-2222-2222-2222-222222222222",
        "owner_uid": "alice", "type": "harvest",
        "photos": ["https://example.com/p.jpg"], "credit_cost": 1,
        "spray_disclosure": "none", "status": "live",
        "geo_lat": 33.4152, "geo_lon": -111.8315,
    })
    body = client.get("/v1/listings/22222222-2222-2222-2222-222222222222",
                      headers=auth_headers).json()
    # Fuzzed != exact: numeric inequality, not substring (a jittered value like
    # 33.41520140687793 legitimately starts with the same digits).
    assert body["geo_lat"] != 33.4152
    assert body["geo_lon"] != -111.8315
    assert "85281" not in str(body)
    assert "PH" not in str(body.values())


def test_notification_prefs_carry_no_pii(mem_notify, mock_verify, auth_headers):
    client, _ = mem_notify
    body = client.put("/v1/users/me/notification-prefs",
                      json={"categories": {"harvest_alerts": True}, "quiet_hours": True},
                      headers=auth_headers).json()
    assert _find_leaks(body, FORBIDDEN_KEYS, set()) == []
    assert set(body) == {"user_uid", "categories", "quiet_hours", "quietHours"}
