"""Photo upload pipeline (API-022).

Flow: ``POST /v1/uploads/sign`` -> client PUTs bytes to ``upload_url`` ->
``POST /v1/uploads/finalize`` validates and returns the public URL that
listing ``photos[]`` entries reference.

- ``StorageBackend`` is the abstraction; ``LocalStubStorage`` (default) keeps
  files under ``./var/uploads`` for dev/test, ``GCSStorage`` is the
  production backend selected by ``STORAGE_BACKEND=gcs`` (needs the
  ``google-cloud-storage`` package + ``GCS_BUCKET`` + ADC credentials).
- Server-side validation on finalize: content is a real image (Pillow),
  size <= 8 MB, and **EXIF GPS is stripped** (privacy: phone photos embed
  exact coordinates — SEC-010).
- Keys are ``u/{uid}/{uuid}.{ext}``; ownership and path traversal are
  enforced on every operation.
- GCS finalize additionally dedupes by perceptual hash and enforces the
  ``GCS_MAX_BYTES`` bucket quota — see ``app/images.py``.

Pre-launch storage checklist (H5):
  1. ``STORAGE_BACKEND=gcs`` and ``GCS_BUCKET`` are set; the
     ``google-cloud-storage`` package is installed; the runtime service
     account can ``storage.objects.get/create/delete`` on the bucket
     (delete is needed for dedupe temp-cleanup and refcount-zero removal).
  2. ``ENVIRONMENT=production`` (or ``prod``) — the app refuses to boot with
     ``STORAGE_BACKEND=local`` in prod (``validate_storage_config``); local
     dev keeps the local stub default.
  3. Listing photos are served as public
     ``https://storage.googleapis.com/{bucket}/{key}`` URLs — the bucket
     needs public-read (or a CDN in front) for photos to load.
  4. ``GCS_MAX_BYTES`` caps total stored bytes (default 5 GiB = Firebase
     Spark free-tier max); finalize rejects over-quota uploads with 413.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Protocol

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 8 * 1024 * 1024
SIGN_URL_TTL_SECONDS = 15 * 60  # signed PUT URLs expire after 15 minutes
KEY_RE = re.compile(r"^u/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+\.[a-z0-9]+$")
GPS_IFD_TAG = 0x8825  # EXIF GPSInfo IFD

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
}


class StorageError(Exception):
    pass


class StorageNotConfigured(StorageError):
    pass


class StorageBackend(Protocol):
    def sign_upload(self, uid: str, content_type: str, size_bytes: int) -> dict:
        """Return {"upload_url", "key", "public_url"} for a client PUT."""
        ...

    def store_raw(self, key: str, data: bytes) -> None:
        """Persist raw PUT bytes (local stub only)."""
        ...

    def finalize(self, uid: str, key: str) -> dict:
        """Validate + strip GPS EXIF. Returns metadata incl. public/thumb URLs."""
        ...


def _check_key(key: str, uid: str) -> None:
    if not KEY_RE.match(key):
        raise StorageError("malformed upload key")
    owner = key.split("/")[1]
    if owner != uid:
        raise StorageError("upload key does not belong to caller")


def _check_key_format(key: str) -> None:
    if not KEY_RE.match(key):
        raise StorageError("malformed upload key")


def _strip_gps_exif(data: bytes) -> tuple[bytes, bool, str]:
    """Validate image bytes and strip GPS EXIF (H5/C7 pipeline core).

    Shared by ``LocalStubStorage.finalize`` and ``GCSStorage.finalize`` so
    the production path strips GPS exactly like the local one: download ->
    validate -> strip -> re-upload. Returns ``(clean_bytes, gps_removed,
    content_type)``. Raises ``StorageError`` on oversized / invalid /
    corrupt input.
    """
    from PIL import Image, UnidentifiedImageError

    if len(data) > MAX_IMAGE_BYTES:
        raise StorageError("upload exceeds 8 MB")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()  # not a real image -> raises
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "JPEG").lower()
            img.load()
            exif = img.getexif()
            gps_removed = GPS_IFD_TAG in exif
            if gps_removed:
                del exif[GPS_IFD_TAG]
            # Re-save: applies GPS stripping and normalizes the file.
            buf = io.BytesIO()
            img.save(buf, format=img.format or "JPEG", exif=exif)
    except UnidentifiedImageError as exc:
        raise StorageError("not a valid image") from exc
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001 — corrupt image, safe 422
        raise StorageError(f"image processing failed: {type(exc).__name__}") from exc
    content_type = f"image/{'jpeg' if fmt == 'jpeg' else fmt}"
    return buf.getvalue(), gps_removed, content_type


class LocalStubStorage:
    """Dev/test backend: files under <root>/<key>, served by /v1/uploads/public."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or get_settings().uploads_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # KEY_RE forbids ".." and "/", so this cannot escape root; belt-and-braces:
        p = (self.root / key).resolve()
        if p != self.root and self.root not in p.parents:
            raise StorageError("path traversal rejected")
        return p

    def public_path(self, key: str) -> Path:
        """Resolved path for serving a finalized upload."""
        _check_key_format(key)
        return self._path(key)

    def sign_upload(self, uid: str, content_type: str, size_bytes: int) -> dict:
        ext = ALLOWED_CONTENT_TYPES.get(content_type.lower())
        if not ext:
            raise StorageError(f"unsupported content type: {content_type}")
        if not (0 < size_bytes <= MAX_IMAGE_BYTES):
            raise StorageError(f"size must be within 1..{MAX_IMAGE_BYTES} bytes")
        key = f"u/{uid}/{uuid.uuid4().hex}.{ext}"
        return {
            "upload_url": f"/v1/uploads/raw/{key}",
            "key": key,
            "public_url": f"/v1/uploads/public/{key}",
        }

    def store_raw(self, key: str, data: bytes) -> None:
        _check_key_format(key)  # KEY_RE forbids ".." — traversal can't be expressed
        if len(data) > MAX_IMAGE_BYTES:
            raise StorageError("upload exceeds 8 MB")
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def finalize(self, uid: str, key: str) -> dict:
        _check_key(key, uid)
        path = self._path(key)
        if not path.is_file():
            raise StorageError("upload not found — PUT bytes to upload_url first")
        clean, gps_removed, content_type = _strip_gps_exif(path.read_bytes())
        path.write_bytes(clean)
        if gps_removed:
            logger.info("stripped EXIF GPS from upload %s", key)
        public_url = f"/v1/uploads/public/{key}"
        return {
            "key": key,
            "public_url": public_url,
            "thumb_url": public_url,  # stub: no separate thumbnail rendition
            "size_bytes": len(clean),
            "content_type": content_type,
            "gps_stripped": gps_removed,
        }


class GCSBlobStore(Protocol):
    """Seam for GCS blob I/O behind ``GCSStorage.finalize``.

    The live adapter uses ``google-cloud-storage`` (lazy import); tests
    inject a fake. Keeping blob I/O behind this seam is what lets the
    download -> validate -> strip -> re-upload pipeline be unit-tested
    without network or credentials.
    """

    def download(self, key: str) -> bytes:
        """Blob bytes for ``key``. Raises ``StorageError`` when absent."""
        ...

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        """Replace the blob at ``key`` with ``data``."""
        ...

    def delete(self, key: str) -> None:
        """Delete the blob at ``key``. Missing keys are a no-op."""
        ...

    def sign_put_url(self, key: str, content_type: str, expires_seconds: int) -> str:
        """Mint a signed URL the client PUTs raw bytes to.

        ``content_type`` is advisory only — the URL does not constrain the
        PUT's Content-Type header, so existing clients need no change.
        """


class _LiveGCSBlobStore:
    """Real GCS I/O. Built lazily so importing this module never needs the
    ``google-cloud-storage`` package (absent in dev/CI)."""

    def __init__(self, bucket: str):
        try:
            from google.cloud import storage as gcs
        except ImportError as exc:
            raise StorageNotConfigured(
                "STORAGE_BACKEND=gcs requires the google-cloud-storage package"
            ) from exc
        try:
            self._bucket = gcs.Client().bucket(bucket)
        except Exception as exc:  # noqa: BLE001 — no ADC/credentials, fail closed
            raise StorageNotConfigured(
                "STORAGE_BACKEND=gcs: could not build a GCS client "
                f"({type(exc).__name__}: {exc}); configure ADC or "
                "GOOGLE_APPLICATION_CREDENTIALS"
            ) from exc

    def download(self, key: str) -> bytes:
        blob = self._bucket.blob(key)
        data = blob.download_as_bytes()
        if data is None:
            raise StorageError("upload not found — PUT bytes to upload_url first")
        return data

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        self._bucket.blob(key).upload_from_string(data, content_type=content_type)

    def delete(self, key: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self._bucket.blob(key).delete()
        except NotFound:
            pass  # already gone — callers treat delete as idempotent

    def sign_put_url(self, key: str, content_type: str, expires_seconds: int) -> str:
        from datetime import timedelta

        # No content_type constraint on the URL: the client PUTs whatever it
        # declared at sign time, and finalize re-uploads with the detected
        # type anyway. Constraining it would 403 clients that don't echo the
        # exact header.
        return self._bucket.blob(key).generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expires_seconds),
            method="PUT",
        )


class GCSStorage:
    """Production backend (STORAGE_BACKEND=gcs).

    ``finalize`` runs the full EXIF pipeline: download -> validate ->
    strip GPS EXIF -> re-upload (H5: production photos must not keep GPS).
    The GPS-stripping core is shared with the local stub via
    ``_strip_gps_exif``; blob I/O goes through the ``GCSBlobStore`` seam.
    """

    def __init__(self, bucket: str | None = None, _blob_store: GCSBlobStore | None = None):
        self.bucket = bucket or get_settings().gcs_bucket or os.environ.get("GCS_BUCKET")
        if not self.bucket:
            raise StorageNotConfigured("GCS_BUCKET is not set")
        self._blob_store = _blob_store
        self._live_store: GCSBlobStore | None = None

    @property
    def blob_store(self) -> GCSBlobStore:
        """The blob I/O seam (live client or injected fake)."""
        return self._store()

    def _store(self) -> GCSBlobStore:
        if self._blob_store is not None:
            return self._blob_store
        # Memoized: one gcs.Client per backend instance, not per blob op.
        if self._live_store is None:
            self._live_store = _LiveGCSBlobStore(self.bucket)
        return self._live_store

    def sign_upload(self, uid: str, content_type: str, size_bytes: int) -> dict:
        ext = ALLOWED_CONTENT_TYPES.get(content_type.lower())
        if not ext:
            raise StorageError(f"unsupported content type: {content_type}")
        if not (0 < size_bytes <= MAX_IMAGE_BYTES):
            raise StorageError(f"size must be within 1..{MAX_IMAGE_BYTES} bytes")
        key = f"u/{uid}/{uuid.uuid4().hex}.{ext}"
        try:
            upload_url = self.blob_store.sign_put_url(
                key, content_type, SIGN_URL_TTL_SECONDS)
        except StorageNotConfigured:
            raise
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 — client-lib errors, safe 501
            raise StorageNotConfigured(
                f"could not mint a GCS signed URL: {type(exc).__name__}") from exc
        return {
            "upload_url": upload_url,
            "key": key,
            "public_url": f"https://storage.googleapis.com/{self.bucket}/{key}",
        }

    def store_raw(self, key: str, data: bytes) -> None:
        raise StorageNotConfigured("GCS backend has no local raw-PUT path")

    def finalize(self, uid: str, key: str) -> dict:
        _check_key(key, uid)
        store = self._store()
        try:
            data = store.download(key)
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 — client-lib errors, safe 422/502
            raise StorageError(f"GCS download failed: {type(exc).__name__}") from exc
        clean, gps_removed, content_type = _strip_gps_exif(data)
        try:
            store.upload(key, clean, content_type)
        except Exception as exc:  # noqa: BLE001 — client-lib errors, safe 502
            raise StorageError(f"GCS re-upload failed: {type(exc).__name__}") from exc
        if gps_removed:
            logger.info("stripped EXIF GPS from GCS upload %s", key)
        public_url = f"https://storage.googleapis.com/{self.bucket}/{key}"
        return {
            "key": key,
            "public_url": public_url,
            "thumb_url": public_url,  # no separate thumbnail rendition (yet)
            "size_bytes": len(clean),
            "content_type": content_type,
            "gps_stripped": gps_removed,
        }


def validate_storage_config(settings: Settings) -> None:
    """Fail-closed storage deployment check (H5).

    ``LocalStubStorage`` writes to the container's ephemeral filesystem —
    on Cloud Run it vanishes at scale-to-zero, and listing photos are
    required for live listings, so the core flow would silently break.
    Refuse to boot ``STORAGE_BACKEND=local`` when ``ENVIRONMENT`` signals
    production. Mirrors ``validate_idv_config`` in ``config.py``: call at
    startup (lifespan), and ``get_storage()`` enforces it lazily too so the
    stub backend can never be instantiated in prod even if startup
    validation is skipped.
    """
    if settings.storage_backend == "local" and os.environ.get(
        "ENVIRONMENT", ""
    ).strip().lower() in ("production", "prod"):
        raise RuntimeError(
            "STORAGE_BACKEND=local with ENVIRONMENT=production: the local stub "
            "writes to the ephemeral container filesystem and must never run "
            "in production. Set STORAGE_BACKEND=gcs (and GCS_BUCKET)."
        )


def get_storage() -> StorageBackend:
    settings = get_settings()
    validate_storage_config(settings)  # H5: fail closed on local-in-prod
    backend = settings.storage_backend
    if backend == "gcs":
        return GCSStorage()
    if backend == "local":
        return LocalStubStorage()
    raise StorageNotConfigured(f"unknown STORAGE_BACKEND: {backend}")


def guess_media_type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


# ---------------------------------------------------------------- C7: uploads registry
#
# Persistent finalize-state record for photo uploads. The raw PUT bytes may
# still carry EXIF GPS; only a successful ``finalize`` (which re-saves the
# image with GPS stripped) may flip a row to finalized, and
# ``GET /v1/uploads/public/{key}`` serves only finalized rows (else 404).


class UploadsRegistry(Protocol):
    def record_raw(self, key: str, owner_uid: str) -> None:
        """Register a raw PUT. (Re-)PUT resets finalize state to FALSE."""
        ...

    def mark_finalized(self, key: str) -> None:
        """Mark a key finalized. Call only after EXIF stripping succeeded."""
        ...

    def is_finalized(self, key: str) -> bool:
        """True only when a finalize record exists and is finalized."""
        ...


class MemoryUploadsRegistry:
    """In-memory registry: tests and dev runs without Postgres."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def record_raw(self, key: str, owner_uid: str) -> None:
        self._rows[key] = {"key": key, "owner_uid": owner_uid, "finalized": False}

    def mark_finalized(self, key: str) -> None:
        row = self._rows.get(key)
        if row is None:
            raise StorageError("no upload record for key")
        row["finalized"] = True

    def is_finalized(self, key: str) -> bool:
        row = self._rows.get(key)
        return row is not None and bool(row["finalized"])


class PostgresUploadsRegistry:
    """Postgres-backed registry over the ``uploads`` table (migration 0025)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def record_raw(self, key: str, owner_uid: str) -> None:
        self._conn.execute(
            "INSERT INTO uploads (key, owner_uid, finalized) VALUES (%s, %s, FALSE) "
            "ON CONFLICT (key) DO UPDATE SET "
            "owner_uid = EXCLUDED.owner_uid, finalized = FALSE",
            (key, owner_uid),
        )
        self._conn.commit()

    def mark_finalized(self, key: str) -> None:
        self._conn.execute(
            "UPDATE uploads SET finalized = TRUE WHERE key = %s",
            (key,),
        )
        self._conn.commit()

    def is_finalized(self, key: str) -> bool:
        rows = self._conn.execute(
            "SELECT finalized FROM uploads WHERE key = %s",
            (key,),
        ).fetchall()
        return bool(rows) and bool(rows[0]["finalized"])
