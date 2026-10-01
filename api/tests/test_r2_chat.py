"""CHAT track: thread attachments (photo messages) + participant_uids."""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from PIL.ExifTags import IFD


@pytest.fixture()
def uploads_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    return tmp_path / "uploads"


@pytest.fixture()
def chat_client(client, monkeypatch):
    from app import listings as listings_mod
    from app import msg as msg_mod
    from app import notify as notify_mod
    from app import uploads as uploads_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from app.storage import MemoryUploadsRegistry
    from conftest import wire_credit_repo
    from conftest import wire_images_repo
    import app.auth as auth_mod

    urepo = users_mod.MemoryUserRepo()
    lrepo = listings_mod.MemoryListingRepo()
    mrepo = msg_mod.MemoryMessageRepo()
    wrepo = wantlist_mod.MemoryWantRepo()
    nrepo = notify_mod.MemoryNotificationRepo()
    client.app.dependency_overrides[users_mod.get_user_repo] = lambda: urepo
    client.app.dependency_overrides[listings_mod.get_listing_repo] = lambda: lrepo
    client.app.dependency_overrides[msg_mod.get_message_repo] = lambda: mrepo
    client.app.dependency_overrides[wantlist_mod.get_want_repo] = lambda: wrepo
    client.app.dependency_overrides[notify_mod.get_notification_repo] = lambda: nrepo
    # C7/C8: the uploads finalize registry is Postgres-backed by default
    # (503 without DATABASE_URL) — use a shared in-memory one here so both
    # /v1/uploads/* and the chat attach path see the same finalize state.
    _uploads_registry = MemoryUploadsRegistry()
    client.app.dependency_overrides[uploads_mod.get_uploads_registry] = \
        lambda: _uploads_registry
    wire_credit_repo(client)
    wire_images_repo(client)

    def fake(token: str) -> dict:
        if token == "good-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "bob-token":
            return {"uid": "bob"}
        if token == "mallory-token":
            return {"uid": "mallory"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)
    return client


ALICE = {"Authorization": "Bearer good-token"}
BOB = {"Authorization": "Bearer bob-token"}
MALLORY = {"Authorization": "Bearer mallory-token"}


def _listing_id(client):
    """Alice owns a listing; bob has a profile."""
    for headers, name in ((ALICE, "Alice"), (BOB, "Bob")):
        r = client.post("/v1/users", json={"display_name": name, "age_attestation": True}, headers=headers)
        assert r.status_code == 200, r.text
    r = client.post("/v1/listings", json={
        "type": "seedling",
        "photos": ["https://example.com/t.jpg"],
        "variety": "Basil",
        "quantity": 6, "unit": "starts",
        "credit_cost": 1, "spray_disclosure": "unsprayed",
        "status": "live",
        "geo_lat": 33.4152, "geo_lon": -111.8315,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }, headers=ALICE)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _thread_id(client, lid):
    r = client.post("/v1/threads", json={"listing_id": lid}, headers=BOB)
    assert r.status_code == 201, r.text
    return r.json()["id"]


PHOTO = "https://cdn.example.com/swaps/tomato-1.jpg"  # legacy: now rejected (C8)


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


def _finalized_key(client, headers, raw: bytes,
                   content_type: str = "image/jpeg") -> tuple[str, str]:
    """Run the full /v1/uploads pipeline; return (key, public_url)."""
    signed = client.post("/v1/uploads/sign",
                         json={"content_type": content_type, "size_bytes": len(raw)},
                         headers=headers).json()
    key = signed["key"]
    put = client.put(signed["upload_url"], content=raw, headers=headers)
    assert put.status_code == 200, put.text
    fin = client.post("/v1/uploads/finalize", json={"key": key}, headers=headers)
    assert fin.status_code == 200, fin.text
    return key, fin.json()["public_url"]


def test_attachment_creates_photo_message_visible_in_thread(chat_client, uploads_dir):
    tid = _thread_id(chat_client, _listing_id(chat_client))
    raw = _gps_jpeg()
    assert IFD.GPSInfo in Image.open(io.BytesIO(raw)).getexif()  # really has GPS
    key, public_url = _finalized_key(chat_client, BOB, raw)
    assert public_url == f"/v1/uploads/public/{key}"

    r = chat_client.post(f"/v1/threads/{tid}/attachments",
                         json={"uploadKey": key}, headers=BOB)
    assert r.status_code == 201, r.text
    msg = r.json()
    assert msg["kind"] == "photo"
    # The stored URL is server-derived from the pipeline — never client input.
    assert msg["photo_url"] == public_url
    assert msg["body"] == public_url  # body round-trips through at-rest encryption
    assert msg["sender_uid"] == "bob"
    assert msg["thread_id"] == tid

    # The photo message shows up in the thread's message list.
    msgs = chat_client.get(f"/v1/threads/{tid}/messages", headers=BOB).json()["messages"]
    assert [m["id"] for m in msgs] == [msg["id"]]
    assert msgs[0]["kind"] == "photo"
    assert msgs[0]["photo_url"] == public_url

    # C8: the attached bytes went through the EXIF pipeline — no GPS survives.
    assert IFD.GPSInfo not in Image.open(uploads_dir / key).getexif()


def test_attachment_finalizes_unfinalized_key_server_side(chat_client, uploads_dir):
    """A key that was PUT but never finalized is completed by the attach
    endpoint itself (finalize is idempotent) rather than rejected — the
    security property is that the stored bytes are EXIF-clean, which the
    server guarantees either way."""
    tid = _thread_id(chat_client, _listing_id(chat_client))
    raw = _gps_jpeg()
    signed = chat_client.post("/v1/uploads/sign",
                              json={"content_type": "image/jpeg", "size_bytes": len(raw)},
                              headers=BOB).json()
    key = signed["key"]
    put = chat_client.put(signed["upload_url"], content=raw, headers=BOB)
    assert put.status_code == 200
    # NOTE: deliberately skipping POST /v1/uploads/finalize.

    r = chat_client.post(f"/v1/threads/{tid}/attachments",
                         json={"uploadKey": key}, headers=BOB)
    assert r.status_code == 201, r.text
    assert r.json()["photo_url"] == f"/v1/uploads/public/{key}"
    assert IFD.GPSInfo not in Image.open(uploads_dir / key).getexif()


def test_attachment_forbidden_for_non_participant(chat_client, uploads_dir):
    tid = _thread_id(chat_client, _listing_id(chat_client))
    r = chat_client.post(f"/v1/threads/{tid}/attachments",
                         json={"uploadKey": "u/mallory/abc123.jpg"}, headers=MALLORY)
    assert r.status_code == 403
    assert r.json()["code"] == "not_a_participant"


def test_attachment_thread_not_found(chat_client, uploads_dir):
    _listing_id(chat_client)
    r = chat_client.post("/v1/threads/00000000-0000-0000-0000-000000000000/attachments",
                         json={"uploadKey": "u/bob/abc123.jpg"}, headers=BOB)
    assert r.status_code == 404
    assert r.json()["code"] == "thread_not_found"


def test_attachment_rejects_arbitrary_urls(chat_client, uploads_dir):
    tid = _thread_id(chat_client, _listing_id(chat_client))
    for bad in (PHOTO,  # the old contract: arbitrary https URL
                "http://example.com/p.jpg",
                "ftp://example.com/p.jpg",
                "javascript:alert(1)",
                "not-a-url",
                "u/bob/evil.jpg/../../x"):  # traversal can't be expressed as a key
        r = chat_client.post(f"/v1/threads/{tid}/attachments",
                             json={"uploadKey": bad}, headers=BOB)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["code"] == "invalid_upload_key"
    # Empty string fails pydantic validation.
    r = chat_client.post(f"/v1/threads/{tid}/attachments",
                         json={"uploadKey": ""}, headers=BOB)
    assert r.status_code == 422


def test_attachment_rejects_foreign_or_missing_upload_key(chat_client, uploads_dir):
    tid = _thread_id(chat_client, _listing_id(chat_client))
    # Bob's finalized key cannot be attached by Alice (also a participant).
    bob_key, _ = _finalized_key(chat_client, BOB, _plain_png(), "image/png")
    assert bob_key.startswith("u/bob/")
    r = chat_client.post(f"/v1/threads/{tid}/attachments",
                         json={"uploadKey": bob_key}, headers=ALICE)
    assert r.status_code == 403
    assert r.json()["code"] == "not_your_upload"

    # Well-formed and owned by the sender, but no bytes were ever PUT -> 422.
    r = chat_client.post(f"/v1/threads/{tid}/attachments",
                         json={"uploadKey": "u/bob/0123456789abcdef0123456789abcdef.png"},
                         headers=BOB)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_upload"

    # PUT non-image bytes -> finalize rejects -> 422.
    signed = chat_client.post("/v1/uploads/sign",
                              json={"content_type": "image/png", "size_bytes": 100},
                              headers=BOB).json()
    chat_client.put(signed["upload_url"], content=b"definitely not an image" * 10,
                    headers=BOB)
    r = chat_client.post(f"/v1/threads/{tid}/attachments",
                         json={"uploadKey": signed["key"]}, headers=BOB)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_upload"


def test_attachment_accepts_dedupe_survivor_key_from_other_user(chat_client, uploads_dir):
    """Perceptual dedupe: Bob uploaded identical bytes first; Alice's finalize
    deleted her temp bytes and handed back Bob's key (``deduped: true``),
    recording (survivor_key, owner=alice, finalized) in the registry — exactly
    what ``finalize_upload`` does on the GCS dedupe path. Attaching that key
    must succeed: Alice demonstrably possessed the bytes and ran the pipeline;
    the old uid-segment check wrongly 403'd this as ``not_your_upload``."""
    from app import uploads as uploads_mod
    tid = _thread_id(chat_client, _listing_id(chat_client))
    bob_key, _ = _finalized_key(chat_client, BOB, _plain_png(), "image/png")
    assert bob_key.startswith("u/bob/")
    # Simulate Alice's dedupe finalize: survivor key recorded under HER uid.
    registry = chat_client.app.dependency_overrides[uploads_mod.get_uploads_registry]()
    registry.record_raw(bob_key, "alice")
    registry.mark_finalized(bob_key)

    r = chat_client.post(f"/v1/threads/{tid}/attachments",
                         json={"uploadKey": bob_key}, headers=ALICE)
    assert r.status_code == 201, r.text
    msg = r.json()
    assert msg["kind"] == "photo"
    assert msg["photo_url"] == f"/v1/uploads/public/{bob_key}"
    assert msg["sender_uid"] == "alice"


def test_attachment_original_uploader_survives_dedupe_clobber(chat_client, uploads_dir):
    """The single-owner registry row gets re-pointed at the deduper, but the
    original uploader must still be able to attach their own key — via the
    uid-segment fallback."""
    from app import uploads as uploads_mod
    tid = _thread_id(chat_client, _listing_id(chat_client))
    bob_key, _ = _finalized_key(chat_client, BOB, _plain_png(), "image/png")
    registry = chat_client.app.dependency_overrides[uploads_mod.get_uploads_registry]()
    registry.record_raw(bob_key, "alice")  # Alice's dedupe clobbers the row
    registry.mark_finalized(bob_key)

    r = chat_client.post(f"/v1/threads/{tid}/attachments",
                         json={"uploadKey": bob_key}, headers=BOB)
    assert r.status_code == 201, r.text
    assert r.json()["photo_url"] == f"/v1/uploads/public/{bob_key}"


def test_thread_responses_include_participant_uids(chat_client):
    lid = _listing_id(chat_client)
    tid = _thread_id(chat_client, lid)

    # Detail (open is idempotent) carries both participants.
    opened = chat_client.post("/v1/threads", json={"listing_id": lid}, headers=BOB).json()
    assert opened["id"] == tid
    assert opened["participant_uids"] == ["alice", "bob"]

    # List view carries them too, for both sides of the conversation.
    for headers in (BOB, ALICE):
        threads = chat_client.get("/v1/threads", headers=headers).json()["threads"]
        mine = next(t for t in threads if t["id"] == tid)
        assert mine["participant_uids"] == ["alice", "bob"]

    # Existing fields are untouched.
    assert set(opened) == {"id", "listing_id", "created_by", "created_at",
                           "message_count", "participant_uids"}
    assert opened["created_by"] == "bob"


def test_text_message_kind_defaults_to_text(chat_client):
    tid = _thread_id(chat_client, _listing_id(chat_client))
    r = chat_client.post(f"/v1/threads/{tid}/messages",
                         json={"body": "still available?"}, headers=BOB)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "text"
    assert r.json()["photo_url"] is None
