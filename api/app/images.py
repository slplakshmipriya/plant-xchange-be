"""Content-addressed image library for GCS uploads (API-022 follow-up).

Replaces the local-filesystem photo pipeline for production
(``STORAGE_BACKEND=gcs``):

- **Dedupe**: after EXIF stripping, ``finalize_gcs_upload`` hashes the clean
  bytes with dHash (perceptual, 64-bit hex). A finalize whose phash already
  exists in ``stored_images`` reuses the stored GCS object: the duplicate
  bytes are deleted and the row's refcount is incremented — no second
  upload. Listing ``photos[]`` then point at the shared object.
- **Quota**: ``GCS_MAX_BYTES`` (default 5 GiB = Firebase Spark free-tier max
  storage) caps total ``size_bytes`` across ``stored_images``. A finalize
  that would exceed the cap is rejected with 413 ``bucket_quota_exceeded``
  and its temp object is deleted.
- **Release**: because dedupe shares objects across listings, deletion is
  refcounted — ``release_listing_images`` decrements per photo URL and only
  deletes the GCS object + row when refcount hits 0. Hooked into listing
  completion (claimed/live -> completed), account-deletion cascade, and the
  terminal-media retention sweep.

Listing photo URLs for GCS are public
``https://storage.googleapis.com/{bucket}/{key}`` URLs (listing photos are
public data — the bucket needs public-read or a CDN in front).

This module is a leaf: it imports only ``config``, ``db`` and ``storage``,
so ``uploads``/``listings``/``exchange``/``users`` can all depend on it.
"""

from __future__ import annotations

import io
import logging
import uuid
from typing import Any, Protocol

from fastapi import Depends

from .config import get_settings
from .db import get_db_conn
from .storage import (
    GCSBlobStore,
    StorageError,
    StorageNotConfigured,
    _check_key,
    _strip_gps_exif,
    get_storage,
)

logger = logging.getLogger(__name__)


class QuotaExceeded(StorageError):
    """A finalize would push stored bytes past GCS_MAX_BYTES (HTTP 413)."""


class DuplicatePhash(StorageError):
    """A stored_images row with this phash already exists (insert race)."""


def dhash(data: bytes) -> str:
    """64-bit difference hash (dHash) of image bytes, hex-encoded.

    Grayscale -> 9x8 downscale -> 64 adjacent-pixel comparisons. Identical
    (or visually near-identical) images hash equal; different images
    (almost surely) don't. Callers hash the EXIF-stripped bytes so two
    uploads of the same photo with different metadata dedupe.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        small = img.convert("L").resize((9, 8), Image.LANCZOS)
        # tobytes(): one row-major byte per pixel for mode "L".
        px = small.tobytes()
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return f"{bits:016x}"


def public_gcs_url(bucket: str, key: str) -> str:
    return f"https://storage.googleapis.com/{bucket}/{key}"


def gcs_key_from_url(url: Any, bucket: str) -> str | None:
    """Extract the GCS object key from a listing photo public URL.

    Returns None for non-GCS URLs (local-stub ``/uploads/public/`` URLs,
    external URLs) so callers can partition by backend.
    """
    if not isinstance(url, str) or not bucket:
        return None
    prefix = f"https://storage.googleapis.com/{bucket}/"
    if not url.startswith(prefix):
        return None
    key = url[len(prefix):].split("?", 1)[0].strip("/")
    return key or None


def _delete_quietly(store: GCSBlobStore, key: str) -> None:
    """Best-effort blob delete; logs instead of raising.

    Used when dropping duplicate/quota-rejected temp bytes and when
    removing refcount-zero objects: the DB row is the source of truth for
    quota accounting, so a failed object delete must not fail the request
    (it leaves an inert orphan, not corrupt state).
    """
    try:
        store.delete(key)
    except Exception as exc:  # noqa: BLE001 — best effort by design
        logger.warning("GCS delete of %s failed: %s", key, type(exc).__name__)


# ---------------------------------------------------------------------------
# stored_images repository (migration 0037)


class StoredImagesRepo(Protocol):
    def get_by_phash(self, phash: str) -> dict[str, Any] | None: ...
    def get_by_gcs_key(self, key: str) -> dict[str, Any] | None: ...
    def insert(self, row: dict[str, Any]) -> dict[str, Any]:
        """Insert a row. Raises DuplicatePhash when phash already exists."""
        ...
    def add_ref(self, phash: str) -> dict[str, Any] | None:
        """Increment refcount. Returns the updated row, or None when absent."""
        ...
    def release(self, gcs_key: str) -> int | None:
        """Decrement refcount (floor 0); delete the row at 0.

        Returns the new refcount, or None when no row exists for the key."""
        ...
    def total_bytes(self) -> int:
        """SUM(size_bytes) over all rows — current bucket usage for quota."""
        ...


class PostgresStoredImagesRepo:
    def __init__(self, conn):
        self._conn = conn

    def get_by_phash(self, phash):
        rows = self._conn.execute(
            "SELECT * FROM stored_images WHERE phash = %s", (phash,)).fetchall()
        return dict(rows[0]) if rows else None

    def get_by_gcs_key(self, key):
        rows = self._conn.execute(
            "SELECT * FROM stored_images WHERE gcs_key = %s", (key,)).fetchall()
        return dict(rows[0]) if rows else None

    def insert(self, row):
        # ON CONFLICT DO NOTHING: the UNIQUE(phash) arbiter for concurrent
        # finalizes of the same image — the loser gets DuplicatePhash and
        # the caller falls back to the winner's row.
        cur = self._conn.execute(
            "INSERT INTO stored_images (id, phash, gcs_key, size_bytes, zipcode, refcount) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (phash) DO NOTHING RETURNING id",
            (row["id"], row["phash"], row["gcs_key"], row["size_bytes"],
             row.get("zipcode"), row.get("refcount", 1)),
        )
        self._conn.commit()
        if cur.fetchone() is None:
            raise DuplicatePhash(f"phash {row['phash']} already stored")
        return self.get_by_phash(row["phash"])

    def add_ref(self, phash):
        rows = self._conn.execute(
            "UPDATE stored_images SET refcount = refcount + 1 "
            "WHERE phash = %s RETURNING *", (phash,)).fetchall()
        self._conn.commit()
        return dict(rows[0]) if rows else None

    def release(self, gcs_key):
        row = self.get_by_gcs_key(gcs_key)
        if row is None:
            return None
        new_ref = max(0, row["refcount"] - 1)
        if new_ref == 0:
            self._conn.execute(
                "DELETE FROM stored_images WHERE gcs_key = %s", (gcs_key,))
        else:
            self._conn.execute(
                "UPDATE stored_images SET refcount = %s WHERE gcs_key = %s",
                (new_ref, gcs_key))
        self._conn.commit()
        return new_ref

    def total_bytes(self):
        rows = self._conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM stored_images").fetchall()
        return int(rows[0]["total"]) if rows else 0


class MemoryStoredImagesRepo:
    """In-memory stored_images (tests). Mirrors the Postgres semantics,
    including DuplicatePhash on conflicting insert."""

    def __init__(self):
        self._rows: dict[str, dict[str, Any]] = {}

    def get_by_phash(self, phash):
        row = self._rows.get(phash)
        return dict(row) if row else None

    def get_by_gcs_key(self, key):
        for row in self._rows.values():
            if row["gcs_key"] == key:
                return dict(row)
        return None

    def insert(self, row):
        if row["phash"] in self._rows:
            raise DuplicatePhash(f"phash {row['phash']} already stored")
        rec = {"id": row["id"], "phash": row["phash"], "gcs_key": row["gcs_key"],
               "size_bytes": row["size_bytes"], "zipcode": row.get("zipcode"),
               "refcount": row.get("refcount", 1)}
        self._rows[row["phash"]] = rec
        return dict(rec)

    def add_ref(self, phash):
        row = self._rows.get(phash)
        if row is None:
            return None
        row["refcount"] += 1
        return dict(row)

    def release(self, gcs_key):
        for phash, row in list(self._rows.items()):
            if row["gcs_key"] == gcs_key:
                new_ref = max(0, row["refcount"] - 1)
                if new_ref == 0:
                    del self._rows[phash]
                else:
                    row["refcount"] = new_ref
                return new_ref
        return None

    def total_bytes(self):
        return sum(r["size_bytes"] for r in self._rows.values())


def get_images_repo(conn=Depends(get_db_conn)) -> StoredImagesRepo:
    """Stored-images repo. Tests override with MemoryStoredImagesRepo."""
    return PostgresStoredImagesRepo(conn)


def get_blob_store() -> GCSBlobStore:
    """Blob store for the active backend. Tests override with a fake."""
    from .storage import GCSStorage

    backend = get_storage()
    if not isinstance(backend, GCSStorage):
        raise StorageNotConfigured("GCS blob store requires STORAGE_BACKEND=gcs")
    return backend.blob_store


def get_blob_store_or_none() -> GCSBlobStore | None:
    """get_blob_store that returns None instead of raising — for routes that
    run on both backends (release is a GCS-only no-op elsewhere). Tests
    override THIS factory to inject a fake blob store."""
    try:
        return get_blob_store()
    except StorageError:
        return None


# ---------------------------------------------------------------------------
# finalize with dedupe + quota


def _finalize_meta(bucket: str, key: str, size_bytes: int, content_type: str,
                   gps_stripped: bool, deduped: bool) -> dict[str, Any]:
    public_url = public_gcs_url(bucket, key)
    return {
        "key": key,
        "public_url": public_url,
        "thumb_url": public_url,  # no separate thumbnail rendition (yet)
        "size_bytes": size_bytes,
        "content_type": content_type,
        "gps_stripped": gps_stripped,
        "deduped": deduped,
    }


def finalize_gcs_upload(blob_store: GCSBlobStore, bucket: str, uid: str, key: str,
                        images_repo: StoredImagesRepo,
                        zipcode: str | None) -> dict[str, Any]:
    """GCS finalize: download -> EXIF-strip -> dedupe -> quota -> store.

    This is the app's GCS finalize path (``GCSStorage.finalize`` remains the
    plain download/strip/re-upload primitive it is tested as). ``zipcode``
    is the uploader's home_zip — informational metadata; the listing does
    not exist yet at upload time.
    """
    _check_key(key, uid)
    try:
        data = blob_store.download(key)
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001 — client-lib errors, safe 422/502
        raise StorageError(f"GCS download failed: {type(exc).__name__}") from exc
    clean, gps_removed, content_type = _strip_gps_exif(data)
    phash = dhash(clean)

    existing = images_repo.get_by_phash(phash)
    if existing is not None:
        # Duplicate: drop the just-uploaded bytes, reuse the stored object.
        _delete_quietly(blob_store, key)
        images_repo.add_ref(existing["phash"])
        return _finalize_meta(bucket, existing["gcs_key"], existing["size_bytes"],
                              content_type, gps_removed, deduped=True)

    cap = get_settings().gcs_max_bytes
    used = images_repo.total_bytes()
    if used + len(clean) > cap:
        _delete_quietly(blob_store, key)
        raise QuotaExceeded(
            f"bucket quota exceeded: {used + len(clean)} > {cap} bytes "
            f"(GCS_MAX_BYTES)")

    try:
        blob_store.upload(key, clean, content_type)
    except Exception as exc:  # noqa: BLE001 — client-lib errors, safe 502
        raise StorageError(f"GCS re-upload failed: {type(exc).__name__}") from exc
    try:
        images_repo.insert({
            "id": str(uuid.uuid4()), "phash": phash, "gcs_key": key,
            "size_bytes": len(clean), "zipcode": zipcode, "refcount": 1,
        })
    except DuplicatePhash:
        # Lost a dedupe race: another finalize stored the same image first.
        # Drop our copy and fall back to the winner's row.
        _delete_quietly(blob_store, key)
        existing = images_repo.get_by_phash(phash)
        if existing is None:  # pragma: no cover — defensive; cannot happen
            raise
        images_repo.add_ref(existing["phash"])
        return _finalize_meta(bucket, existing["gcs_key"], existing["size_bytes"],
                              content_type, gps_removed, deduped=True)
    if gps_removed:
        logger.info("stripped EXIF GPS from GCS upload %s", key)
    return _finalize_meta(bucket, key, len(clean), content_type,
                          gps_removed, deduped=False)


# ---------------------------------------------------------------------------
# refcounted release


def release_listing_images(photo_urls: list[str] | None, *,
                           images_repo: StoredImagesRepo,
                           blob_store: GCSBlobStore | None,
                           bucket: str | None) -> int:
    """Refcount-release the stored images referenced by listing photo URLs.

    No-op when the GCS backend isn't active (blob_store/bucket None):
    local-stub photos are dev-only files. Each distinct GCS key is
    decremented once; the GCS object + row are deleted only when refcount
    hits 0. Returns the number of GCS objects deleted.
    """
    if not photo_urls or blob_store is None or not bucket:
        return 0
    deleted = 0
    seen: set[str] = set()
    for url in photo_urls:
        key = gcs_key_from_url(url, bucket)
        if not key or key in seen:
            continue
        seen.add(key)
        if images_repo.release(key) == 0:
            _delete_quietly(blob_store, key)
            deleted += 1
    return deleted
