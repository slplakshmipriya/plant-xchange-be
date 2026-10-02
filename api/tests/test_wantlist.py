"""API-030: want-list CRUD + match engine on listing go-live."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import notify as notify_mod
from app.wantlist import find_matches, variety_matches


@pytest.fixture()
def mem_all(client):
    """client + user/listing/want/notify memory repos."""
    from app import listings as listings_mod
    from app import sitter as sitter_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from conftest import wire_credit_repo

    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    wrepo = wantlist_mod.MemoryWantRepo()
    nrepo = notify_mod.MemoryNotificationRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wrepo
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: nrepo
    client.app.dependency_overrides[sitter_mod.get_sitter_repo] = lambda: sitter_mod.MemorySitterRepo()
    wire_credit_repo(client)
    return client, urepo, lrepo, wrepo, nrepo


@pytest.fixture()
def frozen(monkeypatch):
    def _freeze(at: datetime):
        monkeypatch.setattr(notify_mod, "_now", lambda: at)
    return _freeze


def _noon():
    # 12:00 UTC = 08:00 EDT — outside quiet hours (21:00-08:00 local).
    return datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _live_payload(**kw):
    base = {
        "type": "seedling",
        "photos": ["https://example.com/a.jpg"],
        "variety": "Cherokee Purple tomato",
        "quantity": 6,
        "unit": "starts",
        "credit_cost": 2,
        "spray_disclosure": "none",
        "status": "live",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
    }
    base.update(kw)
    return base


# ------------------------------------------------ matching rule

@pytest.mark.parametrize("want,listing,expected", [
    ("tomato", "Cherokee Purple tomato", True),
    ("Cherokee Purple tomato", "tomato", True),   # either direction
    ("TOMATO", "cherokee purple tomato", True),   # case-insensitive
    ("basil", "tomato", False),
    ("", "tomato", False),
    ("tomato", None, False),
])
def test_variety_matches(want, listing, expected):
    assert variety_matches(want, listing) is expected


def test_find_matches_excludes_owner_and_wrong_type():
    listing = {"id": "l1", "owner_uid": "alice", "type": "seedling", "variety": "tomato"}
    entries = [
        {"user_uid": "alice", "variety": "tomato", "types": []},          # owner: skip
        {"user_uid": "bob", "variety": "tomato", "types": ["harvest"]},   # type mismatch
        {"user_uid": "carol", "variety": "tomato", "types": ["seedling"]},
        {"user_uid": "dave", "variety": "tomato", "types": []},           # any type
        {"user_uid": "erin", "variety": "basil", "types": []},            # no match
    ]
    got = {e["user_uid"] for e in find_matches(listing, entries)}
    assert got == {"carol", "dave"}


# ------------------------------------------------ CRUD

def test_want_crud_owner_scoped(mem_all, mock_verify, auth_headers):
    client, urepo, _, _, _ = mem_all
    urepo.upsert("alice", display_name="Alice")

    r = client.post("/v1/want-list", json={"variety": "  tomato ", "types": ["seedling"]},
                    headers=auth_headers)
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    assert r.json()["variety"] == "tomato"  # trimmed

    r = client.get("/v1/want-list", headers=auth_headers)
    assert [w["id"] for w in r.json()["items"]] == [wid]

    r = client.patch(f"/v1/want-list/{wid}", json={"variety": "basil"}, headers=auth_headers)
    assert r.json()["variety"] == "basil"

    r = client.delete(f"/v1/want-list/{wid}", headers=auth_headers)
    assert r.status_code == 204
    r = client.get("/v1/want-list", headers=auth_headers)
    assert r.json()["items"] == []


def test_want_rejects_bad_type(mem_all, mock_verify, auth_headers):
    client, urepo, _, _, _ = mem_all
    urepo.upsert("alice", display_name="Alice")
    r = client.post("/v1/want-list", json={"variety": "tomato", "types": ["nope"]},
                    headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_want_type"


# ------------------------------------------------ match engine

def test_match_push_on_create_live(mem_all, mock_verify, auth_headers, frozen):
    frozen(_noon())
    client, urepo, _, wrepo, nrepo = mem_all
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    wrepo.create({"id": "w1", "user_uid": "bob", "variety": "tomato", "types": []})

    r = client.post("/v1/listings", json=_live_payload(), headers=auth_headers)
    assert r.status_code == 201, r.text
    listing_id = r.json()["id"]

    logged = [e for e in nrepo._log if e["category"] == "match"]
    assert len(logged) == 1
    assert logged[0]["user_uid"] == "bob"
    assert logged[0]["ref"] == listing_id
    assert logged[0]["outcome"] == "would_send"  # no FCM creds in test


def test_match_push_on_draft_to_live(mem_all, mock_verify, auth_headers, frozen):
    frozen(_noon())
    client, urepo, _, wrepo, nrepo = mem_all
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    wrepo.create({"id": "w1", "user_uid": "bob", "variety": "tomato", "types": []})

    r = client.post("/v1/listings", json=_live_payload(status="draft"), headers=auth_headers)
    lid = r.json()["id"]
    assert not [e for e in nrepo._log if e["category"] == "match"]

    r = client.patch(f"/v1/listings/{lid}", json={"status": "live"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    logged = [e for e in nrepo._log if e["category"] == "match"]
    assert len(logged) == 1 and logged[0]["user_uid"] == "bob"


def test_match_deduped_per_listing(mem_all, mock_verify, auth_headers, frozen):
    """At-most-one match push per (user, listing) per 24h — relisting the same
    payload twice does not double-notify."""
    frozen(_noon())
    client, urepo, _, wrepo, nrepo = mem_all
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    wrepo.create({"id": "w1", "user_uid": "bob", "variety": "tomato", "types": []})

    for _ in range(2):
        r = client.post("/v1/listings", json=_live_payload(), headers=auth_headers)
        lid = r.json()["id"]
        # Simulate the same listing going live twice (e.g. retry): notify again
        # with the same ref must be deduped by the notify layer.
        from app.wantlist import notify_matches
        from app import listings as listings_mod
        row = client.app.dependency_overrides[listings_mod.get_listing_repo]().get(lid)
        notify_matches(row, wrepo, nrepo)

    bobs = [e for e in nrepo._log if e["user_uid"] == "bob" and e["category"] == "match"
            and e["outcome"] in ("sent", "would_send")]
    # 2 listings x 1 delivered each (the repeated notify_matches calls dedupe)
    assert len(bobs) == 2
    dupes = [e for e in nrepo._log if e["outcome"] == "skipped_duplicate"]
    assert len(dupes) == 2  # the repeats were caught by the 24h dedupe guard


def test_feed_boosts_want_match(mem_all, mock_verify, auth_headers):
    client, urepo, lrepo, wrepo, _ = mem_all
    urepo.upsert("alice", display_name="Alice")
    wrepo.create({"id": "w1", "user_uid": "alice", "variety": "tomato", "types": []})
    now = datetime.now(timezone.utc)
    exp = (now + timedelta(days=3)).isoformat()
    # Same expiry/age: only the boost should separate them.
    lrepo.create({"id": "plain", "owner_uid": "alice", "type": "seedling",
                  "photos": ["https://x/p.jpg"], "variety": "basil", "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live", "expires_at": exp,
                  "created_at": now.isoformat()})
    lrepo.create({"id": "match", "owner_uid": "alice", "type": "seedling",
                  "photos": ["https://x/m.jpg"], "variety": "tomato", "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live", "expires_at": exp,
                  "created_at": now.isoformat()})
    r = client.get("/v1/feed", headers=auth_headers)
    assert [i["id"] for i in r.json()["items"]] == ["match", "plain"]


# ------------------------------------------------ M9: DB-level want-list matching

def test_find_matches_for_listing_agrees_with_find_matches():
    """M9: the repo matcher has the same semantics as the pure function."""
    from app.wantlist import MemoryWantRepo, find_matches

    repo = MemoryWantRepo()
    repo.create({"id": "w1", "user_uid": "carol", "variety": "tomato", "types": []})
    repo.create({"id": "w2", "user_uid": "dave", "variety": "tom",
                 "types": ["seedling"]})  # reverse substring
    repo.create({"id": "w3", "user_uid": "alice", "variety": "tomato",
                 "types": []})  # owner excluded
    repo.create({"id": "w4", "user_uid": "erin", "variety": "tomato",
                 "types": ["harvest"]})  # wrong type excluded
    repo.create({"id": "w5", "user_uid": "frank", "variety": "", "types": []})
    row = {"id": "l1", "owner_uid": "alice", "type": "seedling",
           "variety": "Cherry Tomato"}
    assert {e["user_uid"] for e in repo.find_matches_for_listing(row)} == {"carol", "dave"}
    assert {e["user_uid"] for e in find_matches(row, repo.list_all())} == {"carol", "dave"}


def test_postgres_find_matches_uses_single_indexed_query():
    """M9: DB-level matching is ONE query with a trigram-friendly LIKE arm
    (the pg_trgm GIN index from migration 0030 serves it); a blank variety
    short-circuits without touching the DB at all."""
    from app.wantlist import PostgresWantRepo

    class _Conn:
        def __init__(self):
            self.queries = []

        def execute(self, q, params=None):
            self.queries.append((q, params))
            return self

        def fetchall(self):
            return []

        def commit(self):
            pass

    conn = _Conn()
    repo = PostgresWantRepo(conn)
    assert repo.find_matches_for_listing(
        {"id": "l1", "owner_uid": "alice", "type": "seedling",
         "variety": "Tomato"}) == []
    assert len(conn.queries) == 1  # exactly one round trip
    q, params = conn.queries[0]
    assert "want_list" in q and "LIKE" in q
    assert "user_uid <> %s" in q  # owner excluded in SQL
    assert "cardinality(types) = 0" in q  # empty types = any type, in SQL
    assert params[0] == "alice"

    # Blank variety: no query at all.
    conn.queries.clear()
    assert repo.find_matches_for_listing(
        {"owner_uid": "alice", "type": "seedling", "variety": "  "}) == []
    assert conn.queries == []


def test_notify_matches_uses_db_level_matcher():
    """M9: notify_matches prefers find_matches_for_listing over list_all."""
    from app.notify import MemoryNotificationRepo
    from app.wantlist import MemoryWantRepo, notify_matches

    class _SpyRepo(MemoryWantRepo):
        def __init__(self):
            super().__init__()
            self.calls = []

        def find_matches_for_listing(self, row):
            self.calls.append(row["id"])
            return []

    wrepo = _SpyRepo()
    notify_matches({"id": "l9", "owner_uid": "alice", "type": "seedling",
                    "variety": "tomato"},
                   wrepo, MemoryNotificationRepo())
    assert wrepo.calls == ["l9"]  # used the DB-level matcher


def test_migration_0030_adds_trigram_index():
    """M9: migration 0030 ships the pg_trgm index backing DB-level matching."""
    from app.db import discover_migrations

    versions = dict(discover_migrations())
    assert 30 in versions
    assert versions[30].name == "0030_want_variety_trgm.sql"
    sql = versions[30].read_text()
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in sql
    assert "gin_trgm_ops" in sql
    assert "lower(variety)" in sql


# ------------------------------------------------ L1b: update column whitelist

def test_want_update_rejects_unknown_column():
    """L1b: update keys are whitelisted — an unknown column never reaches SQL."""
    import pytest

    from app.wantlist import PostgresWantRepo

    class _NoExecConn:
        def execute(self, q, params=None):
            raise AssertionError("must not reach SQL")

        def commit(self):
            pass

    with pytest.raises(ValueError, match="unknown want_list columns"):
        PostgresWantRepo(_NoExecConn()).update("w1", {"variety": "x",
                                                      "user_uid": "mallory"})


def test_want_create_rejects_duplicate_variety(mem_all, mock_verify, auth_headers):
    client, _, _, _, _ = mem_all
    r = client.post("/v1/want-list", json={"variety": "Tomato"}, headers=auth_headers)
    assert r.status_code == 201, r.text

    # Same case, different case, and surrounding whitespace all collide.
    for dup in ("Tomato", "TOMATO", "  tomato  "):
        r = client.post("/v1/want-list", json={"variety": dup}, headers=auth_headers)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "want_duplicate"

    # Only one row was stored.
    r = client.get("/v1/want-list", headers=auth_headers)
    assert len(r.json()["items"]) == 1



def test_want_patch_rejects_duplicate_variety(mem_all, mock_verify, auth_headers):
    client, _, _, _, _ = mem_all
    r = client.post("/v1/want-list", json={"variety": "Tomato"}, headers=auth_headers)
    assert r.status_code == 201
    r = client.post("/v1/want-list", json={"variety": "Basil"}, headers=auth_headers)
    wid = r.json()["id"]

    r = client.patch(f"/v1/want-list/{wid}", json={"variety": "  TOMATO "},
                     headers=auth_headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "want_duplicate"

    # Patching to its own current variety is not a duplicate.
    r = client.patch(f"/v1/want-list/{wid}", json={"variety": "basil"},
                     headers=auth_headers)
    assert r.status_code == 200, r.text




def test_want_create_duplicate_is_per_user(mem_all, mock_verify, auth_headers):
    """The same variety for a *different* user is fine."""
    client, _, _, _, _ = mem_all
    r = client.post("/v1/want-list", json={"variety": "Tomato"}, headers=auth_headers)
    assert r.status_code == 201, r.text
    other = {"Authorization": "Bearer nophone-token"}
    r = client.post("/v1/want-list", json={"variety": "TOMATO"}, headers=other)
    assert r.status_code == 201, r.text
