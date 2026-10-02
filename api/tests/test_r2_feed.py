"""API-123 (feed filterable by way), API-135 (GET /v1/trees),
AND-125/AND-126 (potSize / plantAgeYears / pickupWindowDays on create)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _listing_payload(**kw):
    base = {
        "type": "seedling",
        "photos": ["https://example.com/p.jpg"],
        "credit_cost": 1,
        "spray_disclosure": "unsprayed",
        "status": "live",
    }
    base.update(kw)
    return base


@pytest.fixture()
def alice(mem_listings):
    client, urepo, *_ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    return client


def _seed(client, headers, **kw):
    r = client.post("/v1/listings", json=_listing_payload(**kw), headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- feed by way

def test_feed_way_filters_by_listing_type(alice, mock_verify, auth_headers):
    # One live listing per type; a draft seedling must not leak through either.
    _seed(alice, auth_headers, type="seedling", variety="tomato")
    _seed(alice, auth_headers, type="harvest", variety="apples", quantity=5, unit="kg")
    _seed(alice, auth_headers, type="tree", variety="Meyer lemon")
    _seed(alice, auth_headers, type="seedling", variety="draft-only", status="draft")

    for way, expect in (
        ("seedling", {"tomato"}),
        ("harvest", {"apples"}),
        ("pick", {"Meyer lemon"}),
    ):
        r = alice.get(f"/v1/feed?way={way}", headers=auth_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) == {"listings"}, body.keys()  # exactly {listings:[...]}
        assert {i["variety"] for i in body["listings"]} == expect


def test_feed_way_ranking_newest_first_null_expiry_last(alice, mock_verify, auth_headers):
    now = datetime.now(timezone.utc)
    _seed(alice, auth_headers, type="seedling", variety="late",
          expires_at=(now + timedelta(days=10)).isoformat())
    _seed(alice, auth_headers, type="seedling", variety="no-expiry")  # null expiry
    _seed(alice, auth_headers, type="seedling", variety="soon",
          expires_at=(now + timedelta(days=1)).isoformat())

    r = alice.get("/v1/feed?way=seedling", headers=auth_headers)
    assert r.status_code == 200, r.text
    # No want-list entries: newest post first, then soonest expiry (nulls last).
    assert [i["variety"] for i in r.json()["listings"]] == ["soon", "no-expiry", "late"]


def test_feed_way_want_matches_first(mem_listings, mock_verify, auth_headers):
    """An older want-list match outranks a newer non-match."""
    client, urepo, lrepo, _, _ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    r = client.post("/v1/want-list", json={"variety": "tomato"},
                    headers=auth_headers)
    assert r.status_code == 201, r.text
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=5)).isoformat()
    new = (now - timedelta(minutes=5)).isoformat()
    for lid, variety, created in (("m1", "Cherokee Purple tomato", old),
                                  ("m2", "basil", new)):
        lrepo.create({"id": lid, "owner_uid": "bob", "type": "seedling",
                      "photos": [], "variety": variety, "quantity": 1,
                      "unit": "starts", "credit_cost": 1,
                      "spray_disclosure": "none", "status": "live",
                      "created_at": created,
                      "expires_at": (now + timedelta(days=3)).isoformat()})
    r = client.get("/v1/feed?way=seedling", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert [i["variety"] for i in r.json()["listings"]] == [
        "Cherokee Purple tomato", "basil"]


def test_feed_way_limit_honored(alice, mock_verify, auth_headers):
    for i in range(3):
        _seed(alice, auth_headers, type="seedling", variety=f"v{i}")
    r = alice.get("/v1/feed?way=seedling&limit=2", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["listings"]) == 2


def test_feed_way_invalid_is_422(alice, mock_verify, auth_headers):
    r = alice.get("/v1/feed?way=rocket", headers=auth_headers)
    assert r.status_code == 422


def test_feed_way_sitting_lists_active_sitters(alice, mock_verify, auth_headers):
    from app import sitter as sitter_mod

    srepo = sitter_mod.MemorySitterRepo()
    alice.app.dependency_overrides[sitter_mod.get_sitter_repo] = lambda: srepo
    srepo.upsert_profile("bob", {"bio": "pro", "active": True})
    srepo.upsert_profile("zed", {"bio": "idle", "active": False})

    r = alice.get("/v1/feed?way=sitting", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # L4c: the response key reflects the payload type (sitters, not listings).
    assert set(body.keys()) == {"sitters"}
    assert [s["uid"] for s in body["sitters"]] == ["bob"]


def test_feed_without_way_keeps_legacy_shape(alice, mock_verify, auth_headers):
    _seed(alice, auth_headers, type="seedling", variety="tomato")
    r = alice.get("/v1/feed", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"items", "next_cursor"}


# ---------------------------------------------------------------- GET /trees

def test_trees_returns_live_trees_only(alice, mock_verify, auth_headers):
    _seed(alice, auth_headers, type="tree", variety="Meyer lemon",
          geo_lat=33.4, geo_lon=-111.8,
          visit_rules="Daylight only.")
    _seed(alice, auth_headers, type="seedling", variety="tomato")
    _seed(alice, auth_headers, type="tree", variety="draft-tree", status="draft")

    r = alice.get("/v1/trees", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"trees"}
    assert len(body["trees"]) == 1
    card = body["trees"][0]
    assert card["variety"] == "Meyer lemon"
    assert card["visit_rules"] == "Daylight only."
    # Approx location: fuzzed, never the true coordinates.
    assert (card["geo_lat"], card["geo_lon"]) != (33.4, -111.8)


# ---------------------------------------------------------------- new fields

def test_create_round_trips_new_fields(alice, mock_verify, auth_headers):
    payload = _listing_payload(type="seedling", variety="basil",
                               potSize="4 inch", plantAgeYears="2 months",
                               pickupWindowDays=6)
    r = alice.post("/v1/listings", json=payload, headers=auth_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["potSize"] == "4 inch"
    assert body["plantAgeYears"] == "2 months"
    assert body["pickupWindowDays"] == 6

    # GET reflects the persisted values too.
    r2 = alice.get(f"/v1/listings/{body['id']}", headers=auth_headers)
    assert r2.json()["potSize"] == "4 inch"
    assert r2.json()["plantAgeYears"] == "2 months"
    assert r2.json()["pickupWindowDays"] == 6


def test_new_fields_default_when_omitted(alice, mock_verify, auth_headers):
    body = _seed(alice, auth_headers, type="harvest", variety="apples")
    assert body["potSize"] is None
    assert body["plantAgeYears"] is None
    assert body["pickupWindowDays"] == 4  # AND-126 default


def test_pickup_window_days_validation(alice, mock_verify, auth_headers):
    for bad in (0, -3, 99):
        r = alice.post("/v1/listings",
                       json=_listing_payload(pickupWindowDays=bad),
                       headers=auth_headers)
        assert r.status_code == 422, (bad, r.text)
