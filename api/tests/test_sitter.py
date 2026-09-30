"""API-070: sitter profiles, request lifecycle, reviews."""
from __future__ import annotations

import pytest


@pytest.fixture()
def mem_sitting(client, monkeypatch):
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
        if token == "mallory-token":
            return {"uid": "mallory"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)
    return client, urepo, srepo


ALICE = {"Authorization": "Bearer good-token"}
BOB = {"Authorization": "Bearer bob-token"}
MALLORY = {"Authorization": "Bearer mallory-token"}


def _profile(client, headers, name):
    r = client.post("/v1/users", json={"display_name": name, "age_attestation": True}, headers=headers)
    assert r.status_code == 200, r.text


def _sitter(client, headers, **kw):
    base = {"bio": "20 tomato seasons", "experience_years": 5,
            "service_radius_miles": 10, "active": True}
    base.update(kw)
    r = client.put("/v1/sitters/me", json=base, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _request(client, headers, sitter_uid="bob"):
    r = client.post("/v1/sitting-requests", json={
        "sitter_uid": sitter_uid, "plant_count": 12,
        "start_date": "2026-10-10", "end_date": "2026-10-15",
        "notes": "Water the tomatoes daily.",
    }, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_sitter_profile_needs_user_profile(mem_sitting):
    client, _, _ = mem_sitting
    r = client.put("/v1/sitters/me", json={"bio": "x"}, headers=BOB)
    assert r.status_code == 400
    assert r.json()["code"] == "profile_required"


def test_sitter_directory_lists_active_only(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    _profile(client, MALLORY, "Mallory")
    _sitter(client, BOB, bio="Tomato whisperer")
    _sitter(client, MALLORY, active=False)

    body = client.get("/v1/sitters", headers=ALICE).json()
    assert [s["uid"] for s in body["sitters"]] == ["bob"]
    assert body["sitters"][0]["display_name"] == "Bob"
    assert body["sitters"][0]["bio"] == "Tomato whisperer"


def test_full_sitting_lifecycle_with_review(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    _sitter(client, BOB)

    req = _request(client, ALICE)
    assert req["status"] == "requested"

    r = client.post(f"/v1/sitting-requests/{req['id']}/accept", headers=BOB)
    assert r.json()["status"] == "accepted"

    r = client.post(f"/v1/sitting-requests/{req['id']}/complete", headers=ALICE)
    assert r.json()["status"] == "completed"

    r = client.post(f"/v1/sitting-requests/{req['id']}/reviews",
                    json={"rating": 5, "comment": "Plants thrived!"}, headers=ALICE)
    assert r.status_code == 201, r.text
    assert r.json()["rating"] == 5

    body = client.get("/v1/sitters/bob/reviews", headers=ALICE).json()
    assert len(body["reviews"]) == 1
    assert body["reviews"][0]["comment"] == "Plants thrived!"


def test_sitter_reviews_latest_first(mem_sitting):
    # Reviews on the public list come back newest first.
    client, _, srepo = mem_sitting
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    _sitter(client, BOB)

    first = _request(client, ALICE)
    client.post(f"/v1/sitting-requests/{first['id']}/accept", headers=BOB)
    client.post(f"/v1/sitting-requests/{first['id']}/complete", headers=BOB)
    r1 = client.post(f"/v1/sitting-requests/{first['id']}/reviews",
                     json={"rating": 4, "comment": "Older"}, headers=ALICE)
    assert r1.status_code == 201, r1.text

    second = _request(client, ALICE)
    client.post(f"/v1/sitting-requests/{second['id']}/accept", headers=BOB)
    client.post(f"/v1/sitting-requests/{second['id']}/complete", headers=BOB)
    r2 = client.post(f"/v1/sitting-requests/{second['id']}/reviews",
                     json={"rating": 5, "comment": "Newer"}, headers=ALICE)
    assert r2.status_code == 201, r2.text

    # Both reviews are created within the same test tick, so pin the first
    # one to an older timestamp to force a known ordering.
    srepo._reviews[r1.json()["id"]]["created_at"] = "2026-01-01T00:00:00+00:00"

    body = client.get("/v1/sitters/bob/reviews", headers=ALICE).json()
    assert [r["id"] for r in body["reviews"]] == [r2.json()["id"], r1.json()["id"]]
    assert [r["comment"] for r in body["reviews"]] == ["Newer", "Older"]


def test_request_rules(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    _profile(client, MALLORY, "Mallory")

    # Cannot request yourself.
    r = client.post("/v1/sitting-requests", json={
        "sitter_uid": "alice", "plant_count": 3,
        "start_date": "2026-10-10", "end_date": "2026-10-12"}, headers=ALICE)
    assert r.status_code == 422
    assert r.json()["code"] == "cannot_request_self"

    # Sitter must be active.
    _sitter(client, MALLORY, active=False)
    r = client.post("/v1/sitting-requests", json={
        "sitter_uid": "mallory", "plant_count": 3,
        "start_date": "2026-10-10", "end_date": "2026-10-12"}, headers=ALICE)
    assert r.status_code == 422
    assert r.json()["code"] == "sitter_unavailable"

    # Bad dates.
    _sitter(client, BOB)
    r = client.post("/v1/sitting-requests", json={
        "sitter_uid": "bob", "plant_count": 3,
        "start_date": "2026-10-12", "end_date": "2026-10-10"}, headers=ALICE)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_dates"


def test_lifecycle_permissions_and_transitions(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    _profile(client, MALLORY, "Mallory")
    _sitter(client, BOB)

    req = _request(client, ALICE)

    # Only the sitter accepts/declines.
    r = client.post(f"/v1/sitting-requests/{req['id']}/accept", headers=MALLORY)
    assert r.status_code == 403
    r = client.post(f"/v1/sitting-requests/{req['id']}/decline", headers=ALICE)
    assert r.status_code == 403

    # Decline path ends the request.
    r = client.post(f"/v1/sitting-requests/{req['id']}/decline", headers=BOB)
    assert r.json()["status"] == "declined"
    r = client.post(f"/v1/sitting-requests/{req['id']}/accept", headers=BOB)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_transition"

    # Owner cancels a pending request.
    req2 = _request(client, ALICE)
    r = client.post(f"/v1/sitting-requests/{req2['id']}/cancel", headers=BOB)
    assert r.status_code == 403
    r = client.post(f"/v1/sitting-requests/{req2['id']}/cancel", headers=ALICE)
    assert r.json()["status"] == "cancelled"


def test_review_rules(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    _profile(client, MALLORY, "Mallory")
    _sitter(client, BOB)

    req = _request(client, ALICE)
    client.post(f"/v1/sitting-requests/{req['id']}/accept", headers=BOB)

    # No review before completion.
    r = client.post(f"/v1/sitting-requests/{req['id']}/reviews",
                    json={"rating": 5}, headers=ALICE)
    assert r.status_code == 422
    assert r.json()["code"] == "sitting_not_completed"

    client.post(f"/v1/sitting-requests/{req['id']}/complete", headers=BOB)

    # Only the owner reviews.
    r = client.post(f"/v1/sitting-requests/{req['id']}/reviews",
                    json={"rating": 4}, headers=MALLORY)
    assert r.status_code == 403

    r = client.post(f"/v1/sitting-requests/{req['id']}/reviews",
                    json={"rating": 5, "comment": "Great"}, headers=ALICE)
    assert r.status_code == 201

    # Exactly one review per sitting.
    r = client.post(f"/v1/sitting-requests/{req['id']}/reviews",
                    json={"rating": 1}, headers=ALICE)
    assert r.status_code == 409
    assert r.json()["code"] == "duplicate_review"

    # Rating bounds enforced by validation.
    req2 = _request(client, ALICE)
    client.post(f"/v1/sitting-requests/{req2['id']}/accept", headers=BOB)
    client.post(f"/v1/sitting-requests/{req2['id']}/complete", headers=BOB)
    r = client.post(f"/v1/sitting-requests/{req2['id']}/reviews",
                    json={"rating": 6}, headers=ALICE)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# W4 fixes: M12 date validation, L6 radius alignment + directory paging
# ---------------------------------------------------------------------------

def test_sitting_request_rejects_impossible_date(mem_sitting):
    # M12: "2026-13-45" used to reach the DATE column and 500; now -> 422.
    client, _, _ = mem_sitting
    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    _sitter(client, BOB)

    for bad in ("2026-13-45", "2026-02-30", "10/10/2026", "not-a-date"):
        r = client.post("/v1/sitting-requests", json={
            "sitter_uid": "bob", "plant_count": 3,
            "start_date": bad, "end_date": "2026-10-12"}, headers=ALICE)
        assert r.status_code == 422, (bad, r.text)

    # A real date still works.
    r = client.post("/v1/sitting-requests", json={
        "sitter_uid": "bob", "plant_count": 3,
        "start_date": "2026-10-10", "end_date": "2026-10-12"}, headers=ALICE)
    assert r.status_code == 201, r.text
    assert r.json()["start_date"] == "2026-10-10"


def test_service_radius_quantized_to_float32(mem_sitting):
    # L6: the column is REAL, so the model quantizes to float32 on the way in.
    import struct
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    body = _sitter(client, BOB, service_radius_miles=0.1 + 0.2)
    expected = struct.unpack("f", struct.pack("f", 0.1 + 0.2))[0]
    assert body["service_radius_miles"] == expected
    assert body["service_radius_miles"] != 0.1 + 0.2  # would be the raw float64


def test_sitter_directory_pagination_bound(mem_sitting):
    # L6: unbounded list_active is gone — limit is capped, offset pages.
    client, _, _ = mem_sitting
    for headers, name in ((ALICE, "Alice"), (BOB, "Bob"), (MALLORY, "Mallory")):
        _profile(client, headers, name)
        _sitter(client, headers)

    body = client.get("/v1/sitters?limit=2", headers=ALICE).json()
    assert len(body["sitters"]) == 2
    body = client.get("/v1/sitters?limit=2&offset=2", headers=ALICE).json()
    assert len(body["sitters"]) == 1

    r = client.get("/v1/sitters?limit=1000", headers=ALICE)
    assert r.status_code == 422  # over the 500 cap


def test_sitter_rate_credits_round_trip(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    body = _sitter(client, BOB, rate_amount=5, rate_unit="credits")
    assert body["rate_amount"] == 5
    assert body["rate_unit"] == "credits"

    listed = client.get("/v1/sitters", headers=ALICE).json()["sitters"]
    assert listed[0]["rate_amount"] == 5
    assert listed[0]["rate_unit"] == "credits"

    single = client.get("/v1/sitters/bob", headers=ALICE).json()
    assert single["rate_amount"] == 5
    assert single["rate_unit"] == "credits"


def test_sitter_rate_usd_round_trip(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    body = _sitter(client, BOB, rate_amount=12.5, rate_unit="usd")
    assert body["rate_amount"] == 12.5
    assert body["rate_unit"] == "usd"


def test_sitter_rate_absent_by_default(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    body = _sitter(client, BOB)
    assert body["rate_amount"] is None
    assert body["rate_unit"] is None


def test_sitter_rate_validation(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    # amount without unit, unit without amount, bad unit
    for bad in ({"rate_amount": 5}, {"rate_unit": "credits"},
                {"rate_amount": 5, "rate_unit": "eur"}):
        r = client.put("/v1/sitters/me", json=bad, headers=BOB)
        assert r.status_code == 422, (bad, r.text)
    # fractional credits rejected (ledger is integer)
    r = client.put("/v1/sitters/me",
                   json={"rate_amount": 2.5, "rate_unit": "credits"}, headers=BOB)
    assert r.status_code == 422, r.text
    # sub-cent usd rejected (NUMERIC(10,2))
    r = client.put("/v1/sitters/me",
                   json={"rate_amount": 10.999, "rate_unit": "usd"}, headers=BOB)
    assert r.status_code == 422, r.text
    # negative rejected
    r = client.put("/v1/sitters/me",
                   json={"rate_amount": -1, "rate_unit": "usd"}, headers=BOB)
    assert r.status_code == 422, r.text


def test_sitter_services_round_trip(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    body = _sitter(client, BOB, services=["watering", "repotting"])
    assert body["services"] == ["watering", "repotting"]
    single = client.get("/v1/sitters/bob", headers=ALICE).json()
    assert single["services"] == ["watering", "repotting"]
    listed = client.get("/v1/sitters", headers=ALICE).json()["sitters"]
    assert listed[0]["services"] == ["watering", "repotting"]


def test_sitter_services_default_empty(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    body = _sitter(client, BOB)
    assert body["services"] == []


def test_sitter_services_reject_unknown(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    r = client.put("/v1/sitters/me", json={"services": ["watering", "teleportation"]},
                   headers=BOB)
    assert r.status_code == 422, r.text


def test_sitter_services_deduped(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    body = _sitter(client, BOB, services=["watering", "watering", "repotting"])
    assert body["services"] == ["watering", "repotting"]


def test_sitter_availability_round_trip(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    _sitter(client, BOB)
    from datetime import date, timedelta
    d1 = (date.today() + timedelta(days=3)).isoformat()
    d2 = (date.today() + timedelta(days=5)).isoformat()
    r = client.put("/v1/sitters/me/availability",
                   json={"available_dates": [d2, d1]}, headers=BOB)
    assert r.status_code == 200, r.text
    assert r.json()["available_dates"] == sorted([d1, d2])
    single = client.get("/v1/sitters/bob", headers=ALICE).json()
    assert single["available_dates"] == sorted([d1, d2])
    listed = client.get("/v1/sitters", headers=ALICE).json()["sitters"]
    assert listed[0]["available_dates"] == sorted([d1, d2])


def test_sitter_availability_replaces(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    _sitter(client, BOB)
    from datetime import date, timedelta
    d1 = (date.today() + timedelta(days=3)).isoformat()
    d2 = (date.today() + timedelta(days=5)).isoformat()
    client.put("/v1/sitters/me/availability",
               json={"available_dates": [d1, d2]}, headers=BOB)
    r = client.put("/v1/sitters/me/availability",
                   json={"available_dates": [d2]}, headers=BOB)
    assert r.json()["available_dates"] == [d2]
    r = client.put("/v1/sitters/me/availability",
                   json={"available_dates": []}, headers=BOB)
    assert r.json()["available_dates"] == []


def test_sitter_availability_validation(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    _sitter(client, BOB)
    from datetime import date, timedelta
    past = (date.today() - timedelta(days=1)).isoformat()
    r = client.put("/v1/sitters/me/availability",
                   json={"available_dates": [past]}, headers=BOB)
    assert r.status_code == 422, r.text
    r = client.put("/v1/sitters/me/availability",
                   json={"available_dates": ["not-a-date"]}, headers=BOB)
    assert r.status_code == 422, r.text


def test_sitter_availability_requires_sitter_profile(mem_sitting):
    client, _, _ = mem_sitting
    _profile(client, BOB, "Bob")
    from datetime import date, timedelta
    d1 = (date.today() + timedelta(days=3)).isoformat()
    r = client.put("/v1/sitters/me/availability",
                   json={"available_dates": [d1]}, headers=BOB)
    assert r.status_code == 404
    assert r.json()["code"] == "sitter_not_found"
