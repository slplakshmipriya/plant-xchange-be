"""API-022: photo upload — sign/PUT/finalize, EXIF GPS stripping, key safety."""
from __future__ import annotations

import io

import pytest
from PIL import Image
from PIL.ExifTags import IFD


@pytest.fixture()
def mem_uploads(client):
    """Override the uploads finalize-state registry with an in-memory one.

    The default get_uploads_registry is Postgres-backed (503 without
    DATABASE_URL); the uploads routes need this override in every test.
    Also wires the stored-images repo + user repo that finalize depends on.
    Returns (client, registry).
    """
    from app import uploads as uploads_mod
    from app import users as users_mod
    from app.storage import MemoryUploadsRegistry
    from conftest import wire_images_repo

    repo = MemoryUploadsRegistry()
    client.app.dependency_overrides[uploads_mod.get_uploads_registry] = lambda: repo
    wire_images_repo(client)
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: users_mod.MemoryUserRepo()
    return client, repo


@pytest.fixture()
def uploads_dir(monkeypatch, tmp_path, mem_uploads):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    return tmp_path / "uploads"


def _gps_jpeg() -> bytes:
    img = Image.new("RGB", (64, 64), "red")
    ex = Image.Exif()
    ex[IFD.GPSInfo] = {
        0: b"\x02\x03\x00\x00",
        1: "N", 2: (37.0, 46.0, 0.0),
        3: "W", 4: (122.0, 25.0, 0.0),
    }
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=ex)
    return buf.getvalue()


def _plain_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(buf, "PNG")
    return buf.getvalue()


def test_sign_rejects_non_image_content_type(client, mock_verify, auth_headers, uploads_dir):
    r = client.post("/v1/uploads/sign",
                    json={"content_type": "application/pdf", "size_bytes": 100},
                    headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_upload"


def test_sign_rejects_oversize(client, mock_verify, auth_headers, uploads_dir):
    r = client.post("/v1/uploads/sign",
                    json={"content_type": "image/jpeg", "size_bytes": 9 * 1024 * 1024},
                    headers=auth_headers)
    assert r.status_code == 422


def test_sign_requires_auth(client, uploads_dir):
    r = client.post("/v1/uploads/sign", json={"content_type": "image/jpeg", "size_bytes": 10})
    assert r.status_code == 401


def test_full_flow_strips_gps_exif(client, mock_verify, auth_headers, uploads_dir):
    raw = _gps_jpeg()
    # sanity: the crafted upload really contains GPS EXIF
    assert IFD.GPSInfo in Image.open(io.BytesIO(raw)).getexif()

    signed = client.post("/v1/uploads/sign",
                         json={"content_type": "image/jpeg", "size_bytes": len(raw)},
                         headers=auth_headers).json()
    key = signed["key"]
    assert key.startswith("u/alice/")

    put = client.put(signed["upload_url"], content=raw,
                     headers={**auth_headers, "Content-Type": "image/jpeg"})
    assert put.status_code == 200

    fin = client.post("/v1/uploads/finalize", json={"key": key}, headers=auth_headers)
    assert fin.status_code == 200
    meta = fin.json()
    assert meta["gps_stripped"] is True
    assert meta["thumb_url"] == meta["public_url"]
    assert meta["content_type"] == "image/jpeg"

    # the stored file itself no longer carries GPS
    stored = Image.open(uploads_dir / key)
    assert IFD.GPSInfo not in stored.getexif()

    # the public URL serves the file without auth
    pub = client.get(meta["public_url"])
    assert pub.status_code == 200
    assert pub.headers["content-type"] == "image/jpeg"


def test_finalize_rejects_non_image(client, mock_verify, auth_headers, uploads_dir):
    signed = client.post("/v1/uploads/sign",
                         json={"content_type": "image/png", "size_bytes": 100},
                         headers=auth_headers).json()
    client.put(signed["upload_url"], content=b"definitely not an image" * 10,
               headers=auth_headers)
    r = client.post("/v1/uploads/finalize", json={"key": signed["key"]}, headers=auth_headers)
    assert r.status_code == 422


def test_finalize_rejects_other_users_key(client, mock_verify, auth_headers, uploads_dir):
    r = client.post("/v1/uploads/finalize", json={"key": "u/bob/abc123.jpg"},
                    headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_upload"


def test_finalize_requires_put_first(client, mock_verify, auth_headers, uploads_dir):
    signed = client.post("/v1/uploads/sign",
                         json={"content_type": "image/png", "size_bytes": 100},
                         headers=auth_headers).json()
    r = client.post("/v1/uploads/finalize", json={"key": signed["key"]}, headers=auth_headers)
    assert r.status_code == 422


def test_put_rejects_other_users_key(client, mock_verify, auth_headers, uploads_dir):
    r = client.put("/v1/uploads/raw/u/bob/abc123.jpg", content=b"x" * 10, headers=auth_headers)
    assert r.status_code == 403


def test_png_finalize_roundtrip(client, mock_verify, auth_headers, uploads_dir):
    raw = _plain_png()
    signed = client.post("/v1/uploads/sign",
                         json={"content_type": "image/png", "size_bytes": len(raw)},
                         headers=auth_headers).json()
    client.put(signed["upload_url"], content=raw, headers=auth_headers)
    fin = client.post("/v1/uploads/finalize", json={"key": signed["key"]}, headers=auth_headers)
    assert fin.status_code == 200
    assert fin.json()["content_type"] == "image/png"
    assert fin.json()["gps_stripped"] is False


def test_put_rejects_oversize_body(client, mock_verify, auth_headers, uploads_dir):
    signed = client.post("/v1/uploads/sign",
                         json={"content_type": "image/jpeg", "size_bytes": 100},
                         headers=auth_headers).json()
    r = client.put(signed["upload_url"], content=b"x" * (9 * 1024 * 1024), headers=auth_headers)
    assert r.status_code == 413


def test_public_rejects_unfinalized_upload(client, mock_verify, auth_headers, uploads_dir):
    """C7: raw PUT bytes (EXIF GPS still intact) must not be publicly served."""
    raw = _gps_jpeg()
    signed = client.post("/v1/uploads/sign",
                         json={"content_type": "image/jpeg", "size_bytes": len(raw)},
                         headers=auth_headers).json()
    put = client.put(signed["upload_url"], content=raw,
                     headers={**auth_headers, "Content-Type": "image/jpeg"})
    assert put.status_code == 200
    # never finalized -> 404, even though the file exists on disk
    r = client.get(signed["public_url"])
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_public_serves_finalized_upload(client, mock_verify, auth_headers, uploads_dir):
    """C7: PUT -> finalize -> GET public serves the EXIF-stripped file."""
    raw = _gps_jpeg()
    signed = client.post("/v1/uploads/sign",
                         json={"content_type": "image/jpeg", "size_bytes": len(raw)},
                         headers=auth_headers).json()
    client.put(signed["upload_url"], content=raw,
               headers={**auth_headers, "Content-Type": "image/jpeg"})
    fin = client.post("/v1/uploads/finalize", json={"key": signed["key"]},
                      headers=auth_headers)
    assert fin.status_code == 200
    r = client.get(signed["public_url"])
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_public_rejects_failed_finalize(client, mock_verify, auth_headers, uploads_dir):
    """C7: a failed finalize (not a valid image) leaves the row unfinalized."""
    signed = client.post("/v1/uploads/sign",
                         json={"content_type": "image/png", "size_bytes": 100},
                         headers=auth_headers).json()
    client.put(signed["upload_url"], content=b"definitely not an image" * 10,
               headers=auth_headers)
    r = client.post("/v1/uploads/finalize", json={"key": signed["key"]}, headers=auth_headers)
    assert r.status_code == 422
    r = client.get(signed["public_url"])
    assert r.status_code == 404


def test_finalize_other_users_key_does_not_finalize(client, mock_verify, auth_headers,
                                                    uploads_dir, mem_uploads):
    """C7 + ownership: another user's key cannot be finalized by me."""
    _, registry = mem_uploads
    r = client.post("/v1/uploads/finalize", json={"key": "u/bob/abc123.jpg"},
                    headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_upload"
    assert registry.is_finalized("u/bob/abc123.jpg") is False


def test_reput_resets_finalized(client, mock_verify, auth_headers, uploads_dir, mem_uploads):
    """A fresh raw PUT over a finalized key re-arms the gate until re-finalized."""
    _, registry = mem_uploads
    raw = _plain_png()
    signed = client.post("/v1/uploads/sign",
                         json={"content_type": "image/png", "size_bytes": len(raw)},
                         headers=auth_headers).json()
    key = signed["key"]
    client.put(signed["upload_url"], content=raw, headers=auth_headers)
    fin = client.post("/v1/uploads/finalize", json={"key": key}, headers=auth_headers)
    assert fin.status_code == 200
    assert registry.is_finalized(key) is True
    # new raw bytes (unstripped) -> back to unfinalized until finalized again
    client.put(signed["upload_url"], content=raw, headers=auth_headers)
    assert registry.is_finalized(key) is False
    assert client.get(signed["public_url"]).status_code == 404
    fin = client.post("/v1/uploads/finalize", json={"key": key}, headers=auth_headers)
    assert fin.status_code == 200
    assert client.get(signed["public_url"]).status_code == 200


def test_storage_rejects_path_traversal(uploads_dir):
    from app.storage import LocalStubStorage, StorageError

    store = LocalStubStorage(uploads_dir)
    # ".." cannot be expressed in a valid key at all
    with pytest.raises(StorageError):
        store.store_raw("u/alice/../../evil.jpg", b"x")
    with pytest.raises(StorageError):
        store.store_raw("u/alice/../../../tmp/evil.jpg", b"x")
    # a well-formed key still works
    store.store_raw("u/alice/abc123.jpg", b"x")


def test_gcs_skeleton_is_structure_only(monkeypatch):
    import os

    from app.storage import GCSStorage, StorageNotConfigured

    monkeypatch.setenv("GCS_BUCKET", "dummy-bucket")
    store = GCSStorage()
    with pytest.raises(StorageNotConfigured):
        store.sign_upload("alice", "image/jpeg", 100)


def test_unknown_backend_rejected(client, mock_verify, auth_headers, monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    r = client.post("/v1/uploads/sign",
                    json={"content_type": "image/jpeg", "size_bytes": 100},
                    headers=auth_headers)
    assert r.status_code == 501


# --- H5: GCS finalize pipeline + local-in-prod guard --------------------------

class _FakeGCSStore:
    """Dict-backed GCSBlobStore: exercises the download -> validate ->
    strip -> re-upload pipeline with no network."""

    def __init__(self):
        self.blobs: dict[str, tuple[bytes, str]] = {}

    def download(self, key: str) -> bytes:
        from app.storage import StorageError

        try:
            data, _ = self.blobs[key]
        except KeyError:
            raise StorageError("upload not found — PUT bytes to upload_url first")
        return data

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = (data, content_type)


def test_gcs_finalize_strips_gps_via_fake_store(monkeypatch):
    from app.storage import GCSStorage

    monkeypatch.setenv("GCS_BUCKET", "dummy-bucket")
    store = GCSStorage(_blob_store=_FakeGCSStore())
    key = "u/alice/abc123.jpg"
    store._blob_store.blobs[key] = (_gps_jpeg(), "image/jpeg")

    meta = store.finalize("alice", key)

    assert meta["gps_stripped"] is True
    assert meta["public_url"] == f"https://storage.googleapis.com/dummy-bucket/{key}"
    assert meta["content_type"] == "image/jpeg"
    # The re-uploaded bytes really have no GPS IFD.
    data, content_type = store._blob_store.blobs[key]
    assert content_type == "image/jpeg"
    assert IFD.GPSInfo not in Image.open(io.BytesIO(data)).getexif()


def test_gcs_finalize_rejects_bad_image_and_wrong_owner(monkeypatch):
    from app.storage import GCSStorage, StorageError

    monkeypatch.setenv("GCS_BUCKET", "dummy-bucket")
    store = GCSStorage(_blob_store=_FakeGCSStore())
    key = "u/alice/abc123.jpg"
    store._blob_store.blobs[key] = (_gps_jpeg(), "image/jpeg")

    with pytest.raises(StorageError):
        store.finalize("bob", key)  # ownership enforced
    with pytest.raises(StorageError):
        store.finalize("alice", "u/alice/missing.jpg")  # nothing uploaded

    store._blob_store.blobs["u/alice/bad.jpg"] = (b"not an image", "image/jpeg")
    with pytest.raises(StorageError):
        store.finalize("alice", "u/alice/bad.jpg")  # invalid image, not re-uploaded
    data, _ = store._blob_store.blobs["u/alice/bad.jpg"]
    assert data == b"not an image"  # original bytes untouched


def test_validate_storage_config_refuses_local_in_prod(monkeypatch):
    from app.config import get_settings
    from app.storage import get_storage, validate_storage_config

    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="must never run"):
        validate_storage_config(get_settings())
    with pytest.raises(RuntimeError):
        get_storage()  # lazy enforcement even if lifespan wiring is skipped

    monkeypatch.delenv("ENVIRONMENT")
    validate_storage_config(get_settings())  # dev default: no raise
    assert isinstance(get_storage(), object)

    monkeypatch.setenv("STORAGE_BACKEND", "gcs")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("GCS_BUCKET", "dummy-bucket")
    validate_storage_config(get_settings())  # gcs in prod: fine
