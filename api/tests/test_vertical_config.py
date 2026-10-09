"""Configurable verticals: config loading, /v1/config, config-driven
economy/taxonomy, and strict-schema failure modes.

The default vertical must reproduce the pre-config Garden Swap literals
exactly; a second vertical ("toolshare", shipped as a fixture config)
must change behavior through config alone — no code edits per vertical.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.vertical import (
    GARDEN_DEFAULT,
    VerticalConfigError,
    get_vertical,
    reset_vertical_cache,
    vertical_from_dict,
)


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
def toolshare(monkeypatch):
    monkeypatch.setenv("VERTICAL_ID", "toolshare")
    reset_vertical_cache()
    return get_vertical()


def _write_config(tmp_path, payload: dict) -> str:
    p = tmp_path / "vertical.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# Defaults == yesterday's literals
# ---------------------------------------------------------------------------

def test_default_is_garden_default():
    v = get_vertical()
    assert v is GARDEN_DEFAULT
    assert v.vertical_id == "garden"
    assert v.config_version == 1
    assert v.brand.display_name == "Garden Swap"
    assert v.brand.nouns.provider == "sitter"
    assert v.brand.nouns.seeker == "claimer"
    assert all(vars(v.modules).values())
    assert v.economy.credit_name == "credit"
    assert v.economy.credits_enabled is True
    assert v.economy.usd_services_enabled is True
    assert v.economy.starter_credits == 3
    assert v.economy.earn_cap_amount == 10
    assert v.economy.earn_cap_window_days == 7
    assert v.economy.max_listing_cost == 100
    assert v.economy.credit_expiry == "seasonal"
    assert v.fees.sitter_platform_fee_pct == 18
    assert v.taxonomy.listing_types == ("seedling", "harvest", "tree")
    assert v.taxonomy.sitter_services == (
        "watering", "repotting", "fertilizing", "pruning",
        "pest_control", "vacation_care",
    )
    assert v.geo.default_radius_miles == 5
    assert v.trust.idv_required_actions == ()


def test_shipped_garden_json_matches_default():
    from pathlib import Path

    raw = json.loads(Path("app/verticals/garden.json").read_text(encoding="utf-8"))
    assert vertical_from_dict(raw) == GARDEN_DEFAULT


def test_toolshare_fixture_loads(toolshare):
    v = toolshare
    assert v.vertical_id == "toolshare"
    assert v.brand.display_name == "ToolShare"
    assert v.economy.credit_name == "token"
    assert v.economy.starter_credits == 5
    assert v.economy.max_listing_cost == 50
    assert v.fees.sitter_platform_fee_pct == 10
    # v1 honesty (H1): listing_types is declared display metadata, NOT
    # enforced — the fixture must advertise the vocabulary the write
    # path actually accepts, not fictional tool types.
    assert v.taxonomy.listing_types == ("seedling", "harvest", "tree")
    assert v.taxonomy.listing_types == GARDEN_DEFAULT.taxonomy.listing_types
    assert v.taxonomy.sitter_services == ("lending", "repair")
    assert v.geo.default_radius_miles == 10
    assert v.modules.trees is False
    assert v.modules.bookings is False
    assert v.modules.listings is True


def test_config_path_beats_vertical_id(monkeypatch, tmp_path):
    path = _write_config(tmp_path, {"vertical_id": "custom-x"})
    monkeypatch.setenv("VERTICAL_ID", "toolshare")
    monkeypatch.setenv("VERTICAL_CONFIG_PATH", path)
    reset_vertical_cache()
    assert get_vertical().vertical_id == "custom-x"


# ---------------------------------------------------------------------------
# GET /v1/config
# ---------------------------------------------------------------------------

def test_config_endpoint_public_allowlist(client):
    r = client.get("/v1/config")  # no auth header: exempt by design
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "public, max-age=300"
    assert r.headers["ETag"] == '"garden-1"'
    body = r.json()
    assert set(body) == {
        "vertical_id", "config_version", "brand", "modules", "economy",
        "fees", "taxonomy", "geo", "trust",
    }
    assert body["vertical_id"] == "garden"
    assert body["brand"]["display_name"] == "Garden Swap"
    assert body["brand"]["nouns"]["provider"] == "sitter"
    assert body["economy"] == {
        "credit_name": "credit", "credits_enabled": True,
        "usd_services_enabled": True, "starter_credits": 3,
        "max_listing_cost": 100, "credit_expiry": "seasonal",
    }
    assert body["fees"] == {"sitter_platform_fee_pct": 18}
    assert body["taxonomy"]["sitter_services"][0] == "watering"
    # Anti-gaming tuning and secret-adjacent keys never serialize.
    blob = json.dumps(body).lower()
    for banned in ("earn_cap", "database", "secret", "password", "token"):
        assert banned not in blob


def test_config_endpoint_reflects_active_vertical(client, toolshare):
    r = client.get("/v1/config")
    assert r.status_code == 200
    assert r.headers["ETag"] == '"toolshare-1"'
    body = r.json()
    assert body["vertical_id"] == "toolshare"
    assert body["brand"]["display_name"] == "ToolShare"
    assert body["economy"]["starter_credits"] == 5
    assert body["economy"]["max_listing_cost"] == 50
    assert body["modules"]["trees"] is False


# ---------------------------------------------------------------------------
# Config drives behavior (toolshare / custom fixtures)
# ---------------------------------------------------------------------------

def test_starter_grant_follows_config(toolshare):
    from app import credits as credits_mod

    repo = credits_mod.MemoryCreditRepo()
    credits_mod.ensure_starter_credits("alice", repo)
    starters = [e for e in repo.entries("alice") if e["reason"] == "starter"]
    assert len(starters) == 1
    assert starters[0]["delta"] == 5
    assert repo.balance("alice") == 5


def test_earn_cap_follows_config(monkeypatch, tmp_path):
    from app import credits as credits_mod

    path = _write_config(tmp_path, {
        "vertical_id": "captest", "economy": {"earn_cap_amount": 3}})
    monkeypatch.setenv("VERTICAL_CONFIG_PATH", path)
    reset_vertical_cache()

    repo = credits_mod.MemoryCreditRepo()
    repo.add_entry("alice", 3, "welcome_bonus", idempotency_key="c1")
    with pytest.raises(credits_mod.EarnCapExceededError):
        repo.add_entry("alice", 1, "welcome_bonus", idempotency_key="c2")


def test_fee_quote_follows_config(toolshare):
    from app import payments as payments_mod

    assert payments_mod.quote_booking({"subtotal_cents": 10000})["fee_cents"] == 1000


def test_fee_quote_default_is_18_percent():
    from app import payments as payments_mod

    assert payments_mod.quote_booking({"subtotal_cents": 10000})["fee_cents"] == 1800


@pytest.fixture()
def mem_sitting_v(client, monkeypatch):
    """Sitter stack (mirrors test_sitter's fixture) for taxonomy tests."""
    from app import sitter as sitter_mod
    from app import users as users_mod
    from conftest import wire_credit_repo
    import app.auth as auth_mod

    urepo = users_mod.MemoryUserRepo()
    srepo = sitter_mod.MemorySitterRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[sitter_mod.get_sitter_repo] = lambda: srepo
    wire_credit_repo(client)

    def fake(token: str) -> dict:
        if token == "good-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "bob-token":
            return {"uid": "bob"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)
    return client


BOB = {"Authorization": "Bearer " + "bob-" + "token"}


def test_sitter_services_follow_taxonomy(mem_sitting_v, toolshare):
    client = mem_sitting_v
    r = client.post("/v1/users", json={"display_name": "Bob",
                                       "age_attestation": True}, headers=BOB)
    assert r.status_code == 200, r.text

    # Garden's taxonomy no longer applies under toolshare.
    r = client.put("/v1/sitters/me", json={"services": ["watering"]}, headers=BOB)
    assert r.status_code == 422
    # Toolshare's own service is accepted.
    r = client.put("/v1/sitters/me", json={"services": ["repair"]}, headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["services"] == ["repair"]


def test_listing_models_enforce_vertical_max(toolshare):
    from app.listings import ListingIn, ListingPatch
    from app.slots import SlotIn

    base = {"type": "seedling", "photos": ["https://x/a.jpg"],
            "spray_disclosure": "none"}
    ListingIn(**base, credit_cost=50)  # at the toolshare ceiling: fine
    with pytest.raises(ValidationError):
        ListingIn(**base, credit_cost=51)
    with pytest.raises(ValidationError):
        ListingPatch(credit_cost=51)
    SlotIn(dayMs=0, startMs=0, endMs=1, maxPickers=1, creditCost=50)
    with pytest.raises(ValidationError):
        SlotIn(dayMs=0, startMs=0, endMs=1, maxPickers=1, creditCost=51)


def test_listing_above_vertical_max_422_http(mem_listings, mock_verify,
                                             auth_headers, toolshare):
    client, urepo, *_ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    payload = {
        "type": "seedling", "photos": ["https://example.com/a.jpg"],
        "variety": "Cherokee Purple tomato", "quantity": 6, "unit": "starts",
        "credit_cost": 51, "spray_disclosure": "Neem oil only.",
        "status": "live", "geo_lat": 33.4152, "geo_lon": -111.8315,
    }
    r = client.post("/v1/listings", json=payload, headers=auth_headers)
    assert r.status_code == 422

    payload["credit_cost"] = 50
    r = client.post("/v1/listings", json=payload, headers=auth_headers)
    assert r.status_code == 201, r.text
    listing_id = r.json().get("id") or r.json()["listing"]["id"]
    r = client.patch(f"/v1/listings/{listing_id}",
                     json={"credit_cost": 51}, headers=auth_headers)
    assert r.status_code == 422


def test_listing_type_vocabulary_boundary(mem_listings, mock_verify,
                                          auth_headers, toolshare):
    """H1 boundary, documented behavior: under toolshare the enforced
    garden vocabulary creates fine; a declared-but-fictional type 422s.
    This pins the current v1 limit — when listing_types becomes
    enforced (future migration), THIS test is the one to change."""
    client, urepo, *_ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    payload = {
        "type": "seedling", "photos": ["https://example.com/a.jpg"],
        "variety": "Cherokee Purple tomato", "quantity": 6, "unit": "starts",
        "credit_cost": 10, "spray_disclosure": "Neem oil only.",
        "status": "live", "geo_lat": 33.4152, "geo_lon": -111.8315,
    }
    r = client.post("/v1/listings", json=payload, headers=auth_headers)
    assert r.status_code == 201, r.text
    payload["type"] = "tool"  # toolshare's pre-H1 fictional type
    r = client.post("/v1/listings", json=payload, headers=auth_headers)
    assert r.status_code == 422


def test_earn_cap_error_uses_vertical_credit_name(toolshare):
    from app import credits as credits_mod

    repo = credits_mod.MemoryCreditRepo()
    repo.add_entry("alice", 10, "welcome_bonus", idempotency_key="t1")
    with pytest.raises(credits_mod.EarnCapExceededError) as exc_info:
        repo.add_entry("alice", 1, "welcome_bonus", idempotency_key="t2")
    detail = exc_info.value.detail
    assert detail["code"] == "earn_cap_exceeded"  # code unchanged
    assert "tokens earned" in detail["message"]


def test_sitter_radius_default_follows_vertical(mem_sitting_v, toolshare):
    client = mem_sitting_v
    r = client.post("/v1/users", json={"display_name": "Bob",
                                       "age_attestation": True}, headers=BOB)
    assert r.status_code == 200, r.text
    r = client.put("/v1/sitters/me", json={}, headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["service_radius_miles"] == 10


def test_sitter_radius_default_is_garden_five(mem_sitting_v):
    client = mem_sitting_v
    r = client.post("/v1/users", json={"display_name": "Bob",
                                       "age_attestation": True}, headers=BOB)
    assert r.status_code == 200, r.text
    r = client.put("/v1/sitters/me", json={}, headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["service_radius_miles"] == 5


# ---------------------------------------------------------------------------
# Strict schema: bad configs fail loudly at load
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"bogus": 1},                                            # unknown top key
    {"economy": {"bogus": 1}},                               # unknown nested
    {"brand": {"nouns": {"bogus": "x"}}},                    # unknown deep key
    {"fees": {"sitter_platform_fee_pct": 101}},              # fee > 100
    {"fees": {"sitter_platform_fee_pct": -1}},               # fee < 0
    {"economy": {"max_listing_cost": 101}},                  # past DB CHECK
    {"economy": {"max_listing_cost": 0}},                    # below floor
    {"economy": {"starter_credits": -1}},
    {"economy": {"earn_cap_amount": -1}},
    {"economy": {"earn_cap_window_days": 0}},
    {"taxonomy": {"sitter_services": []}},                   # empty, sitters on
    {"taxonomy": {"listing_types": []}},                     # empty, listings on
    {"modules": {"listings": "yes"}},                        # wrong type
    {"economy": {"credits_enabled": "yes"}},                 # switch not bool
    {"economy": {"usd_services_enabled": 1}},                # switch not bool
])
def test_strict_schema_rejections(payload):
    with pytest.raises(VerticalConfigError):
        vertical_from_dict(payload)


def test_credit_expiry_never_not_implemented():
    with pytest.raises(VerticalConfigError, match="not implemented"):
        vertical_from_dict({"economy": {"credit_expiry": "never"}})


@pytest.mark.parametrize("payload", [
    {"fees": {"sitter_platform_fee_pct": float("nan")}},
    {"fees": {"sitter_platform_fee_pct": float("inf")}},
    {"geo": {"default_radius_miles": float("nan")}},
    {"geo": {"default_radius_miles": float("inf")}},
])
def test_nonfinite_numbers_rejected(payload):
    # JSON NaN/Infinity literals parse to non-finite floats; a NaN fee
    # or radius must not slip through range checks (L2).
    with pytest.raises(VerticalConfigError):
        vertical_from_dict(payload)


def test_malformed_config_by_id_fails_loudly(monkeypatch, tmp_path):
    import app.vertical as vertical_mod

    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(vertical_mod, "VERTICALS_DIR", tmp_path)
    monkeypatch.setenv("VERTICAL_ID", "broken")
    reset_vertical_cache()
    with pytest.raises(VerticalConfigError, match="invalid JSON"):
        get_vertical()


def test_unknown_vertical_id_fails_loudly(monkeypatch):
    monkeypatch.setenv("VERTICAL_ID", "no-such-vertical")
    reset_vertical_cache()
    with pytest.raises(VerticalConfigError, match="unknown VERTICAL_ID"):
        get_vertical()


def test_malformed_config_file_fails_loudly(monkeypatch, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("VERTICAL_CONFIG_PATH", str(p))
    reset_vertical_cache()
    with pytest.raises(VerticalConfigError):
        get_vertical()
