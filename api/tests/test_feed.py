"""Feed ranking: exact want-list matches first, then inexact matches, then
the rest — ranked in the database so LIMIT/OFFSET paginate the global
order. Within each tier: posting date desc (nulls last), expiry asc
(nulls last), id asc. Cursor pagination."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.listings import MemoryListingRepo
from app.wantlist import match_tier


def _row(**kw):
    base = {
        "id": "x", "owner_uid": "alice", "type": "seedling",
        "variety": "tomato", "status": "live",
        "created_at": None, "expires_at": None,
    }
    base.update(kw)
    return base


def _iso(dt):
    return dt.isoformat()


def _entry(variety, user_uid="bob", types=None):
    return {"user_uid": user_uid, "variety": variety, "types": types or []}


def _repo(*rows):
    repo = MemoryListingRepo()
    for row in rows:
        repo.create(row)
    return repo


def _ranked(repo, entries, **kw):
    return [r["id"] for r in repo.list_live_ranked(entries, **kw)]


def test_exact_outranks_inexact_outranks_plain():
    now = datetime.now(timezone.utc)
    # Dates deliberately run counter to the tiers: tiers must dominate.
    exact = _row(id="exact", variety="Tomato",
                 created_at=_iso(now - timedelta(days=9)))
    inexact = _row(id="inexact", variety="Cherokee Purple tomato",
                   created_at=_iso(now - timedelta(days=5)))
    plain = _row(id="plain", variety="basil",
                 created_at=_iso(now - timedelta(minutes=5)))
    entries = [_entry("tomato")]
    repo = _repo(exact, inexact, plain)
    assert _ranked(repo, entries) == ["exact", "inexact", "plain"]
    assert match_tier(exact, entries) == 0
    assert match_tier(inexact, entries) == 1
    assert match_tier(plain, entries) == 2


def test_exact_tier_orders_by_posted_desc():
    now = datetime.now(timezone.utc)
    old = _row(id="old", variety="tomato",
               created_at=_iso(now - timedelta(days=2)),
               expires_at=_iso(now + timedelta(days=10)))
    new = _row(id="new", variety="TOMATO",
               created_at=_iso(now - timedelta(minutes=5)),
               expires_at=_iso(now + timedelta(days=10)))
    entries = [_entry("ToMaTo")]
    repo = _repo(old, new)
    assert _ranked(repo, entries) == ["new", "old"]


def test_exact_tier_expiry_breaks_posted_ties_nulls_last():
    now = datetime.now(timezone.utc)
    created = _iso(now - timedelta(hours=1))
    soon = _row(id="soon", variety="tomato", created_at=created,
                expires_at=_iso(now + timedelta(days=1)))
    later = _row(id="later", variety="tomato", created_at=created,
                 expires_at=_iso(now + timedelta(days=6)))
    undated = _row(id="undated", variety="tomato", created_at=created,
                   expires_at=None)
    entries = [_entry("tomato")]
    repo = _repo(later, undated, soon)
    assert _ranked(repo, entries) == ["soon", "later", "undated"]


def test_inexact_tier_orders_by_posted_desc():
    now = datetime.now(timezone.utc)
    old = _row(id="old", variety="Green tomato",
               created_at=_iso(now - timedelta(days=2)))
    new = _row(id="new", variety="Cherokee Purple tomato",
               created_at=_iso(now - timedelta(minutes=5)))
    entries = [_entry("tomato")]
    repo = _repo(old, new)
    assert _ranked(repo, entries) == ["new", "old"]


def test_own_listings_never_count_as_matches():
    # find_matches excludes the caller's own listings from matching.
    now = datetime.now(timezone.utc)
    mine = _row(id="mine", owner_uid="bob", variety="tomato",
                created_at=_iso(now - timedelta(days=5)))
    theirs = _row(id="theirs", owner_uid="alice", variety="basil",
                  created_at=_iso(now - timedelta(minutes=5)))
    entries = [_entry("tomato", user_uid="bob")]
    repo = _repo(mine, theirs)
    assert match_tier(mine, entries) == 2
    assert _ranked(repo, entries) == ["theirs", "mine"]


def test_entry_type_filter_honored():
    now = datetime.now(timezone.utc)
    seedling = _row(id="seedling", type="seedling", variety="tomato",
                    created_at=_iso(now - timedelta(minutes=1)))
    harvest = _row(id="harvest", type="harvest", variety="tomato",
                   created_at=_iso(now - timedelta(minutes=2)))
    entries = [_entry("tomato", types=["harvest"])]
    repo = _repo(seedling, harvest)
    assert match_tier(seedling, entries) == 2
    assert match_tier(harvest, entries) == 0
    assert _ranked(repo, entries) == ["harvest", "seedling"]


def test_ranking_is_global_across_pages():
    # The point of DB-side ranking: a match that would sit on DB page 2
    # still outranks non-matches on page 1. limit=1 must return the match.
    now = datetime.now(timezone.utc)
    new1 = _row(id="new1", variety="basil",
                created_at=_iso(now - timedelta(minutes=5)))
    new2 = _row(id="new2", variety="kale",
                created_at=_iso(now - timedelta(minutes=10)))
    old_match = _row(id="old-match", variety="tomato",
                     created_at=_iso(now - timedelta(days=5)))
    entries = [_entry("tomato")]
    repo = _repo(new1, new2, old_match)
    assert _ranked(repo, entries, limit=1, offset=0) == ["old-match"]
    assert _ranked(repo, entries, limit=1, offset=1) == ["new1"]
    assert _ranked(repo, entries, limit=1, offset=2) == ["new2"]
    assert _ranked(repo, entries, limit=1, offset=3) == []


def test_way_branch_ranks_within_type():
    now = datetime.now(timezone.utc)
    seedling_match = _row(id="seedling-match", type="seedling",
                          variety="tomato",
                          created_at=_iso(now - timedelta(days=3)))
    seedling_plain = _row(id="seedling-plain", type="seedling",
                          variety="basil",
                          created_at=_iso(now - timedelta(minutes=5)))
    harvest_match = _row(id="harvest-match", type="harvest",
                         variety="tomato",
                         created_at=_iso(now - timedelta(days=1)))
    entries = [_entry("tomato")]
    repo = _repo(seedling_match, seedling_plain, harvest_match)
    assert _ranked(repo, entries, listing_type="seedling") == [
        "seedling-match", "seedling-plain"]


def test_feed_returns_live_only_with_pagination(mem_listings, mock_verify, auth_headers):
    client, urepo, lrepo, _, _ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    now = datetime.now(timezone.utc)
    # No wants: every listing is tier 2, so posting date desc wins.
    lrepo.create({"id": "live1", "owner_uid": "alice", "type": "seedling",
                  "photos": ["https://x/1.jpg"], "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live",
                  "created_at": _iso(now - timedelta(hours=2)),
                  "expires_at": _iso(now + timedelta(days=1))})
    lrepo.create({"id": "live2", "owner_uid": "alice", "type": "harvest",
                  "photos": ["https://x/2.jpg"], "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live",
                  "created_at": _iso(now - timedelta(hours=1)),
                  "expires_at": _iso(now + timedelta(days=5))})
    lrepo.create({"id": "draft1", "owner_uid": "alice", "type": "seedling",
                  "photos": ["https://x/3.jpg"], "credit_cost": 1,
                  "spray_disclosure": "none", "status": "draft",
                  "created_at": _iso(now - timedelta(minutes=1)),
                  "expires_at": _iso(now + timedelta(days=1))})

    r = client.get("/v1/feed?limit=1", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["id"] for i in body["items"]] == ["live2"]  # newest post first
    assert body["next_cursor"]

    r2 = client.get(f"/v1/feed?limit=1&cursor={body['next_cursor']}", headers=auth_headers)
    body2 = r2.json()
    assert [i["id"] for i in body2["items"]] == ["live1"]
    assert body2["next_cursor"] is None


def test_feed_match_outranks_across_pages(mem_listings, mock_verify, auth_headers):
    client, urepo, lrepo, wrepo, _ = mem_listings
    urepo.upsert("alice", display_name="Alice")
    urepo.upsert("bob", display_name="Bob")
    now = datetime.now(timezone.utc)
    lrepo.create({"id": "new-plain", "owner_uid": "bob", "type": "seedling",
                  "variety": "basil", "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live",
                  "created_at": _iso(now - timedelta(minutes=5)),
                  "expires_at": _iso(now + timedelta(days=5))})
    lrepo.create({"id": "old-match", "owner_uid": "bob", "type": "seedling",
                  "variety": "Cherokee Purple tomato", "credit_cost": 1,
                  "spray_disclosure": "none", "status": "live",
                  "created_at": _iso(now - timedelta(days=5)),
                  "expires_at": _iso(now + timedelta(days=10))})
    # The auth user wants tomatoes: the old match must top page 1 even
    # though the DB page would otherwise hold only the newer listing.
    r = client.post("/v1/want-list", json={"variety": "tomato"},
                    headers=auth_headers)
    assert r.status_code == 201, r.text

    body = client.get("/v1/feed?limit=1", headers=auth_headers).json()
    assert [i["id"] for i in body["items"]] == ["old-match"]
    body2 = client.get(
        f"/v1/feed?limit=1&cursor={body['next_cursor']}",
        headers=auth_headers).json()
    assert [i["id"] for i in body2["items"]] == ["new-plain"]


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
        def list_live_ranked(self, entries, limit=None, offset=0,
                             listing_type=None):
            assert limit is not None, "feed must pass a page size"
            return super().list_live_ranked(entries, limit=limit,
                                            offset=offset,
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
