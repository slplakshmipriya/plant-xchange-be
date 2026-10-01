"""Container-local LRU read cache for hot backend reads (API-XXX).

Warm-path latency only: this cache lives in the container process, so it is
empty after a scale-to-zero cold start — it does NOT shorten wake-up. What it
does is make repeated warm reads (feed pages, profiles, sitter profiles,
thread message pages) microseconds instead of a Neon round trip.

Deliberate deviations from the first sketch (documented so nobody
"fixes" them back):
- No 10ms cache-vs-DB race. A hedge makes sense for a *network* cache;
  this lookup is ~1 microsecond in-process, so plain cache-first with TTL
  strictly dominates racing.
- Writes are DB-then-cache, sequential, not parallel. The cache op is
  microseconds; parallel fan-out buys nothing in-process and complicates
  failure semantics. Write methods that return the new row write it
  straight into the cache (write-through); methods returning None just
  invalidate.
- Not 1GB / 6h-activity-window keyed. The container runs on 512MB, so the
  bound is entry-count (MAX_ENTRIES); a sliding TTL gives the activity
  semantics — entries for idle users expire on their own.

Scope: exactly the four hot domains — listings (feed backing queries),
user profiles, sitter profiles, message pages. Auth-path lookups
(get_by_phone_hash) and sweeper scans stay uncached on purpose.

Consistency model: TTL-bounded staleness (60s listings, 30s messages,
300s profiles) PLUS explicit invalidation on every write path, so a
mutation is visible on the very next read through the same container.
Under multi-instance scale-out each instance holds its own cache and TTL
is the only cross-instance bound — acceptable for this app's traffic.

Threading: uvicorn runs one worker process per container, but sync repo
code executes on a threadpool, so all cache ops take the lock.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

# TTLs (seconds). Listings/messages change often -> short; profiles rarely -> long.
TTL_LISTING = 60
TTL_USER = 300
TTL_SITTER = 300
TTL_MESSAGE = 30

# Entry-count bound for the process-global cache. A few thousand small dicts
# is single-digit MB — comfortably inside the container's memory.
MAX_ENTRIES = 4096

_MISS = object()


class ReadCache:
    """Thread-safe TTL LRU with sliding expiry and defensive copies.

    Sliding expiry: every hit refreshes the entry's deadline, so entries for
    actively-read keys stay warm (the "active users" semantics) while idle
    keys age out. Stored values are deep-copied on the way in AND out, so a
    caller mutating a returned dict can never corrupt the cached copy.
    """

    def __init__(self, maxsize: int = MAX_ENTRIES) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str, ttl: float) -> Any:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return _MISS
            expires_at, value = entry
            if expires_at <= now:
                del self._data[key]
                self.misses += 1
                return _MISS
            # Hit: refresh sliding expiry, mark most-recently-used.
            self._data[key] = (now + ttl, value)
            self._data.move_to_end(key)
            self.hits += 1
            return copy.deepcopy(value)

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, copy.deepcopy(value))
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def get_or_load(self, key: str, loader: Callable[[], Any], ttl: float) -> Any:
        """Read-through: return cached, else load, store, and return.

        ``None`` results are NOT cached — a "row doesn't exist" answer must
        not survive the row's creation (e.g. user signs up between reads).
        """
        value = self.get(key, ttl)
        if value is not _MISS:
            return value
        value = loader()
        if value is not None:
            self.set(key, value, ttl)
        return value

    def invalidate(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._data.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [k for k in self._data if k.startswith(prefix)]:
                del self._data[key]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses,
                    "entries": len(self._data)}


# Process-global instance. Uvicorn runs a single worker per container, so
# one global == one per container.
cache = ReadCache()


# ---------------------------------------------------------------------------
# Caching repo decorators. Each wraps the corresponding repo Protocol:
# reads go through the cache, writes go to the DB first and then refresh or
# invalidate the affected keys. Unknown methods are NOT proxied — the
# decorator lists every method it supports so a Protocol change fails
# loudly here instead of silently bypassing the cache.
# ---------------------------------------------------------------------------


class CachedUserRepo:
    """Caches UserRepo.get / get_many; write-through on upsert."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def get(self, uid: str) -> dict[str, Any] | None:
        return cache.get_or_load(f"u:{uid}", lambda: self._inner.get(uid),
                                 TTL_USER)

    def get_many(self, uids: list[str]) -> dict[str, dict[str, Any]]:
        # Per-uid read-through: partition into hits/misses, one inner query
        # for the misses, then populate. Preserves the batched-query win.
        wanted = list(dict.fromkeys(uids))  # dedupe, keep order
        out: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for uid in wanted:
            value = cache.get(f"u:{uid}", TTL_USER)
            if value is _MISS:
                missing.append(uid)
            else:
                out[uid] = value
        if missing:
            fresh = self._inner.get_many(missing)
            for uid, row in fresh.items():
                cache.set(f"u:{uid}", row, TTL_USER)
            out.update(fresh)
        return out

    def get_by_phone_hash(self, phone_hash: str) -> dict[str, Any] | None:
        # Auth path: infrequent, correctness-sensitive -> skip the cache.
        return self._inner.get_by_phone_hash(phone_hash)

    def upsert(self, uid: str, **fields: Any) -> dict[str, Any]:
        row = self._inner.upsert(uid, **fields)
        cache.set(f"u:{uid}", row, TTL_USER)  # write-through
        return row

    def set_idv_status(self, uid: str, status: str) -> None:
        self._inner.set_idv_status(uid, status)
        cache.invalidate(f"u:{uid}")

    def delete(self, uid: str) -> bool:
        ok = self._inner.delete(uid)
        cache.invalidate(f"u:{uid}")
        return ok


class CachedListingRepo:
    """Caches listing reads; any write invalidates the live/owner lists."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def _invalidate_lists(self, listing_id: str | None = None) -> None:
        # Feed pages, counts, and owner lists all derive from listing rows.
        cache.invalidate_prefix("l:live")
        cache.invalidate_prefix("l:owner")
        if listing_id is not None:
            cache.invalidate(f"l:{listing_id}")

    def get(self, listing_id: str) -> dict[str, Any] | None:
        return cache.get_or_load(f"l:{listing_id}",
                                 lambda: self._inner.get(listing_id),
                                 TTL_LISTING)

    def list_live(self, limit: int | None = None, offset: int = 0,
                  listing_type: str | None = None) -> list[dict[str, Any]]:
        return cache.get_or_load(
            f"l:live:{limit}:{offset}:{listing_type}",
            lambda: self._inner.list_live(limit=limit, offset=offset,
                                          listing_type=listing_type),
            TTL_LISTING)

    def count_live(self, listing_type: str | None = None) -> int:
        return cache.get_or_load(
            f"l:live:count:{listing_type}",
            lambda: self._inner.count_live(listing_type),
            TTL_LISTING)

    def list_by_owner(self, uid: str) -> list[dict[str, Any]]:
        return cache.get_or_load(f"l:owner:{uid}",
                                 lambda: self._inner.list_by_owner(uid),
                                 TTL_LISTING)

    def list_harvest_events(self, listing_id: str) -> list[dict[str, Any]]:
        return cache.get_or_load(
            f"l:harvest:{listing_id}",
            lambda: self._inner.list_harvest_events(listing_id), TTL_LISTING)

    def list_live_expiring_before(self, cutoff: Any) -> list[dict[str, Any]]:
        # Sweeper scan: cutoff varies per call and must be fresh -> uncached.
        return self._inner.list_live_expiring_before(cutoff)

    def _write_through(self, listing_id: str | None,
                       row: dict[str, Any] | None) -> None:
        # Invalidate the derived lists, then write-through the single row
        # when the DB returned it.
        self._invalidate_lists(listing_id)
        if listing_id is not None and row is not None:
            cache.set(f"l:{listing_id}", row, TTL_LISTING)

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        row = self._inner.create(data)
        self._invalidate_lists(row.get("id"))
        return row

    def update(self, listing_id: str,
               fields: dict[str, Any]) -> dict[str, Any] | None:
        row = self._inner.update(listing_id, fields)
        self._write_through(listing_id, row)
        return row

    def set_status(self, listing_id: str,
                   status: str) -> dict[str, Any] | None:
        row = self._inner.set_status(listing_id, status)
        self._write_through(listing_id, row)
        return row

    def claim(self, listing_id: str,
              claimer_uid: str) -> dict[str, Any] | None:
        row = self._inner.claim(listing_id, claimer_uid)
        self._write_through(listing_id, row)
        return row

    def complete_if_claimed(self, listing_id: str) -> dict[str, Any] | None:
        row = self._inner.complete_if_claimed(listing_id)
        self._write_through(listing_id, row)
        return row

    def complete_if_live(self, listing_id: str) -> dict[str, Any] | None:
        row = self._inner.complete_if_live(listing_id)
        self._write_through(listing_id, row)
        return row

    def decrement_remaining(self, listing_id: str,
                            delta: float) -> dict[str, Any] | None:
        row = self._inner.decrement_remaining(listing_id, delta)
        self._write_through(listing_id, row)
        return row

    def sweep_expired(self, now: Any) -> int:
        n = self._inner.sweep_expired(now)
        # Bulk status flips: drop every listing key, not just the lists.
        cache.invalidate_prefix("l:")
        return n

    def log_harvest_event(self, listing_id: str, recorder_uid: str,
                          delta_kg: float, remaining_after: float) -> None:
        self._inner.log_harvest_event(listing_id, recorder_uid, delta_kg,
                                      remaining_after)
        cache.invalidate(f"l:harvest:{listing_id}")


class CachedSitterRepo:
    """Caches sitter profiles, the active-sitter list, dates, and reviews."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def get_profile(self, uid: str) -> dict[str, Any] | None:
        return cache.get_or_load(f"s:{uid}",
                                 lambda: self._inner.get_profile(uid),
                                 TTL_SITTER)

    def list_active(self, limit: int = 100,
                    offset: int = 0) -> list[dict[str, Any]]:
        return cache.get_or_load(
            f"s:active:{limit}:{offset}",
            lambda: self._inner.list_active(limit=limit, offset=offset),
            TTL_SITTER)

    def get_available_dates(self, uid: str) -> list[str]:
        return cache.get_or_load(f"s:dates:{uid}",
                                 lambda: self._inner.get_available_dates(uid),
                                 TTL_SITTER)

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        return cache.get_or_load(f"s:req:{request_id}",
                                 lambda: self._inner.get_request(request_id),
                                 TTL_SITTER)

    def list_reviews_for_sitter(self, sitter_uid: str) -> list[dict[str, Any]]:
        return cache.get_or_load(
            f"s:reviews:{sitter_uid}",
            lambda: self._inner.list_reviews_for_sitter(sitter_uid),
            TTL_SITTER)

    def get_review_by_sitting(self, sitting_id: str) -> dict[str, Any] | None:
        # Low-frequency lookup -> skip the cache.
        return self._inner.get_review_by_sitting(sitting_id)

    def upsert_profile(self, uid: str,
                       fields: dict[str, Any]) -> dict[str, Any]:
        row = self._inner.upsert_profile(uid, fields)
        cache.set(f"s:{uid}", row, TTL_SITTER)  # write-through
        cache.invalidate_prefix("s:active")  # profile feeds the active list
        return row

    def set_available_dates(self, uid: str, days: list[str]) -> None:
        self._inner.set_available_dates(uid, days)
        cache.invalidate(f"s:dates:{uid}")
        cache.invalidate_prefix("s:active")

    def create_request(self, row: dict[str, Any]) -> dict[str, Any]:
        created = self._inner.create_request(row)
        cache.set(f"s:req:{created['id']}", created, TTL_SITTER)
        return created

    def set_request_status(self, request_id: str, status: str,
                           expected_status: str) -> dict[str, Any]:
        updated = self._inner.set_request_status(request_id, status,
                                                 expected_status)
        if updated is not None:
            cache.set(f"s:req:{request_id}", updated, TTL_SITTER)
        else:
            cache.invalidate(f"s:req:{request_id}")
        return updated

    def create_review(self, row: dict[str, Any]) -> dict[str, Any]:
        created = self._inner.create_review(row)
        # The sitter uid lives on the review row (memory + postgres agree).
        sitter_uid = created.get("sitter_uid") or row.get("sitter_uid")
        if sitter_uid:
            cache.invalidate(f"s:reviews:{sitter_uid}")
        return created


class CachedMessageRepo:
    """Caches thread message pages; send invalidates the thread's pages."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def list_messages(self, thread_id: str, offset: int,
                      limit: int) -> list[dict[str, Any]]:
        return cache.get_or_load(
            f"m:{thread_id}:{offset}:{limit}",
            lambda: self._inner.list_messages(thread_id, offset, limit),
            TTL_MESSAGE)

    def add_message(self, thread_id: str, sender_uid: str, body: str,
                    kind: str = "text",
                    photo_url: str | None = None) -> dict[str, Any]:
        row = self._inner.add_message(thread_id, sender_uid, body, kind=kind,
                                      photo_url=photo_url)
        cache.invalidate_prefix(f"m:{thread_id}:")
        return row

    def __getattr__(self, name: str) -> Any:
        # Thread management (get_or_create_thread, etc.) is low-frequency ->
        # delegate without caching rather than enumerating every method.
        return getattr(self._inner, name)
