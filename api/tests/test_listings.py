"""API-020: listing CRUD, lifecycle state machine, geo fuzzing, expiry sweep."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.listings import TRANSITIONS, can_transition, fuzz_location


def _listing_payload(**kw):
    base = {
        "type": "seedling",
        "photos": ["https://example.com/a.jpg"],
        "variety": "Cherokee Purple tomato",
        "quantity": 6,
        "unit": "starts",
        "credit_cost": 2,
        "spray_disclosure": "Neem oil only, last applied 3 weeks ago.",
        "status": "live",
        "geo_lat": 33.4152,
        "geo_lon": -111.8315,
    }
    base.update(kw)
    return base


@pytest.fixture()
def alice_profile(mem_listings, mock_verify, auth_headers):
    client, urepo, lrepo, _, _ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    return client, urepo, lrepo


@pytest.fixture()
def mem_retention(mem_listings):
    """Wire the in-memory retention repo into the sweep route (M19).

    Shares the notification log and listing rows with the mem_listings
    fakes so retention purges are observable in tests."""
    from app import listings as listings_mod

    client, _, lrepo, _, nrepo = mem_listings
    rrepo = listings_mod.MemoryRetentionRepo(
        notification_log=nrepo._log, listings=lrepo._rows)
    client.app.dependency_overrides[listings_mod.get_retention_repo] = lambda: rrepo
    return rrepo


# ------------------------------------------------ state machine table test

VALID = [
    ("draft", "live"), ("draft", "cancelled"),
    ("live", "claimed"), ("live", "expired"), ("live", "cancelled"),
    ("claimed", "completed"), ("claimed", "cancelled"), ("claimed", "live"),
]
INVALID = [
    ("draft", "claimed"), ("draft", "completed"), ("draft", "expired"),
    ("live", "draft"), ("live", "completed"),
    ("claimed", "draft"), ("claimed", "expired"),
    ("completed", "live"), ("completed", "cancelled"),
    ("expired", "live"), ("cancelled", "live"),
]


@pytest.mark.parametrize("frm,to", VALID)
def test_valid_transitions(frm, to):
    assert can_transition(frm, to), f"{frm}->{to} should be legal"


@pytest.mark.parametrize("frm,to", INVALID)
def test_invalid_transitions(frm, to):
    assert not can_transition(frm, to), f"{frm}->{to} should be illegal"


def test_transition_table_is_exhaustive():
    states = {"draft", "live", "claimed", "completed", "expired", "cancelled"}
    assert set(TRANSITIONS) == states
    for targets in TRANSITIONS.values():
        assert targets <= states


# ------------------------------------------------ CRUD

def test_create_requires_photo(alice_profile, mock_verify, auth_headers):
    client, _, _ = alice_profile
    r = client.post("/v1/listings", json=_listing_payload(photos=[]), headers=auth_headers)
    assert r.status_code == 422  # pydantic min_length


def test_create_requires_spray_disclosure(alice_profile, mock_verify, auth_headers):
    client, _, _ = alice_profile
    payload = _listing_payload()
    del payload["spray_disclosure"]
    r = client.post("/v1/listings", json=payload, headers=auth_headers)
    assert r.status_code == 422


def test_create_rejects_bad_credit_cost(alice_profile, mock_verify, auth_headers):
    client, _, _ = alice_profile
    r = client.post("/v1/listings", json=_listing_payload(credit_cost=101), headers=auth_headers)
    assert r.status_code == 422


def test_create_requires_profile(mem_listings, mock_verify, auth_headers):
    client, *_ = mem_listings  # no profile for alice
    r = client.post("/v1/listings", json=_listing_payload(), headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["code"] == "profile_required"


def test_create_and_get_roundtrip(alice_profile, mock_verify, auth_headers):
    client, _, _ = alice_profile
    r = client.post("/v1/listings", json=_listing_payload(), headers=auth_headers)
    assert r.status_code == 201
    created = r.json()
    assert created["status"] == "live"
    assert created["owner_uid"] == "alice"

    r = client.get(f"/v1/listings/{created['id']}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]


def test_detail_fuzzes_geo(alice_profile, mock_verify, auth_headers):
    client, _, lrepo = alice_profile
    r = client.post("/v1/listings", json=_listing_payload(), headers=auth_headers)
    body = r.json()
    # fuzzed point must differ from the true stored coordinates
    assert (body["geo_lat"], body["geo_lon"]) != (33.4152, -111.8315)
    # ...but stay within ~0.6 mi (haversine)
    def haversine_mi(a, b, c, d):
        p = math.pi / 180
        h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
        return 2 * 3959 * math.asin(math.sqrt(h))
    dist = haversine_mi(33.4152, -111.8315, body["geo_lat"], body["geo_lon"])
    assert 0 < dist <= 0.6


def test_fuzz_location_deterministic_with_seeded_rng():
    a = fuzz_location(33.4, -111.8, random.Random(42))
    b = fuzz_location(33.4, -111.8, random.Random(42))
    assert a == b
    c = fuzz_location(33.4, -111.8, random.Random(43))
    assert a != c


def test_patch_illegal_transition_rejected(alice_profile, mock_verify, auth_headers):
    client, _, _ = alice_profile
    created = client.post("/v1/listings", json=_listing_payload(), headers=auth_headers).json()
    r = client.patch(f"/v1/listings/{created['id']}", json={"status": "draft"}, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_transition"


def test_patch_to_claimed_or_completed_rejected(alice_profile, mock_verify, auth_headers):
    """H1: PATCH status is restricted to {draft, live, cancelled} — an owner
    cannot bypass the claim/confirm flow (and its credit movement) by
    PATCHing straight to claimed/completed."""
    client, _, _ = alice_profile
    created = client.post("/v1/listings", json=_listing_payload(), headers=auth_headers).json()
    for target in ("claimed", "completed"):
        r = client.patch(f"/v1/listings/{created['id']}", json={"status": target},
                         headers=auth_headers)
        assert r.status_code == 422, (target, r.text)
    # The listing was not moved.
    r = client.get(f"/v1/listings/{created['id']}", headers=auth_headers)
    assert r.json()["status"] == "live"


def test_patch_draft_to_live_still_works(alice_profile, mock_verify, auth_headers):
    client, _, _ = alice_profile
    created = client.post("/v1/listings", json=_listing_payload(status="draft"),
                          headers=auth_headers).json()
    r = client.patch(f"/v1/listings/{created['id']}", json={"status": "live"},
                     headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "live"


def test_patch_locked_in_terminal_state(alice_profile, mock_verify, auth_headers):
    client, _, _ = alice_profile
    created = client.post("/v1/listings", json=_listing_payload(), headers=auth_headers).json()
    client.post(f"/v1/listings/{created['id']}/cancel", headers=auth_headers)
    r = client.patch(f"/v1/listings/{created['id']}", json={"variety": "x"}, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "listing_locked"


def test_non_owner_cannot_patch(alice_profile, mock_verify):
    client, _, _ = alice_profile
    created = client.post("/v1/listings", json=_listing_payload(),
                          headers={"Authorization": "Bearer good-token"}).json()
    # nophone-token is a different uid
    r = client.patch(f"/v1/listings/{created['id']}", json={"variety": "x"},
                     headers={"Authorization": "Bearer nophone-token"})
    assert r.status_code == 403


def test_cancel_flow(alice_profile, mock_verify, auth_headers):
    client, _, _ = alice_profile
    created = client.post("/v1/listings", json=_listing_payload(), headers=auth_headers).json()
    r = client.post(f"/v1/listings/{created['id']}/cancel", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    # second cancel is an illegal transition
    r = client.post(f"/v1/listings/{created['id']}/cancel", headers=auth_headers)
    assert r.status_code == 422


def test_get_unknown_listing_404(alice_profile, mock_verify, auth_headers):
    client, _, _ = alice_profile
    r = client.get("/v1/listings/00000000-0000-0000-0000-000000000000", headers=auth_headers)
    assert r.status_code == 404


# ------------------------------------------------ sweep

def _sweep(client, secret="s3cret"):
    return client.post("/v1/internal/sweep", headers={"X-Sweep-Secret": secret})


def test_sweep_requires_secret_config(monkeypatch, alice_profile, mem_retention):
    client, _, _ = alice_profile
    monkeypatch.delenv("SWEEP_SECRET", raising=False)
    r = client.post("/v1/internal/sweep", headers={"X-Sweep-Secret": "x"})
    assert r.status_code == 503
    assert r.json()["code"] == "sweep_not_configured"


def test_sweep_rejects_bad_secret(monkeypatch, alice_profile, mem_retention):
    client, _, _ = alice_profile
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    r = _sweep(client, secret="wrong")
    assert r.status_code == 401


def test_sweep_is_auth_exempt_but_secret_gated(monkeypatch, alice_profile, mem_retention):
    client, _, _ = alice_profile  # no Authorization header sent
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    r = client.post("/v1/internal/sweep", headers={"X-Sweep-Secret": "s3cret"})
    assert r.status_code == 200  # no 401 despite no bearer token


def _empty_retention():
    return {"notification_log_purged": 0, "resolved_disputes_purged": 0,
            "terminal_media_gc": 0}


def test_sweep_expires_past_due_and_is_idempotent(monkeypatch, alice_profile, mem_retention,
                                                 mock_verify, auth_headers):
    client, _, lrepo = alice_profile
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    # Disable quiet hours so nudge delivery is deterministic in tests.
    r = client.put("/v1/users/me/notification-prefs",
                   json={"categories": {"harvestAlerts": True, "wantMatches": True,
                                        "expiryNudges": True, "creditWarnings": True,
                                        "bookingReminders": True},
                         "quietHours": None}, headers=auth_headers)
    assert r.status_code == 200, r.text
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    old = lrepo.create({"id": "a1", "owner_uid": "alice", "type": "seedling",
                        "photos": ["https://x/y.jpg"], "credit_cost": 1,
                        "spray_disclosure": "none", "status": "live", "expires_at": past})
    new = lrepo.create({"id": "a2", "owner_uid": "alice", "type": "seedling",
                        "photos": ["https://x/y.jpg"], "credit_cost": 1,
                        "spray_disclosure": "none", "status": "live", "expires_at": future})
    # a2 expires in ~24h -> 48h nudge fires.
    assert _sweep(client).json() == {"expired": 1, "nudged_48h": 1, "nudged_12h": 0,
                                     "retention": _empty_retention()}
    assert lrepo.get("a1")["status"] == "expired"
    assert lrepo.get("a2")["status"] == "live"
    # Idempotent: expiry stays done and the nudge dedupes on its per-mark ref.
    assert _sweep(client).json() == {"expired": 0, "nudged_48h": 0, "nudged_12h": 0,
                                     "retention": _empty_retention()}


def test_sweep_fires_12h_nudge(monkeypatch, alice_profile, mem_retention,
                               mock_verify, auth_headers):
    client, _, lrepo = alice_profile
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    r = client.put("/v1/users/me/notification-prefs",
                   json={"categories": {"harvestAlerts": True, "wantMatches": True,
                                        "expiryNudges": True, "creditWarnings": True,
                                        "bookingReminders": True},
                         "quietHours": None}, headers=auth_headers)
    assert r.status_code == 200, r.text
    soon = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    lrepo.create({"id": "b1", "owner_uid": "alice", "type": "harvest",
                  "photos": ["https://x/y.jpg"], "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live", "expires_at": soon})
    assert _sweep(client).json() == {"expired": 0, "nudged_48h": 0, "nudged_12h": 1,
                                     "retention": _empty_retention()}


def test_naive_datetimes_are_treated_as_utc(alice_profile, mock_verify, auth_headers):
    """Review: naive ISO datetimes must not 500 the naive/aware comparison."""
    client, _, _ = alice_profile
    naive_future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None).isoformat()
    r = client.post("/v1/listings", json=_listing_payload(expires_at=naive_future),
                    headers=auth_headers)
    assert r.status_code == 201, r.text
    assert r.json()["expires_at"].endswith("+00:00")

    naive_past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    r = client.post("/v1/listings", json=_listing_payload(expires_at=naive_past),
                    headers=auth_headers)
    assert r.status_code == 400  # invalid_expiry, not a 500
    assert r.json()["code"] == "invalid_expiry"


# ------------------------------------------------ M1: deterministic geo fuzzing

def _haversine_mi(a, b, c, d):
    p = math.pi / 180
    h = (math.sin((c - a) * p / 2) ** 2
         + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2)
    return 2 * 3959 * math.asin(math.sqrt(h))


def test_fuzz_location_for_listing_is_stable():
    """M1: repeated reads of one listing return the IDENTICAL fuzzed point,
    so averaging reads cannot triangulate the true coordinate."""
    from app.listings import fuzz_location_for_listing

    a = fuzz_location_for_listing(33.4, -111.8, "listing-1")
    b = fuzz_location_for_listing(33.4, -111.8, "listing-1")
    assert a == b


def test_fuzz_location_for_listing_varies_and_stays_in_band():
    from app.listings import fuzz_location_for_listing

    a = fuzz_location_for_listing(33.4, -111.8, "listing-1")
    b = fuzz_location_for_listing(33.4, -111.8, "listing-2")
    assert a != b  # distinct listings get distinct offsets
    for pt in (a, b):
        dist = _haversine_mi(33.4, -111.8, pt[0], pt[1])
        assert 0.04 < dist <= 0.55  # the intended 0.05..0.5 mi band


def test_fuzz_location_for_listing_bound_to_server_secret(monkeypatch):
    """The offset derives from the GEO key: rotating the key moves the point."""
    from cryptography.fernet import Fernet

    from app.listings import fuzz_location_for_listing

    a = fuzz_location_for_listing(33.4, -111.8, "listing-1")
    monkeypatch.setenv("GEO_ENCRYPTION_KEY", Fernet.generate_key().decode())
    b = fuzz_location_for_listing(33.4, -111.8, "listing-1")
    assert a != b


def test_repeated_reads_return_identical_geo(alice_profile, mock_verify, auth_headers):
    """M1 end-to-end: the served point for a listing never moves between reads."""
    client, _, _ = alice_profile
    created = client.post("/v1/listings", json=_listing_payload(), headers=auth_headers).json()
    first = (created["geo_lat"], created["geo_lon"])
    for _ in range(3):
        r = client.get(f"/v1/listings/{created['id']}", headers=auth_headers)
        assert (r.json()["geo_lat"], r.json()["geo_lon"]) == first


# ------------------------------------------------ M5a/M6: harvest completion

class _RecordingConn:
    """Minimal psycopg stand-in: records SQL, returns canned rows."""

    def __init__(self):
        self.queries: list[tuple[str, Any]] = []

    def execute(self, q, params=None):
        self.queries.append((q, params))

        class _Cur:
            rowcount = 1

            def fetchone(_self):
                return {"id": "x"}

            def fetchall(_self):
                return []

        return _Cur()

    def commit(self):
        pass


def test_decrement_remaining_casts_delta_to_numeric():
    """M5a: the delta is cast to NUMERIC so the subtraction never evaluates
    in float8 — float dust would break the fully-picked transition."""
    from app.listings import PostgresListingRepo

    conn = _RecordingConn()
    repo = PostgresListingRepo(conn)
    repo.get = lambda lid: {"id": lid, "remaining_qty": "0"}  # stub the re-read
    repo.decrement_remaining("x", 0.1)
    update_q = next(q for q, _ in conn.queries if "remaining_qty" in q and "UPDATE" in q)
    assert "%s::numeric" in update_q
    assert update_q.count("%s::numeric") == 3  # subtraction, guard, epsilon
    # The guard tolerates tiny negative dust and the SET clamps to zero.
    assert "GREATEST" in update_q


def test_harvest_positive_float_dust_still_completes(alice_profile, mock_verify, auth_headers):
    """M5a end-to-end: 1.0 - 3x(1/3) leaves +1.1e-16, not 0. The epsilon
    compare must complete the listing instead of stranding it live."""
    client, _, _ = alice_profile
    payload = _listing_payload(type="harvest", quantity=1.0, unit="kg", status="live")
    lid = client.post("/v1/listings", json=payload, headers=auth_headers).json()["id"]
    third = 1 / 3
    for _ in range(3):
        r = client.post("/v1/harvest-events",
                        json={"listing_id": lid, "delta_kg": third}, headers=auth_headers)
        assert r.status_code == 201, r.text
    body = r.json()
    assert body["listing"]["status"] == "completed"
    assert body["remaining_kg"] == 0


def test_harvest_negative_float_dust_still_completes(alice_profile, mock_verify, auth_headers):
    """M5a end-to-end: 0.9 - 3x(0.1+0.2) lands at -1.1e-16. The old
    ``>= 0`` guard rejected this legitimate final pick with a 422; the
    epsilon-tolerant guard accepts it, clamps to zero, and completes."""
    client, _, _ = alice_profile
    payload = _listing_payload(type="harvest", quantity=0.9, unit="kg", status="live")
    lid = client.post("/v1/listings", json=payload, headers=auth_headers).json()["id"]
    delta = 0.1 + 0.2
    for _ in range(3):
        r = client.post("/v1/harvest-events",
                        json={"listing_id": lid, "delta_kg": delta}, headers=auth_headers)
        assert r.status_code == 201, r.text
    body = r.json()
    assert body["listing"]["status"] == "completed"
    assert body["remaining_kg"] == 0


def test_harvest_real_shortfall_still_rejected(alice_profile, mock_verify, auth_headers):
    """M5a: the epsilon only forgives dust — a genuine overpick is still
    a 422 and the listing stays live with its quantity intact."""
    client, _, _ = alice_profile
    payload = _listing_payload(type="harvest", quantity=1.0, unit="kg", status="live")
    lid = client.post("/v1/listings", json=payload, headers=auth_headers).json()["id"]
    r = client.post("/v1/harvest-events",
                    json={"listing_id": lid, "delta_kg": 1.5}, headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "insufficient_quantity"
    r = client.get(f"/v1/listings/{lid}", headers=auth_headers)
    assert r.json()["status"] == "live"
    assert r.json()["remaining_qty"] == 1.0


def test_complete_if_live_is_single_conditional_flip():
    """M6: complete_if_live flips live->completed atomically; a non-live
    listing (or a lost race) yields None instead of stranding state."""
    from app.listings import MemoryListingRepo

    repo = MemoryListingRepo()
    repo._rows["l1"] = {"id": "l1", "status": "live"}
    repo._rows["l2"] = {"id": "l2", "status": "cancelled"}
    assert repo.complete_if_live("l1")["status"] == "completed"
    assert repo.complete_if_live("l1") is None  # already completed: loser gets None
    assert repo.complete_if_live("l2") is None
    assert repo.complete_if_live("nope") is None


def test_harvest_completion_never_walks_two_commits(alice_profile, mock_verify, auth_headers):
    """M6 end-to-end: fully picking a harvest completes it via the single
    conditional flip — set_status is never used for the completion walk."""
    client, _, lrepo = alice_profile
    calls = []
    orig = lrepo.set_status

    def spy(lid, status):
        calls.append(status)
        return orig(lid, status)

    lrepo.set_status = spy
    try:
        payload = _listing_payload(type="harvest", quantity=2, unit="kg", status="live")
        lid = client.post("/v1/listings", json=payload, headers=auth_headers).json()["id"]
        r = client.post("/v1/harvest-events", json={"listing_id": lid, "delta_kg": 2},
                        headers=auth_headers)
        assert r.status_code == 201, r.text
        assert r.json()["listing"]["status"] == "completed"
    finally:
        lrepo.set_status = orig
    assert "claimed" not in calls and "completed" not in calls


# ------------------------------------------------ M9: bounded hot paths (listing side)

def _live_row(i, now, **kw):
    row = {"id": f"p{i}", "owner_uid": "alice", "type": "seedling",
           "photos": ["https://x/y.jpg"], "credit_cost": 1,
           "spray_disclosure": "none", "status": "live",
           "expires_at": (now + timedelta(days=i + 1)).isoformat()}
    row.update(kw)
    return row


def test_list_live_pagination_and_count(alice_profile):
    """M9: DB-level pagination — the feed scores one page, not the table."""
    _, _, lrepo = alice_profile
    now = datetime.now(timezone.utc)
    for i in range(3):
        lrepo.create(_live_row(i, now))
    page1 = lrepo.list_live(limit=2, offset=0)
    assert [r["id"] for r in page1] == ["p0", "p1"]  # soonest expiry first
    page2 = lrepo.list_live(limit=2, offset=1)
    assert [r["id"] for r in page2] == ["p1", "p2"]
    assert lrepo.count_live() == 3
    assert lrepo.count_live(listing_type="harvest") == 0
    assert len(lrepo.list_live(limit=10, listing_type="seedling")) == 3
    # Back-compat: unbounded call still works.
    assert len(lrepo.list_live()) == 3


def test_list_live_expiring_before_bounds_nudge_scan(alice_profile):
    """M9: the sweep's nudge scan only sees listings expiring before cutoff."""
    _, _, lrepo = alice_profile
    now = datetime.now(timezone.utc)
    lrepo.create(_live_row(0, now, id="soon",
                           expires_at=(now + timedelta(hours=1)).isoformat()))
    lrepo.create(_live_row(1, now, id="later",
                           expires_at=(now + timedelta(days=30)).isoformat()))
    lrepo.create(_live_row(2, now, id="noexp", expires_at=None))
    got = lrepo.list_live_expiring_before(now + timedelta(hours=48))
    assert [r["id"] for r in got] == ["soon"]


# ------------------------------------------------ M19: retention

def test_retention_purges_old_notification_log(mem_listings, mem_retention):
    from app.listings import utcnow

    _, _, _, _, nrepo = mem_listings
    rrepo = mem_retention
    nrepo._log.append({"user_uid": "alice", "category": "match", "ref": "old",
                       "outcome": "sent", "sent_at": utcnow() - timedelta(days=100)})
    nrepo._log.append({"user_uid": "alice", "category": "match", "ref": "new",
                       "outcome": "sent", "sent_at": utcnow()})
    assert rrepo.purge_notification_log(90) == 1
    assert [e["ref"] for e in nrepo._log] == ["new"]


def test_retention_purges_old_resolved_disputes(mem_retention):
    from app.listings import utcnow

    rrepo = mem_retention
    rrepo._disputes.extend([
        {"id": "d1", "status": "resolved",
         "resolved_at": (utcnow() - timedelta(days=800)).isoformat()},
        {"id": "d2", "status": "resolved", "resolved_at": utcnow().isoformat()},
        {"id": "d3", "status": "open", "resolved_at": None},
    ])
    assert rrepo.purge_resolved_disputes(730) == 1
    assert [d["id"] for d in rrepo._disputes] == ["d2", "d3"]


def test_retention_gc_terminal_listing_media(mem_listings, mem_retention):
    from app.listings import utcnow

    _, _, lrepo, _, _ = mem_listings
    rrepo = mem_retention
    old = (utcnow() - timedelta(days=200)).isoformat()
    rrepo._uploads["k1"] = {"key": "k1", "finalized": True}
    rrepo._uploads["k2"] = {"key": "k2", "finalized": True}
    lrepo._rows["t1"] = {"id": "t1", "status": "completed", "created_at": old,
                         "photos": ["https://cdn.example/v1/uploads/public/k1",
                                    "https://external.example/pic.jpg"]}
    lrepo._rows["t2"] = {"id": "t2", "status": "live", "created_at": old,
                         "photos": ["https://cdn.example/v1/uploads/public/k2"]}
    assert rrepo.gc_terminal_listing_media(180) == 1
    assert "k1" not in rrepo._uploads  # terminal + old: registry row gone
    assert "k2" in rrepo._uploads  # live listing: untouched
    assert lrepo._rows["t1"]["photos"] == []
    assert lrepo._rows["t2"]["photos"] != []


def test_sweep_reports_retention_counts(monkeypatch, alice_profile, mem_retention):
    client, _, _ = alice_profile
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    body = _sweep(client).json()
    assert body["retention"] == {"notification_log_purged": 0,
                                 "resolved_disputes_purged": 0,
                                 "terminal_media_gc": 0}


# ------------------------------------------------ L1a: update column whitelist

def test_listing_update_rejects_unknown_column():
    """L1a: update keys are whitelisted — an unknown column never reaches SQL."""
    from app.listings import PostgresListingRepo

    class _NoExecConn(_RecordingConn):
        def execute(self, q, params=None):
            raise AssertionError("must not reach SQL")

    with pytest.raises(ValueError, match="unknown listing columns"):
        PostgresListingRepo(_NoExecConn()).update("x", {"variety": "ok",
                                                        "owner_uid": "mallory"})


def test_listing_update_allows_whitelisted_columns():
    from app.listings import PostgresListingRepo

    conn = _RecordingConn()
    repo = PostgresListingRepo(conn)
    repo.get = lambda lid: {"id": lid}
    repo.update("x", {"variety": "tomato", "remaining_qty": 3})
    update_q = next(q for q, _ in conn.queries if q.startswith("UPDATE"))
    assert "variety = %s" in update_q
    assert "remaining_qty = %s" in update_q


# ------------------------------------------------ L8: claimer_uid visibility

def _claimed_row():
    return {"id": "l1", "owner_uid": "alice", "type": "seedling", "photos": [],
            "variety": "tomato", "quantity": None, "unit": None, "credit_cost": 1,
            "pickup_window": None, "expires_at": None, "geo_lat": None, "geo_lon": None,
            "spray_disclosure": "none", "status": "claimed", "created_at": None,
            "remaining_qty": None, "visit_rules": None, "claimer_uid": "bob"}


def test_public_listing_claimer_uid_owner_claimer_only():
    """L8: claimer_uid is revealed only to the owner or the claimer."""
    from app.listings import public_listing

    row = _claimed_row()
    # viewer_uid=None = participant/internal context (claim/exchange routes,
    # where the viewer is always owner or claimer): revealed.
    assert public_listing(row)["claimer_uid"] == "bob"
    assert public_listing(row, viewer_uid="mallory")["claimer_uid"] is None  # third party
    assert public_listing(row, viewer_uid="alice")["claimer_uid"] == "bob"  # owner
    assert public_listing(row, viewer_uid="bob")["claimer_uid"] == "bob"  # claimer


def test_claimer_uid_hidden_from_third_party(alice_profile, mock_verify, auth_headers):
    """L8 end-to-end: an authenticated third party no longer sees claimer_uid
    on the listing detail; the owner still sees it on their own routes."""
    client, _, lrepo = alice_profile
    lid = client.post("/v1/listings", json=_listing_payload(), headers=auth_headers).json()["id"]
    lrepo.claim(lid, "bob")  # repo-level claim, as the exchange flow would do

    # Third-party authenticated viewer: hidden.
    other = client.get(f"/v1/listings/{lid}", headers={"Authorization": "Bearer nophone-token"})
    assert other.status_code == 200
    assert other.json()["claimer_uid"] is None

    # Owner sees the claimer on the cancel response (owner route).
    r = client.post(f"/v1/listings/{lid}/cancel", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["claimer_uid"] == "bob"


def test_repo_implementations_cover_listing_protocol():
    """Structural guard: every ListingRepo protocol member must exist on
    both the Postgres and memory implementations and on the caching
    decorator. Catches an accidentally deleted method (like the count_live
    regression that 500'd /v1/feed) without needing a live Postgres."""
    import typing

    from app.cache import CachedListingRepo
    from app.listings import (
        ListingRepo,
        MemoryListingRepo,
        PostgresListingRepo,
    )

    # Protocol members, minus dunders and typing internals.
    members = {
        name for name in dir(ListingRepo)
        if not name.startswith("_") or name == "__init__"
    }
    members = {m for m in members if not m.startswith("__")}
    assert "count_live" in members and "list_live_ranked" in members
    for impl in (MemoryListingRepo, PostgresListingRepo, CachedListingRepo):
        missing = sorted(m for m in members if not hasattr(impl, m))
        assert not missing, f"{impl.__name__} is missing {missing}"
