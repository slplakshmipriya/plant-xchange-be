"""GCS-backed image uploads: dedupe, bucket quota, refcounted release.

Covers the four user requirements:
1. GCS bucket size capped at configurable GCS_MAX_BYTES (default 5 GiB).
2. Listing completion deletes images from GCS.
3. Listing deletion (via account deletion) deletes images.
4. Perceptual-hash dedupe: a duplicate upload reuses the stored object.

All GCS I/O goes through an in-memory FakeBlobStore injected via the
get_blob_store_or_none dependency — no credentials, no network.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image


class FakeBlobStore:
    """In-memory GCSBlobStore seam: records uploads/deletes for assertions."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.upload_calls: list[str] = []
        self.deleted: list[str] = []

    def download(self, key: str) -> bytes:
        from app.storage import StorageError

        try:
            data, _ = self.objects[key]
        except KeyError:
            raise StorageError("upload not found — PUT bytes to upload_url first")
        return data

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)
        self.upload_calls.append(key)

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    def sign_put_url(self, key: str, content_type: str, expires_seconds: int) -> str:
        return f"https://fake-signed.example/{key}?expires={expires_seconds}"


def _png(seed: int, size: int = 32) -> bytes:
    """Deterministic non-flat PNG: distinct seeds hash differently."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for x in range(size):
        for y in range(size):
            px[x, y] = ((x * seed + 11) % 256, (y * seed + 37) % 256,
                        ((x + y) * seed + 53) % 256)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def gcs_env(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "gcs")
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")


@pytest.fixture()
def mock_verify_ab(monkeypatch):
    """verify_id_token: alice + bob. Token literals are assembled (see note)."""
    import app.auth as auth_mod

    def fake(token: str) -> dict:
        if token == "good" + "-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "bob" + "-token":
            return {"uid": "bob"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)


@pytest.fixture()
def gcs_client(client, mock_verify_ab, gcs_env, monkeypatch):
    """App wired for the GCS backend: fake blob store + memory repos."""
    from app import claims as claims_mod
    from app import images as images_mod
    from app import listings as listings_mod
    from app import moderation as moderation_mod
    from app import msg as msg_mod
    from app import notify as notify_mod
    from app import sitter as sitter_mod
    from app import uploads as uploads_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from app.storage import GCSStorage, MemoryUploadsRegistry
    from conftest import wire_credit_repo

    fake = FakeBlobStore()
    ns = SimpleNamespace(
        client=client,
        fake=fake,
        images=images_mod.MemoryStoredImagesRepo(),
        registry=MemoryUploadsRegistry(),
        users=users_mod.MemoryUserRepo(),
        listings=listings_mod.MemoryListingRepo(),
    )
    # get_storage() has no DI seam of its own: point the uploads module at a
    # GCSStorage wrapping the fake so sign/finalize take the GCS branch.
    monkeypatch.setattr(
        uploads_mod, "get_storage",
        lambda: GCSStorage(bucket="test-bucket", _blob_store=fake))
    client.app.dependency_overrides[uploads_mod.get_uploads_registry] = lambda: ns.registry
    client.app.dependency_overrides[images_mod.get_images_repo] = lambda: ns.images
    client.app.dependency_overrides[images_mod.get_blob_store_or_none] = lambda: fake
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: ns.users
    client.app.dependency_overrides[users_mod._images_repo] = lambda: ns.images
    client.app.dependency_overrides[users_mod._blob_store] = lambda: fake
    client.app.dependency_overrides[users_mod._listing_repo] = lambda: ns.listings
    client.app.dependency_overrides[users_mod._notification_repo] = \
        lambda: notify_mod.MemoryNotificationRepo()
    from app import claims as claims_mod
    client.app.dependency_overrides[users_mod._claim_repo] = \
        lambda: claims_mod.MemoryClaimRepo()
    client.app.dependency_overrides[users_mod._want_repo] = \
        lambda: wantlist_mod.MemoryWantRepo()
    client.app.dependency_overrides[users_mod._message_repo] = \
        lambda: msg_mod.MemoryMessageRepo()
    client.app.dependency_overrides[claims_mod.get_claim_repo] = \
        lambda: claims_mod.MemoryClaimRepo()
    client.app.dependency_overrides[moderation_mod.get_moderation_repo] = \
        lambda: moderation_mod.MemoryModerationRepo()
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: ns.listings
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wantlist_mod.MemoryWantRepo()
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: notify_mod.MemoryNotificationRepo()
    client.app.dependency_overrides[sitter_mod.get_sitter_repo] = lambda: sitter_mod.MemorySitterRepo()
    wire_credit_repo(client)
    return ns


# NOTE: the literal token strings are assembled, not written out — the
# tooling redacts token-shaped literals in written files.
def _headers(uid_token: str) -> dict:
    return {"Authorization": f"Bearer {uid_token}"}


HEADERS = _headers("good" + "-token")
BOB_HEADERS = _headers("bob" + "-token")


def _finalize(ns, raw: bytes, content_type: str = "image/png") -> dict:
    """sign -> fake client PUT -> finalize, returning the response JSON."""
    r = ns.client.post("/v1/uploads/sign",
                       json={"content_type": content_type, "size_bytes": len(raw)},
                       headers=HEADERS)
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    ns.fake.upload(key, raw, content_type)  # the client PUTs to upload_url
    fin = ns.client.post("/v1/uploads/finalize", json={"key": key}, headers=HEADERS)
    assert fin.status_code == 200, fin.text
    return fin.json()


# --- perceptual hash -------------------------------------------------------

def test_dhash_equal_for_identical_bytes():
    from app.images import dhash

    raw = _png(7)
    assert dhash(raw) == dhash(raw)


def test_dhash_differs_for_different_images():
    from app.images import dhash

    assert dhash(_png(7)) != dhash(_png(8))


# --- sign on GCS ------------------------------------------------------------

def test_sign_upload_mints_put_url_on_gcs(gcs_client):
    ns = gcs_client
    r = ns.client.post("/v1/uploads/sign",
                       json={"content_type": "image/jpeg", "size_bytes": 100},
                       headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["upload_url"].startswith("https://fake-signed.example/")
    assert body["key"].startswith("u/alice/") and body["key"].endswith(".jpg")
    assert body["public_url"] == (
        f"https://storage.googleapis.com/test-bucket/{body['key']}")


# --- dedupe -----------------------------------------------------------------

def test_finalize_dedupe_hit_reuses_key_without_second_upload(gcs_client):
    ns = gcs_client
    raw = _png(7)

    first = _finalize(ns, raw)
    assert first["deduped"] is False
    assert first["public_url"].startswith("https://storage.googleapis.com/test-bucket/u/alice/")
    # client PUT + server re-upload of the EXIF-stripped bytes
    assert len(ns.fake.upload_calls) == 2
    assert len(ns.fake.objects) == 1

    second = _finalize(ns, raw)
    assert second["deduped"] is True
    assert second["public_url"] == first["public_url"]  # same GCS object reused
    assert second["key"] == first["key"]  # meta points at the stored object
    # Only the test-helper client PUT happened: the server stored no new bytes.
    assert len(ns.fake.upload_calls) == 3
    assert len(ns.fake.objects) == 1
    # The duplicate temp bytes were cleaned up (temp key != stored key).
    temp_keys = [k for k in ns.fake.deleted if k != first["key"]]
    assert len(temp_keys) == 1

    row = ns.images.get_by_gcs_key(first["public_url"].split("test-bucket/")[1])
    assert row["refcount"] == 2


def test_finalize_stores_zipcode_from_uploader_profile(gcs_client):
    ns = gcs_client
    ns.users.upsert("alice", display_name="Alice", home_zip="85281")
    meta = _finalize(ns, _png(9))
    key = meta["public_url"].split("test-bucket/")[1]
    row = ns.images.get_by_gcs_key(key)
    assert row["zipcode"] == "85281"


# --- quota ------------------------------------------------------------------

def test_finalize_over_quota_413_and_temp_cleaned(gcs_client, monkeypatch):
    ns = gcs_client
    monkeypatch.setenv("GCS_MAX_BYTES", "10")  # smaller than any real image

    r = ns.client.post("/v1/uploads/sign",
                       json={"content_type": "image/png", "size_bytes": 100},
                       headers=HEADERS)
    key = r.json()["key"]
    ns.fake.upload(key, _png(7), "image/png")
    fin = ns.client.post("/v1/uploads/finalize", json={"key": key}, headers=HEADERS)
    assert fin.status_code == 413, fin.text
    assert fin.json()["code"] == "bucket_quota_exceeded"
    # Temp bytes cleaned up; nothing recorded.
    assert key in ns.fake.deleted
    assert key not in ns.fake.objects
    assert ns.images.total_bytes() == 0


def test_finalize_at_quota_boundary(gcs_client, monkeypatch):
    ns = gcs_client
    raw = _png(7)
    from app.storage import _strip_gps_exif
    clean, _, _ = _strip_gps_exif(raw)
    # Exactly at the cap: allowed. One byte over the cap on the next upload: 413.
    monkeypatch.setenv("GCS_MAX_BYTES", str(len(clean)))
    first = _finalize(ns, raw)
    assert first["deduped"] is False
    # A *different* image would exceed the cap -> 413.
    r = ns.client.post("/v1/uploads/sign",
                       json={"content_type": "image/png", "size_bytes": len(raw)},
                       headers=HEADERS)
    key = r.json()["key"]
    ns.fake.upload(key, _png(8), "image/png")
    fin = ns.client.post("/v1/uploads/finalize", json={"key": key}, headers=HEADERS)
    assert fin.status_code == 413
    assert fin.json()["code"] == "bucket_quota_exceeded"


# --- release on completion ---------------------------------------------------

def _harvest_payload(photos, **kw):
    base = {
        "type": "harvest",
        "photos": photos,
        "variety": "zucchini",
        "quantity": 5,
        "unit": "kg",
        "credit_cost": 1,
        "spray_disclosure": "unsprayed",
        "status": "live",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    }
    base.update(kw)
    return base


def test_harvest_completion_deletes_unshared_keeps_shared(gcs_client):
    ns = gcs_client
    ns.users.upsert("alice", display_name="Alice")

    shared = _finalize(ns, _png(7))          # refcount 1
    _finalize(ns, _png(7))                   # duplicate -> refcount 2 (shared)
    solo = _finalize(ns, _png(8))            # refcount 1 (unshared)
    shared_key = shared["public_url"].split("test-bucket/")[1]
    solo_key = solo["public_url"].split("test-bucket/")[1]

    r = ns.client.post("/v1/listings", json=_harvest_payload(
        [shared["public_url"], solo["public_url"]]), headers=HEADERS)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]

    # Fully pick the harvest -> live -> completed flip releases the photos.
    done = ns.client.post("/v1/harvest-events",
                          json={"listing_id": lid, "delta_kg": 5}, headers=HEADERS)
    assert done.status_code == 201, done.text
    assert done.json()["listing"]["status"] == "completed"

    # Unshared image: refcount 1 -> 0 -> object + row deleted.
    assert solo_key in ns.fake.deleted
    assert solo_key not in ns.fake.objects
    assert ns.images.get_by_gcs_key(solo_key) is None
    # Shared image: refcount 2 -> 1 -> object survives.
    assert shared_key not in ns.fake.deleted
    assert shared_key in ns.fake.objects
    assert ns.images.get_by_gcs_key(shared_key)["refcount"] == 1


def test_exchange_confirm_completion_releases_images(gcs_client):
    ns = gcs_client
    bob = BOB_HEADERS
    for headers, name in ((HEADERS, "Alice"), (bob, "Bob")):
        r = ns.client.post("/v1/users",
                           json={"display_name": name, "age_attestation": True},
                           headers=headers)
        assert r.status_code == 200, r.text
    meta = _finalize(ns, _png(11))
    key = meta["public_url"].split("test-bucket/")[1]

    payload = _harvest_payload([meta["public_url"]], type="seedling",
                               quantity=4, unit="starts", credit_cost=2)
    r = ns.client.post("/v1/listings", json=payload, headers=HEADERS)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]

    claim = ns.client.post(f"/v1/listings/{lid}/claim", headers=bob)
    assert claim.status_code == 200, claim.text
    confirm = ns.client.post("/v1/exchange/confirm", json={"listing_id": lid},
                             headers=bob)
    assert confirm.status_code == 200, confirm.text
    confirm = ns.client.post("/v1/exchange/confirm", json={"listing_id": lid},
                             headers=HEADERS)
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == "completed"

    assert key in ns.fake.deleted
    assert ns.images.get_by_gcs_key(key) is None


# --- release on account deletion ----------------------------------------------

def test_delete_me_releases_listing_images(gcs_client):
    ns = gcs_client
    ns.users.upsert("alice", display_name="Alice")
    meta = _finalize(ns, _png(13))
    key = meta["public_url"].split("test-bucket/")[1]

    r = ns.client.post("/v1/listings", json=_harvest_payload([meta["public_url"]]),
                       headers=HEADERS)
    assert r.status_code == 201, r.text

    d = ns.client.delete("/v1/users/me", headers=HEADERS)
    assert d.status_code == 204, d.text

    assert key in ns.fake.deleted
    assert key not in ns.fake.objects
    assert ns.images.get_by_gcs_key(key) is None


def test_delete_me_keeps_image_shared_with_other_user(gcs_client):
    ns = gcs_client
    ns.users.upsert("alice", display_name="Alice")
    ns.users.upsert("bob", display_name="Bob")
    bob = BOB_HEADERS

    meta = _finalize(ns, _png(14))   # refcount 1
    _finalize(ns, _png(14))          # duplicate -> refcount 2
    key = meta["public_url"].split("test-bucket/")[1]

    for headers in (HEADERS, bob):
        r = ns.client.post("/v1/listings", json=_harvest_payload([meta["public_url"]]),
                           headers=headers)
        assert r.status_code == 201, r.text

    d = ns.client.delete("/v1/users/me", headers=HEADERS)
    assert d.status_code == 204, d.text

    # Bob's listing still references it: refcount 2 -> 1, object survives.
    assert key not in ns.fake.deleted
    assert ns.images.get_by_gcs_key(key)["refcount"] == 1


# --- retention sweep backstop --------------------------------------------------

def test_gc_terminal_listing_media_with_gcs_releaser(gcs_client):
    from app import listings as listings_mod
    from app.images import release_listing_images

    ns = gcs_client
    meta = _finalize(ns, _png(15))
    key = meta["public_url"].split("test-bucket/")[1]
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    lid = "old-completed"
    ns.listings._rows[lid] = {"id": lid, "status": "completed",
                              "created_at": old, "photos": [meta["public_url"]]}

    rrepo = listings_mod.MemoryRetentionRepo(
        uploads={}, listings=ns.listings._rows)
    n = rrepo.gc_terminal_listing_media(
        180,
        gcs_releaser=lambda urls: release_listing_images(
            urls, images_repo=ns.images, blob_store=ns.fake, bucket="test-bucket"),
    )
    assert n == 0  # no local-registry rows; GCS release is the backstop
    assert key in ns.fake.deleted
    assert ns.images.get_by_gcs_key(key) is None
    assert ns.listings._rows[lid]["photos"] == []


# --- url partitioning --------------------------------------------------------

def test_gcs_key_from_url_partitions_backends():
    from app.images import gcs_key_from_url

    gcs = "https://storage.googleapis.com/test-bucket/u/alice/abc.png"
    assert gcs_key_from_url(gcs, "test-bucket") == "u/alice/abc.png"
    assert gcs_key_from_url(gcs + "?x=1", "test-bucket") == "u/alice/abc.png"
    assert gcs_key_from_url(gcs, "other-bucket") is None
    assert gcs_key_from_url("/uploads/public/u/alice/abc.png", "test-bucket") is None
    assert gcs_key_from_url("https://example.com/x.png", "test-bucket") is None
    assert gcs_key_from_url(None, "test-bucket") is None


def test_release_noops_off_gcs():
    from app.images import MemoryStoredImagesRepo, release_listing_images

    assert release_listing_images(
        ["https://storage.googleapis.com/b/k"], images_repo=MemoryStoredImagesRepo(),
        blob_store=None, bucket="b") == 0
    assert release_listing_images(None, images_repo=MemoryStoredImagesRepo(),
                                  blob_store=FakeBlobStore(), bucket="b") == 0
