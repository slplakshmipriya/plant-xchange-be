"""At-rest encryption for listing geo coordinates (SEC-010).

- geo_lat/geo_lon are Fernet ciphertext at rest (both repos)
- public serializers decrypt in-process immediately before fuzzing, so
  served coordinates stay fuzzed floats near the true point
"""
from __future__ import annotations

import math

import pytest


@pytest.fixture()
def mem_geo(client, monkeypatch):
    from app import listings as listings_mod
    from app import notify as notify_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from conftest import wire_credit_repo
    import app.auth as auth_mod

    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    wrepo = wantlist_mod.MemoryWantRepo()
    nrepo = notify_mod.MemoryNotificationRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wrepo
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: nrepo
    wire_credit_repo(client)

    def fake(token: str) -> dict:
        if token == "good-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)
    return client, urepo, lrepo


ALICE = {"Authorization": "Bearer good-token"}
LAT, LON = 33.4152, -111.8315


def _create_listing(client, **over):
    r = client.post("/v1/users", json={"display_name": "Alice", "age_attestation": True}, headers=ALICE)
    assert r.status_code == 200, r.text
    payload = {
        "type": "seedling", "photos": ["https://example.com/t.jpg"],
        "variety": "Basil", "quantity": 6, "unit": "starts",
        "credit_cost": 1, "spray_disclosure": "unsprayed", "status": "live",
        "geo_lat": LAT, "geo_lon": LON,
    }
    payload.update(over)
    r = client.post("/v1/listings", json=payload, headers=ALICE)
    assert r.status_code == 201, r.text
    return r.json()["id"]  # create returns public_listing(row) directly


def _haversine_mi(a, b, c, d):
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 2 * 3959 * math.asin(math.sqrt(h))


def test_geo_is_ciphertext_at_rest(mem_geo):
    from app.crypto import GEO_KEY_ENV, decrypt_float

    client, _, lrepo = mem_geo
    lid = _create_listing(client)
    stored_lat = lrepo._rows[lid]["geo_lat"]
    stored_lon = lrepo._rows[lid]["geo_lon"]
    assert isinstance(stored_lat, str) and isinstance(stored_lon, str)
    assert stored_lat != str(LAT) and "33.4152" not in stored_lat
    assert decrypt_float(stored_lat, GEO_KEY_ENV) == pytest.approx(LAT)
    assert decrypt_float(stored_lon, GEO_KEY_ENV) == pytest.approx(LON)


def test_public_listing_still_serves_fuzzed_geo(mem_geo):
    client, _, _ = mem_geo
    lid = _create_listing(client)
    r = client.get(f"/v1/listings/{lid}", headers=ALICE)
    assert r.status_code == 200, r.text
    body = r.json()  # get returns public_listing(row) directly
    assert isinstance(body["geo_lat"], float)
    assert body["geo_lat"] != LAT  # never the exact point
    assert 0 < _haversine_mi(LAT, LON, body["geo_lat"], body["geo_lon"]) <= 0.6


def test_none_geo_round_trips(mem_geo):
    from app.listings import public_listing

    client, _, lrepo = mem_geo
    lid = _create_listing(client, geo_lat=None, geo_lon=None)
    assert lrepo._rows[lid]["geo_lat"] is None
    out = public_listing(lrepo._rows[lid], viewer_uid=None)
    assert out["geo_lat"] is None and out["geo_lon"] is None


def test_update_path_encrypts(mem_geo):
    """ListingPatch has no geo fields today; the repo update path still
    encrypts geo if a future caller passes it (defense in depth)."""
    from app.crypto import GEO_KEY_ENV, decrypt_float
    from app.listings import public_listing

    client, _, lrepo = mem_geo
    lid = _create_listing(client)
    lrepo.update(lid, {"geo_lat": 34.0, "geo_lon": -112.0})
    stored = lrepo._rows[lid]["geo_lat"]
    assert isinstance(stored, str) and stored != "34.0"
    assert decrypt_float(stored, GEO_KEY_ENV) == pytest.approx(34.0)
    assert public_listing(lrepo._rows[lid], viewer_uid=None)["geo_lat"] != 34.0  # served fuzzed


def test_tampered_geo_raises():
    from app.crypto import GEO_KEY_ENV, decrypt_float

    with pytest.raises(RuntimeError):
        decrypt_float("not-a-valid-token", GEO_KEY_ENV)


def test_missing_key_fail_closed(monkeypatch):
    from app.crypto import GEO_KEY_ENV, decrypt_float, encrypt_float

    monkeypatch.delenv(GEO_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError):
        encrypt_float(33.4, GEO_KEY_ENV)
    with pytest.raises(RuntimeError):
        decrypt_float("whatever", GEO_KEY_ENV)


def test_tree_card_fuzzes_decrypted_geo():
    import random

    from app.crypto import GEO_KEY_ENV, encrypt_float
    from app.listings import _tree_card

    row = {"id": "t1", "variety": "Apple",
           "geo_lat": encrypt_float(LAT, GEO_KEY_ENV),
           "geo_lon": encrypt_float(LON, GEO_KEY_ENV),
           "pickup_window": None, "expires_at": None,
           "spray_disclosure": "unsprayed"}
    out = _tree_card(row)
    assert out["geo_lat"] != LAT
    assert 0 < _haversine_mi(LAT, LON, out["geo_lat"], out["geo_lon"]) <= 0.6
