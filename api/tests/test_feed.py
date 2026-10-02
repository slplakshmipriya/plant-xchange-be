"""Feed ranking: want-list matches first, then newest post, then soonest
expiry (nulls last); cursor pagination."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.feed import _feed_rank_key


def _row(**kw):
    base = {
        "id": "x", "owner_uid": "alice", "type": "seedling", "photos": [],
        "variety": "tomato", "quantity": 1, "unit": "starts",
        "credit_cost": 1, "pickup_window": None,
        "expires_at": None, "geo_lat": None, "geo_lon": None,
        "spray_disclosure": "none", "status": "live", "created_at": None,
    }
    base.update(kw)
    return base


def _iso(dt):
    return dt.isoformat()


def _entry(variety, user_uid="bob", types=None):
    return {"user_uid": user_uid, "variety": variety, "types": types or []}


def _ranked(rows, entries):
    return [r["id"] for r in sorted(rows, key=lambda r: _feed_rank_key(r, entries))]


def test_want_match_outranks_newer_non_match():
    now = datetime.now(timezone.utc)
    old_match = _row(id="old-match", variety="Cherokee Purple tomato",
                     created_at=_iso(now - timedelta(days=5)),
                     expires_at=_iso(now + timedelta(days=10)))
    new_plain = _row(id="new-plain", variety="basil",
                     created_at=_iso(now - timedelta(minutes=5)),
                     expires_at=_iso(now + timedelta(days=1)))
    entries = [_entry("tomato")]
    assert _ranked([new_plain, old_match], entries) == ["old-match", "new-plain"]


def test_newer_post_breaks_non_match_ties():
    now = datetime.now(timezone.utc)
    exp = _iso(now + timedelta(days=3))
    old = _row(id="old", created_at=_iso(now - timedelta(days=2)), expires_at=exp)
    new = _row(id="new", created_at=_iso(now - timedelta(minutes=5)), expires_at=exp)
    assert _ranked([old, new], []) == ["new", "old"]


def test_sooner_expiry_breaks_date_ties():
    now = datetime.now(timezone.utc)
    created = _iso(now - timedelta(hours=1))
    soon = _row(id="soon", created_at=created,
                expires_at=_iso(now + timedelta(days=1)))
    later = _row(id="later", created_at=created,
                 expires_at=_iso(now + timedelta(days=6)))
    assert _ranked([later, soon], []) == ["soon", "later"]


def test_null_expiry_sorts_last():
    now = datetime.now(timezone.utc)
    created = _iso(now - timedelta(hours=1))
    dated = _row(id="dated", created_at=created,
                 expires_at=_iso(now + timedelta(days=6)))
    undated = _row(id="undated", created_at=created, expires_at=None)
    assert _ranked([undated, dated], []) == ["dated", "undated"]


def test_own_listings_never_count_as_matches():
    # find_matches excludes the caller's own listings from matching.
    now = datetime.now(timezone.utc)
    mine = _row(id="mine", owner_uid="bob", variety="tomato",
                created_at=_iso(now - timedelta(days=5)))
    theirs = _row(id="theirs", owner_uid="alice", variety="basil",
                  created_at=_iso(now - timedelta(minutes=5)))
    entries = [_entry("tomato", user_uid="bob")]
    assert _ranked([mine, theirs], entries) == ["theirs", "mine"]


def test_feed_returns_live_only_with_pagination(mem_listings, mock_verify, auth_headers):
    client, urepo, lrepo, _, _ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    now = datetime.now(timezone.utc)
    lrepo.create({"id": "live1", "owner_uid": "alice", "type": "seedling",
                  "photos": ["https://x/1.jpg"], "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live",
                  "expires_at": _iso(now + timedelta(days=1))})
    lrepo.create({"id": "live2", "owner_uid": "alice", "type": "harvest",
                  "photos": ["https://x/2.jpg"], "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live",
                  "expires_at": _iso(now + timedelta(days=5))})
    lrepo.create({"id": "draft1", "owner_uid": "alice", "type": "seedling",
                  "photos": ["https://x/3.jpg"], "credit_cost": 1,
                  "spray_disclosure": "none", "status": "draft",
                  "expires_at": _iso(now + timedelta(days=1))})

    r = client.get("/v1/feed?limit=1", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["id"] for i in body["items"]] == ["live1"]  # soonest expiry first
    assert body["next_cursor"]

    r2 = client.get(f"/v1/feed?limit=1&cursor={body['next_cursor']}", headers=auth_headers)
    body2 = r2.json()
    assert [i["id"] for i in body2["items"]] == ["live2"]
    assert body2["next_cursor"] is None


def test_feed_bad_cursor_is_400(mem_listings, mock_verify, auth_headers):
    client, *_ = mem_listings
    r = client.get("/v1/feed?cursor=!!!not-base64!!!", headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_cursor"


def test_feed_geo_is_fuzzed_not_exact(mem_listings, mock_verify, auth_headers):
    client, urepo, lrepo, _, _ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    lrepo.create({"id": "g1", "owner_uid": "alice", "type": "seedling",
                  "photos": ["https://x/g.jpg"], "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live",
                  "geo_lat": 33.4152, "geo_lon": -111.8315})
    r = client.get("/v1/feed", headers=auth_headers)
    item = r.json()["items"][0]
    assert (item["geo_lat"], item["geo_lon"]) != (33.4152, -111.8315)


# ------------------------------------------------ M9: DB-level feed pagination

def _feed_listing(i, now):
    return {"id": f"f{i}", "owner_uid": "alice", "type": "seedling",
            "photos": ["https://x/y.jpg"], "credit_cost": 1,
            "spray_disclosure": "none", "status": "live",
            "created_at": (now - timedelta(minutes=i)).isoformat(),
            "expires_at": (now + timedelta(days=i + 1)).isoformat()}


def test_feed_pagination_is_db_level(mem_listings, mock_verify, auth_headers):
    """M9: the feed fetches one page from the DB (limit/offset) — it no
    longer loads the whole live set into memory."""
    client, urepo, lrepo, _, _ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    now = datetime.now(timezone.utc)
    for i in range(3):
        lrepo.create(_feed_listing(i, now))
    r = client.get("/v1/feed?limit=2", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["id"] for i in body["items"]] == ["f0", "f1"]
    assert body["next_cursor"]
    r2 = client.get(f"/v1/feed?limit=2&cursor={body['next_cursor']}", headers=auth_headers)
    body2 = r2.json()
    assert [i["id"] for i in body2["items"]] == ["f2"]
    assert body2["next_cursor"] is None


def test_feed_page_query_uses_limit_offset(mock_verify):
    """M9: the feed route passes limit/offset into the listing repo — a
    repo that rejects unbounded listing queries still works."""
    from fastapi.testclient import TestClient

    from app.listings import MemoryListingRepo, get_listing_repo
    from app.main import create_app
    from app.sitter import MemorySitterRepo, get_sitter_repo
    from app.users import MemoryUserRepo, get_user_repo
    from app.wantlist import MemoryWantRepo, get_want_repo

    class _BoundedRepo(MemoryListingRepo):
        def list_live(self, limit=None, offset=0, listing_type=None):
            assert limit is not None, "feed must pass a page size"
            return super().list_live(limit=limit, offset=offset,
                                      listing_type=listing_type)

    app = create_app()
    urepo, lrepo, wrepo = MemoryUserRepo(), _BoundedRepo(), MemoryWantRepo()
    urepo.upsert("alice", display_name="Alice")
    lrepo.create(_feed_listing(0, datetime.now(timezone.utc)))
    app.dependency_overrides[get_user_repo] = lambda: urepo
    app.dependency_overrides[get_listing_repo] = lambda: lrepo
    app.dependency_overrides[get_want_repo] = lambda: wrepo
    app.dependency_overrides[get_sitter_repo] = lambda: MemorySitterRepo()
    client = TestClient(app)
    r = client.get("/v1/feed?limit=10", headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1


# ------------------------------------------------ M10c: batch sitting display names

def test_batch_display_names_prefers_get_many():
    """M10c: a repo with get_many() is called ONCE with deduped uids."""
    from app.feed import _batch_display_names

    calls = []

    class _BatchRepo:
        def get_many(self, uids):
            calls.append(list(uids))
            return {u: {"display_name": f"Name-{u}"} for u in uids}

    names = _batch_display_names(_BatchRepo(), ["a", "b", "a"])
    assert names == {"a": "Name-a", "b": "Name-b"}
    assert calls == [["a", "b"]]  # one batched call, deduped


def test_batch_display_names_falls_back_to_get():
    """M10c compat: repos without get_many() still work via per-uid get()."""
    from app.feed import _batch_display_names

    class _LegacyRepo:
        def __init__(self):
            self.calls = []

        def get(self, uid):
            self.calls.append(uid)
            return {"display_name": f"N-{uid}"}

    repo = _LegacyRepo()
    names = _batch_display_names(repo, ["a", "b"])
    assert names == {"a": "N-a", "b": "N-b"}
    assert repo.calls == ["a", "b"]
