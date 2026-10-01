"""R2 credit economy: seasonal expiry, expiry warnings, earn cap, starter credits."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException


def _dt(y, m, d, hh=0, mi=0):
    return datetime(y, m, d, hh, mi, tzinfo=timezone.utc)


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _season_end_ms(y, m, d):
    """Expected season end: last millisecond of the given day (UTC)."""
    return _ms(datetime(y, m, d, 23, 59, 59, 999000, tzinfo=timezone.utc))


@pytest.fixture()
def frozen_time(monkeypatch):
    """Freeze credits._utcnow at a chosen instant (controls created_at,
    balance(), and the endpoint clock)."""
    from app import credits as credits_mod

    def freeze(dt):
        monkeypatch.setattr(credits_mod, "_utcnow", lambda: dt)

    return freeze


@pytest.fixture()
def mem_economy(client, monkeypatch):
    """In-memory user + listing + credit repos; alice/bob/mallory tokens."""
    from app import credits as credits_mod
    from app import claims as claims_mod
    from app import listings as listings_mod
    from app import moderation as moderation_mod
    from app import notify as notify_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from conftest import wire_credit_repo
    from conftest import wire_images_repo
    import app.auth as auth_mod

    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    crepo = wire_credit_repo(client)
    wire_images_repo(client)
    wrepo = wantlist_mod.MemoryWantRepo()
    nrepo = notify_mod.MemoryNotificationRepo()
    claim_repo = claims_mod.MemoryClaimRepo()
    mrepo = moderation_mod.MemoryModerationRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wrepo
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: nrepo
    client.app.dependency_overrides[claims_mod.get_claim_repo] = lambda: claim_repo
    client.app.dependency_overrides[moderation_mod.get_moderation_repo] = lambda: mrepo

    def fake(token: str) -> dict:
        if token == "good-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "bob-token":
            return {"uid": "bob"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)
    assert credits_mod.router is not None  # route registered on the app
    return client, urepo, lrepo, crepo


ALICE = {"Authorization": "Bearer good-token"}
BOB = {"Authorization": "Bearer bob-token"}


def _profile(client, headers, name):
    r = client.post("/v1/users", json={"display_name": name, "age_attestation": True}, headers=headers)
    assert r.status_code == 200, r.text


def _listing_payload(cost=2):
    return {
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


# ---------------------------------------------------------------------------
# season_end_ms boundary cases
# ---------------------------------------------------------------------------

def test_season_end_ms_boundaries():
    from app.credits import season_end_ms

    # Oct–Feb season containing Feb 28 (non-leap): ends that same day.
    assert season_end_ms(_ms(_dt(2026, 2, 28, 12))) == _season_end_ms(2026, 2, 28)
    assert season_end_ms(_ms(_dt(2026, 1, 5))) == _season_end_ms(2026, 2, 28)
    # Leap year: Feb 29 exists and is the season end.
    assert season_end_ms(_ms(_dt(2024, 2, 29, 12))) == _season_end_ms(2024, 2, 29)
    assert season_end_ms(_ms(_dt(2024, 2, 1))) == _season_end_ms(2024, 2, 29)
    # Mar–Sep season boundaries.
    assert season_end_ms(_ms(_dt(2026, 3, 1))) == _season_end_ms(2026, 9, 30)
    assert season_end_ms(_ms(_dt(2026, 6, 15))) == _season_end_ms(2026, 9, 30)
    assert season_end_ms(_ms(_dt(2026, 9, 30, 8))) == _season_end_ms(2026, 9, 30)
    # Oct–Dec: season ends next February.
    assert season_end_ms(_ms(_dt(2026, 10, 1))) == _season_end_ms(2027, 2, 28)
    assert season_end_ms(_ms(_dt(2026, 12, 25))) == _season_end_ms(2027, 2, 28)
    # Oct 2024 -> Feb 2025 (non-leap); Dec 2023 -> Feb 2024 (leap).
    assert season_end_ms(_ms(_dt(2024, 10, 5))) == _season_end_ms(2025, 2, 28)
    assert season_end_ms(_ms(_dt(2023, 12, 5))) == _season_end_ms(2024, 2, 29)


def test_season_end_ms_exact_edges():
    from app.credits import season_end_ms

    # First ms of a season and last ms of the previous season agree.
    assert season_end_ms(_ms(_dt(2026, 3, 1))) == _season_end_ms(2026, 9, 30)
    assert season_end_ms(_season_end_ms(2026, 2, 28)) == _season_end_ms(2026, 2, 28)
    # The returned instant is the final millisecond of the day.
    end = season_end_ms(_ms(_dt(2026, 5, 5)))
    assert datetime.fromtimestamp(end / 1000, tz=timezone.utc) == \
        datetime(2026, 9, 30, 23, 59, 59, 999000, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# expiry_warnings tranche logic
# ---------------------------------------------------------------------------

def test_expiry_warnings_windows(mem_economy, frozen_time):
    from app.credits import expiry_warnings
    _, _, _, crepo = mem_economy

    frozen_time(_dt(2026, 9, 1))   # Mar–Sep season -> expires 2026-09-30
    crepo.add_entry("alice", 4, "exchange_earn", ref_id="r1")
    frozen_time(_dt(2026, 10, 5))  # Oct–Feb season -> expires 2027-02-28
    crepo.add_entry("alice", 6, "exchange_earn", ref_id="r2")

    sep_end = _season_end_ms(2026, 9, 30)

    # 5 days before season end: inside both warning windows.
    w = expiry_warnings(crepo.entries("alice"), _ms(_dt(2026, 9, 25, 12)))
    assert w["warn_30d"] == [{"credits": 4, "expiresAtMs": sep_end}]
    assert w["warn_7d"] == [{"credits": 4, "expiresAtMs": sep_end}]

    # 20 days before: 30-day window only.
    w = expiry_warnings(crepo.entries("alice"), _ms(_dt(2026, 9, 10, 12)))
    assert w["warn_30d"] == [{"credits": 4, "expiresAtMs": sep_end}]
    assert w["warn_7d"] == []

    # 60 days before: no warnings, nothing expired.
    w = expiry_warnings(crepo.entries("alice"), _ms(_dt(2026, 8, 1)))
    assert w == {"warn_30d": [], "warn_7d": []}

    # After season end: the Sep tranche is gone (not listed as expiring).
    w = expiry_warnings(crepo.entries("alice"), _ms(_dt(2026, 10, 1)))
    assert w == {"warn_30d": [], "warn_7d": []}


def test_expiry_warnings_fifo_spend(mem_economy, frozen_time):
    from app.credits import expiry_warnings
    _, _, _, crepo = mem_economy

    frozen_time(_dt(2026, 4, 1))
    crepo.add_entry("alice", 5, "exchange_earn", ref_id="r1")
    frozen_time(_dt(2026, 4, 2))
    crepo.add_entry("alice", 3, "exchange_earn", ref_id="r2")
    frozen_time(_dt(2026, 4, 3))
    crepo.add_entry("alice", -6, "exchange_spend", ref_id="r3")

    # FIFO: the spend eats the oldest lot first; 2 remain, expiring 2026-09-30.
    w = expiry_warnings(crepo.entries("alice"), _ms(_dt(2026, 9, 25)))
    assert w["warn_30d"] == [{"credits": 2, "expiresAtMs": _season_end_ms(2026, 9, 30)}]


# ---------------------------------------------------------------------------
# GET /v1/users/me/credit-expiry
# ---------------------------------------------------------------------------

def test_credit_expiry_endpoint_shape_and_tranches(mem_economy, frozen_time):
    client, _, _, crepo = mem_economy

    frozen_time(_dt(2026, 4, 1))
    _profile(client, ALICE, "Alice")  # +3 starter, expires 2026-09-30
    crepo.add_entry("alice", 2, "exchange_earn", ref_id="r1")  # expires 2026-09-30

    frozen_time(_dt(2026, 9, 25, 12))
    body = client.get("/v1/users/me/credit-expiry", headers=ALICE).json()
    assert set(body) == {"balance", "expiring", "seasonEndMs"}
    assert body["balance"] == 5
    assert body["expiring"] == [
        {"credits": 5, "expiresAtMs": _season_end_ms(2026, 9, 30)}
    ]
    assert body["seasonEndMs"] == _season_end_ms(2026, 9, 30)


def test_credit_expiry_endpoint_far_from_season_end(mem_economy, frozen_time):
    client, _, _, _ = mem_economy

    frozen_time(_dt(2026, 4, 1))
    _profile(client, ALICE, "Alice")

    # June: season end (Sep 30) is >30 days out -> no expiring tranches.
    frozen_time(_dt(2026, 6, 1, 12))
    body = client.get("/v1/users/me/credit-expiry", headers=ALICE).json()
    assert body["balance"] == 3
    assert body["expiring"] == []
    assert body["seasonEndMs"] == _season_end_ms(2026, 9, 30)


def test_credit_expiry_requires_auth(mem_economy):
    client, _, _, _ = mem_economy
    r = client.get("/v1/users/me/credit-expiry")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Earn cap (enforced in the add_entry choke point)
# ---------------------------------------------------------------------------

def test_earn_cap_blocks_11th_credit(mem_economy, frozen_time):
    from app.credits import EarnCapExceededError
    _, _, _, crepo = mem_economy

    frozen_time(_dt(2026, 5, 1))
    for i in range(10):
        crepo.add_entry("alice", 1, "exchange_earn", idempotency_key=f"earn{i}")

    with pytest.raises(EarnCapExceededError) as ei:
        crepo.add_entry("alice", 1, "exchange_earn", idempotency_key="earn10")
    assert isinstance(ei.value, HTTPException)
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "earn_cap_exceeded"

    # A single grant that would cross the cap is blocked too.
    with pytest.raises(EarnCapExceededError):
        crepo.add_entry("alice", 11, "exchange_earn", idempotency_key="big")


def test_earn_cap_resets_after_window(mem_economy, frozen_time):
    _, _, _, crepo = mem_economy

    frozen_time(_dt(2026, 5, 1))
    for i in range(10):
        crepo.add_entry("alice", 1, "exchange_earn", idempotency_key=f"w{i}")

    # 8 days later the rolling window no longer covers the earlier earns.
    frozen_time(_dt(2026, 5, 9, 0, 0))
    crepo.add_entry("alice", 1, "exchange_earn", idempotency_key="w10")
    assert crepo.balance("alice") >= 11


def test_earn_cap_counts_gross_earned_not_net(mem_economy, frozen_time):
    from app.credits import EarnCapExceededError
    _, _, _, crepo = mem_economy

    frozen_time(_dt(2026, 5, 1))
    for i in range(10):
        crepo.add_entry("alice", 1, "exchange_earn", idempotency_key=f"g{i}")
    # Spending does not free up earn capacity (anti-gaming: earn-then-dump).
    crepo.add_entry("alice", -9, "exchange_spend", idempotency_key="spend1")
    with pytest.raises(EarnCapExceededError):
        crepo.add_entry("alice", 1, "exchange_earn", idempotency_key="g10")


def test_earn_cap_exempts_starter_and_replays(mem_economy, frozen_time):
    from app import credits as credits_mod
    _, _, _, crepo = mem_economy

    frozen_time(_dt(2026, 5, 1))
    for i in range(10):
        crepo.add_entry("alice", 1, "exchange_earn", idempotency_key=f"s{i}")

    # Starter bootstrap is not "earned": never blocked by the cap.
    credits_mod.ensure_starter_credits("alice", crepo)
    assert any(e["reason"] == "starter" for e in crepo.entries("alice"))

    # Idempotent replay of an existing key is not new issuance: no cap hit.
    row = crepo.add_entry("alice", 1, "exchange_earn", idempotency_key="s0")
    assert row["idempotency_key"] == "s0"


def test_earn_cap_surfaces_as_409_through_exchange_confirm(mem_economy, frozen_time):
    """The cap lives in the choke point, so the (untouched) exchange confirm
    path surfaces it as a clean 409 envelope."""
    client, _, _, crepo = mem_economy
    frozen_time(_dt(2026, 5, 1))

    _profile(client, ALICE, "Alice")
    _profile(client, BOB, "Bob")
    # Alice has already earned 10 credits inside the rolling window.
    for i in range(10):
        crepo.add_entry("alice", 1, "exchange_earn", idempotency_key=f"c{i}")

    r = client.post("/v1/listings", json=_listing_payload(cost=2), headers=ALICE)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]

    r = client.post(f"/v1/listings/{lid}/claim", headers=BOB)
    assert r.status_code == 200, r.text
    r = client.post("/v1/exchange/confirm", json={"listing_id": lid}, headers=BOB)
    assert r.status_code == 200, r.text

    # Alice's confirm would earn her 2 more -> over the cap -> 409.
    r = client.post("/v1/exchange/confirm", json={"listing_id": lid}, headers=ALICE)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "earn_cap_exceeded"
    assert "request_id" in r.json()


# ---------------------------------------------------------------------------
# Starter credits on signup
# ---------------------------------------------------------------------------

def test_starter_credits_granted_on_signup(mem_economy):
    client, _, _, _ = mem_economy

    r = client.post("/v1/users", json={"display_name": "Alice", "age_attestation": True}, headers=ALICE)
    assert r.status_code == 200, r.text

    body = client.get("/v1/wallet", headers=ALICE).json()
    assert body["balance"] == 3
    starters = [e for e in body["entries"] if e["reason"] == "starter"]
    assert len(starters) == 1 and starters[0]["delta"] == 3

    # Repeat signup is idempotent: no double grant.
    r = client.post("/v1/users", json={"display_name": "Alice", "age_attestation": True}, headers=ALICE)
    assert r.status_code == 200, r.text
    assert client.get("/v1/wallet", headers=ALICE).json()["balance"] == 3


# ---------------------------------------------------------------------------
# Ledger stays append-only across expiry (derived view, no mutation)
# ---------------------------------------------------------------------------

def test_ledger_append_only_across_expiry(mem_economy, frozen_time):
    client, _, _, crepo = mem_economy

    frozen_time(_dt(2026, 4, 1))
    _profile(client, ALICE, "Alice")  # +3 starter
    crepo.add_entry("alice", 2, "exchange_earn", ref_id="r1")
    before = copy.deepcopy(crepo.entries("alice"))
    assert crepo.balance("alice") == 5

    # Past the Sep 30 season end: credits are gone from the derived balance,
    # but every ledger row is byte-identical — no expiry rows, no mutation.
    frozen_time(_dt(2026, 10, 15, 12))
    assert crepo.balance("alice") == 0
    assert crepo.entries("alice") == before

    body = client.get("/v1/users/me/credit-expiry", headers=ALICE).json()
    assert body["balance"] == 0
    assert body["expiring"] == []
    assert body["seasonEndMs"] == _season_end_ms(2027, 2, 28)

    # New credits issued in the new season work normally.
    crepo.add_entry("alice", 1, "exchange_earn", ref_id="r2")
    assert crepo.balance("alice") == 1
    assert len(crepo.entries("alice")) == len(before) + 1


# ---------------------------------------------------------------------------
# H12: starter-grant and earn-cap races
# ---------------------------------------------------------------------------

def test_starter_grant_idempotent_under_concurrency():
    """Concurrent ensure_starter_credits calls grant exactly once (the
    deterministic idempotency key collapses the race)."""
    import threading

    from app.credits import MemoryCreditRepo, ensure_starter_credits

    repo = MemoryCreditRepo()
    threads = [
        threading.Thread(target=ensure_starter_credits, args=("alice", repo))
        for _ in range(16)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    starters = [e for e in repo.entries("alice") if e["reason"] == "starter"]
    assert len(starters) == 1
    assert repo.balance("alice") == 3


def test_starter_uses_deterministic_idempotency_key():
    from app.credits import MemoryCreditRepo, ensure_starter_credits

    repo = MemoryCreditRepo()
    ensure_starter_credits("alice", repo)
    ensure_starter_credits("alice", repo)
    starters = [e for e in repo.entries("alice") if e["reason"] == "starter"]
    assert len(starters) == 1
    assert starters[0]["idempotency_key"] == "starter:alice"


def test_earn_cap_holds_under_concurrency():
    """16 threads racing to earn 1 credit each: the per-uid lock serializes
    cap-check + insert, so at most 10 succeed and the rest get 409."""
    import threading

    from app.credits import EarnCapExceededError, MemoryCreditRepo

    repo = MemoryCreditRepo()
    results = []

    def earn(i):
        try:
            repo.add_entry("alice", 1, "exchange_earn", ref_id=f"r{i}",
                           idempotency_key=f"earn:{i}")
            results.append("ok")
        except EarnCapExceededError:
            results.append("capped")

    threads = [threading.Thread(target=earn, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 10
    assert results.count("capped") == 6
