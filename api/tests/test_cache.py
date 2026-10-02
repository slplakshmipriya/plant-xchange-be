"""Tests for the container-local LRU read cache (app/cache.py) and the
GET /v1/internal/warm keep-warm endpoint."""
from __future__ import annotations

import threading

import pytest

from app import cache as cache_mod
from app.cache import (
    CachedListingRepo,
    CachedMessageRepo,
    CachedSitterRepo,
    CachedUserRepo,
    ReadCache,
    cache,
)


@pytest.fixture()
def tiny_cache():
    return ReadCache(maxsize=4)


@pytest.fixture()
def fake_clock(monkeypatch):
    """Deterministic monotonic clock for TTL tests."""
    now = {"t": 1000.0}

    def _monotonic():
        return now["t"]

    monkeypatch.setattr(cache_mod.time, "monotonic", _monotonic)
    return now


# --- ReadCache unit tests -------------------------------------------------


def test_get_or_load_caches(tiny_cache):
    calls = []

    def loader():
        calls.append(1)
        return {"v": 1}

    assert tiny_cache.get_or_load("k", loader, ttl=60) == {"v": 1}
    assert tiny_cache.get_or_load("k", loader, ttl=60) == {"v": 1}
    assert len(calls) == 1
    assert tiny_cache.stats()["hits"] == 1
    assert tiny_cache.stats()["misses"] == 1


def test_none_results_are_not_cached(tiny_cache):
    # A "row doesn't exist" answer must not survive the row's creation.
    calls = []

    def loader():
        calls.append(1)
        return None

    assert tiny_cache.get_or_load("k", loader, ttl=60) is None
    assert tiny_cache.get_or_load("k", loader, ttl=60) is None
    assert len(calls) == 2


def test_ttl_expiry(tiny_cache, fake_clock):
    tiny_cache.set("k", "v", ttl=10)
    assert tiny_cache.get("k", ttl=10) == "v"
    fake_clock["t"] += 11
    assert tiny_cache.get("k", ttl=10) is cache_mod._MISS


def test_sliding_expiry_keeps_active_keys_warm(tiny_cache, fake_clock):
    tiny_cache.set("k", "v", ttl=10)
    fake_clock["t"] += 9
    assert tiny_cache.get("k", ttl=10) == "v"  # hit refreshes the deadline
    fake_clock["t"] += 9
    assert tiny_cache.get("k", ttl=10) == "v"  # still warm: 18s > 10s ttl
    fake_clock["t"] += 11
    assert tiny_cache.get("k", ttl=10) is cache_mod._MISS


def test_lru_eviction(tiny_cache):
    for i in range(4):
        tiny_cache.set(f"k{i}", i, ttl=60)
    tiny_cache.get("k0", ttl=60)  # k0 now most-recently-used
    tiny_cache.set("k4", 4, ttl=60)  # evicts k1 (least-recently-used)
    assert tiny_cache.get("k1", ttl=60) is cache_mod._MISS
    assert tiny_cache.get("k0", ttl=60) == 0
    assert tiny_cache.stats()["entries"] == 4


def test_invalidate_and_prefix(tiny_cache):
    tiny_cache.set("l:live:20:0:None", [1], ttl=60)
    tiny_cache.set("l:123", {"id": "123"}, ttl=60)
    tiny_cache.set("u:alice", {"uid": "alice"}, ttl=60)
    tiny_cache.invalidate_prefix("l:")
    assert tiny_cache.get("l:live:20:0:None", ttl=60) is cache_mod._MISS
    assert tiny_cache.get("l:123", ttl=60) is cache_mod._MISS
    assert tiny_cache.get("u:alice", ttl=60) == {"uid": "alice"}
    tiny_cache.invalidate("u:alice")
    assert tiny_cache.get("u:alice", ttl=60) is cache_mod._MISS


def test_deepcopy_isolation(tiny_cache):
    tiny_cache.set("k", {"items": [1, 2]}, ttl=60)
    first = tiny_cache.get("k", ttl=60)
    first["items"].append(999)  # mutate the caller's copy
    assert tiny_cache.get("k", ttl=60) == {"items": [1, 2]}


def test_thread_safety_single_flight(tiny_cache):
    calls = []
    lock = threading.Lock()

    def loader():
        with lock:
            calls.append(1)
        return {"v": 1}

    def hammer():
        for _ in range(50):
            assert tiny_cache.get_or_load("k", loader, ttl=60) == {"v": 1}

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Many concurrent misses may each load, but never corrupt the cache.
    assert tiny_cache.get("k", ttl=60) == {"v": 1}
    assert len(calls) >= 1


# --- Decorator integration tests (memory repos) ----------------------------


class CountingProxy:
    """Wraps a repo, counting inner read calls."""

    def __init__(self, inner):
        self._inner = inner
        self.reads = 0

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr) or name in (
            "upsert", "create", "update", "set_status", "claim",
            "complete_if_claimed", "complete_if_live", "decrement_remaining",
            "sweep_expired", "log_harvest_event", "set_idv_status", "delete",
            "upsert_profile", "set_available_dates", "create_request",
            "set_request_status", "create_review", "add_message",
        ):
            return attr

        def counted(*a, **k):
            self.reads += 1
            return attr(*a, **k)

        return counted


def test_user_repo_caches_and_write_through():
    from app.users import MemoryUserRepo

    inner = CountingProxy(MemoryUserRepo())
    repo = CachedUserRepo(inner)
    repo.upsert("alice", display_name="Alice")
    assert repo.get("alice")["display_name"] == "Alice"
    assert repo.get("alice")["display_name"] == "Alice"
    assert inner.reads == 0  # upsert wrote through; no DB read at all
    # get_many: one miss -> one batched inner query, then fully cached.
    repo.upsert("bob", display_name="Bob")
    got = repo.get_many(["alice", "bob", "carol"])
    assert set(got) == {"alice", "bob"}
    assert inner.reads == 1
    repo.get_many(["alice", "bob"])
    assert inner.reads == 1
    # set_idv_status invalidates.
    repo.set_idv_status("alice", "verified")
    assert repo.get("alice")["idv_status"] == "verified"
    assert inner.reads == 2


def _listing_data(i):
    return {
        "id": f"l{i}",
        "owner_uid": "alice",
        "type": "seedling",
        "status": "live",
        "variety": "basil",
        "quantity": 1,
    }


def test_listing_repo_feed_caching_and_invalidation():
    from app.listings import MemoryListingRepo

    inner = CountingProxy(MemoryListingRepo())
    repo = CachedListingRepo(inner)
    repo.create(_listing_data(1))
    page1 = repo.list_live(limit=20, offset=0)
    assert len(page1) == 1
    repo.list_live(limit=20, offset=0)
    assert inner.reads == 1  # second page served from cache
    assert repo.count_live() == 1
    assert repo.count_live() == 1
    assert inner.reads == 2
    # A new listing invalidates the feed pages and counts.
    repo.create(_listing_data(2))
    assert len(repo.list_live(limit=20, offset=0)) == 2
    assert repo.count_live() == 2
    # Single-row write-through: update returns the row, no re-read needed.
    repo.update("l1", {"variety": "mint"})
    assert repo.get("l1")["variety"] == "mint"
    assert inner.reads == 4
    # Claim flips status -> feed invalidated, single row write-through.
    claimed = repo.claim("l1", "bob")
    assert claimed["status"] == "claimed"
    assert repo.get("l1")["status"] == "claimed"
    # Owner list cached + invalidated on create.
    assert len(repo.list_by_owner("alice")) == 2


def test_ranked_feed_cached_per_viewer():
    """Ranked feed pages are cached at cid (viewer-uid) level: one
    viewer's ranking must never be served to another viewer."""
    from app.listings import MemoryListingRepo

    inner = CountingProxy(MemoryListingRepo())
    repo = CachedListingRepo(inner)
    repo.create({"id": "rl1", "owner_uid": "alice", "type": "seedling",
                 "status": "live", "variety": "basil", "quantity": 1})
    a_entries = [{"user_uid": "viewer-a", "variety": "basil", "types": []}]
    b_entries = [{"user_uid": "viewer-b", "variety": "basil", "types": []}]

    assert len(repo.list_live_ranked(a_entries, limit=20, offset=0)) == 1
    repo.list_live_ranked(a_entries, limit=20, offset=0)
    assert inner.reads == 1  # same viewer + page served from cache

    # A different viewer gets their own cache entry, not viewer-a's ranking.
    assert len(repo.list_live_ranked(b_entries, limit=20, offset=0)) == 1
    assert inner.reads == 2

    # A listing write invalidates ranked pages for every viewer.
    repo.create({"id": "rl2", "owner_uid": "alice", "type": "seedling",
                 "status": "live", "variety": "mint", "quantity": 1})
    assert len(repo.list_live_ranked(a_entries, limit=20, offset=0)) == 2
    assert inner.reads == 3


def test_sitter_repo_caching_and_invalidation():
    from app.sitter import MemorySitterRepo

    inner = CountingProxy(MemorySitterRepo())
    repo = CachedSitterRepo(inner)
    repo.upsert_profile("s1", {"bio": "hello"})
    assert repo.get_profile("s1")["bio"] == "hello"
    assert repo.get_profile("s1")["bio"] == "hello"
    assert inner.reads == 0  # write-through
    assert len(repo.list_active()) >= 1
    assert len(repo.list_active()) >= 1
    assert inner.reads == 1
    # Profile edit invalidates the active-sitter list.
    repo.upsert_profile("s1", {"bio": "updated"})
    assert repo.get_profile("s1")["bio"] == "updated"
    repo.list_active()
    assert inner.reads == 2
    # Availability dates cached, invalidated on change.
    repo.set_available_dates("s1", ["2026-10-05"])
    assert repo.get_available_dates("s1") == ["2026-10-05"]
    assert repo.get_available_dates("s1") == ["2026-10-05"]
    assert inner.reads == 3


def test_message_repo_page_cache_and_send_invalidation():
    from app.msg import MemoryMessageRepo

    inner = CountingProxy(MemoryMessageRepo())
    repo = CachedMessageRepo(inner)
    inner._inner._messages["t1"] = []
    repo.add_message("t1", "alice", "hi")
    page = repo.list_messages("t1", 0, 20)
    assert len(page) == 1
    repo.list_messages("t1", 0, 20)
    assert inner.reads == 1
    # A new message invalidates the thread's cached pages.
    repo.add_message("t1", "bob", "hello")
    assert len(repo.list_messages("t1", 0, 20)) == 2
    assert inner.reads == 2


# --- /v1/internal/warm endpoint -------------------------------------------


class _FakeCursor:
    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q):
        self.queries.append(q)

    def fetchone(self):
        return (1,)


class _FakeConn:
    def __init__(self):
        self.cursor_obj = _FakeCursor()

    def cursor(self):
        return self.cursor_obj


@pytest.fixture()
def warm_client(client, monkeypatch):
    """TestClient with SWEEP_SECRET set and get_db_conn faked."""
    from app import db as db_mod
    from app import listings as listings_mod

    monkeypatch.setenv("SWEEP_SECRET", "test-warm-secret")
    fake = _FakeConn()
    client.app.dependency_overrides[db_mod.get_db_conn] = lambda: fake
    return client, fake


def test_warm_rejects_bad_secret(warm_client):
    client, fake = warm_client
    r = client.get("/v1/internal/warm")
    assert r.status_code == 401
    r = client.get("/v1/internal/warm", headers={"x-sweep-secret": "wrong"})
    assert r.status_code == 401
    assert fake.cursor_obj.queries == []  # DB never touched on auth failure


def test_warm_pings_db_with_good_secret(warm_client):
    client, fake = warm_client
    r = client.get("/v1/internal/warm", headers={"x-sweep-secret": "test-warm-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "warm"
    assert isinstance(body["db_ms"], (int, float))
    assert fake.cursor_obj.queries == ["SELECT 1"]


def test_warm_503_without_secret_configured(client, monkeypatch):
    from app import db as db_mod

    monkeypatch.delenv("SWEEP_SECRET", raising=False)
    client.app.dependency_overrides[db_mod.get_db_conn] = lambda: _FakeConn()
    r = client.get("/v1/internal/warm", headers={"x-sweep-secret": "anything"})
    assert r.status_code == 503
    assert r.json()["code"] == "sweep_not_configured"
