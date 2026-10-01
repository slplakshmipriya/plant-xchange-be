"""Listing-scoped messaging (API-080).

- ``POST /v1/threads``: open (or fetch the existing) thread about a listing.
  One thread per ``(listing_id, user)`` — the conversation is between the
  listing owner and the user who opened it.
- ``GET /v1/threads``: threads the caller participates in (opened by them or
  on their listings).
- ``POST /v1/threads/{id}/messages`` + ``GET .../messages``: send and read,
  participant-only, cursor-paginated (opaque base64 cursor, like the feed).
  Messages carry a ``kind`` ("text" | "photo"); photo messages also carry
  ``photo_url``.
- ``POST /v1/threads/{id}/attachments``: attach a photo from the
  ``/v1/uploads`` pipeline as a "photo"-kind message, participant-only.
  Takes a finalized upload key (``u/<uid>/<id>.<ext>``), never a URL; the
  server re-runs finalize (EXIF GPS stripping) and derives the public URL
  server-side — arbitrary client URLs are rejected (C8).
- Thread payloads include ``participant_uids`` (the two uids in the
  conversation: the opener and the listing owner) so clients can identify
  the other party.
- Geo rule: thread/message payloads never carry coordinates — exact geo stays
  hidden until the exchange-confirm flow (API-060) completes, and even then
  it is exchanged out of band, not through these serializers.
- At-rest encryption: ``messages.body`` holds Fernet ciphertext
  (``MESSAGE_ENCRYPTION_KEY``), encrypted on write in both repos and
  decrypted in ``_serialize_message`` — the single read boundary. Photo
  messages store the URL as the body, so the body is uniformly ciphertext.
- ``GET /v1/support/threads/{id}/messages``: support-staff dashboard
  (``SUPPORT_UIDS`` gate, same as dispute resolution). Support reads are
  H2-scoped: a thread is readable only while a report names its listing or
  one of its participants, or an open dispute references the listing —
  otherwise 403. ``reason`` is a controlled-vocabulary enum
  (``SupportViewReason``) written into ``moderation_views``. Paginated
  (``limit`` <= 100, opaque cursor like the participant path), and audit
  rows are written in one batched insert. The in-app disclosure
  ("Staff may review reported chats") and the user-visible
  "viewed by support" notice are Android-client follow-ups — they cannot be
  done server-side; this endpoint provides the data for both.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any, Protocol

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import get_current_uid
from .cache import CachedMessageRepo
from .crypto import MESSAGE_KEY_ENV, decrypt_text, encrypt_text
from .db import get_db_conn
from .listings import ListingRepo, get_listing_repo
from .moderation import (
    ModerationRepo,
    ModerationViewRepo,
    SupportViewReason,
    get_moderation_repo,
    get_moderation_view_repo,
    require_support,
)
from .storage import (
    GCSBlobStore,
    GCSStorage,
    StorageError,
    StorageNotConfigured,
    _check_key_format,
    get_storage,
)
from .images import (
    StoredImagesRepo,
    get_blob_store_or_none,
    get_images_repo,
    release_listing_images,
)
from .uploads import UploadsRegistry, get_uploads_registry
from .users import UserRepo, get_user_repo

router = APIRouter(prefix="/v1", tags=["messaging"])

PAGE_SIZE = 20

# H10: plaintext byte bound for message bodies. The 2000-char pydantic bound
# counts code points, but Fernet ciphertext size scales with UTF-8 *bytes*;
# 2000 emoji = 8000 bytes = ~10.7k chars of ciphertext, past any sane CHECK.
# 2800 bytes of plaintext encrypts to ~3.8k chars, comfortably under the
# 16384-char DB CHECK (migration 0027) with headroom for key/token overhead.
MAX_MESSAGE_BYTES = 2800


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        raise HTTPException(400, {"code": "invalid_cursor",
                                  "message": "Malformed message cursor"})
    if offset < 0:
        raise HTTPException(400, {"code": "invalid_cursor",
                                  "message": "Malformed message cursor"})
    return offset


class MessageRepo(Protocol):
    def get_or_create_thread(self, listing_id: str, uid: str) -> dict[str, Any]: ...
    def get_thread(self, thread_id: str) -> dict[str, Any] | None: ...
    def list_threads_for(self, uid: str, owner_listing_ids: list[str]) -> list[dict[str, Any]]: ...
    def add_message(self, thread_id: str, sender_uid: str, body: str,
                    kind: str = "text", photo_url: str | None = None) -> dict[str, Any]: ...
    def get_message(self, message_id: str) -> dict[str, Any] | None: ...
    def soft_delete_message(self, message_id: str) -> dict[str, Any] | None: ...
    def list_messages(self, thread_id: str, offset: int, limit: int) -> list[dict[str, Any]]: ...
    def count_messages(self, thread_id: str) -> int: ...
    # M10a: batched counts so list_threads doesn't issue one COUNT per thread.
    def counts_for_threads(self, thread_ids: list[str]) -> dict[str, int]: ...


def _serialize_thread(row: dict[str, Any], message_count: int = 0,
                      listing: dict[str, Any] | None = None) -> dict[str, Any]:
    # No geo fields, ever (API-080).
    participants = {row["created_by"]}
    owner_uid = listing.get("owner_uid") if listing else None
    if owner_uid:
        participants.add(owner_uid)
    return {
        "id": str(row["id"]),
        "listing_id": str(row["listing_id"]),
        "created_by": row["created_by"],
        "created_at": row.get("created_at"),
        "message_count": message_count,
        "participant_uids": sorted(participants),
    }


def _serialize_message(row: dict[str, Any],
                       reveal_deleted: bool = False) -> dict[str, Any]:
    """Public message shape. The single read boundary: ``body`` is decrypted
    here (fail closed — tampered token or missing key raises).

    Soft-deleted messages serialize as tombstones: content hidden from
    participants, but the slot (and pagination offsets) stay stable.
    ``reveal_deleted`` is for the audit-logged support path only — deleted
    content stays reviewable for reported-then-deleted messages.
    """
    if row.get("deleted_at") and not reveal_deleted:
        return {
            "id": str(row["id"]),
            "thread_id": str(row["thread_id"]),
            "sender_uid": row["sender_uid"],
            "kind": row.get("kind", "text"),
            "deleted": True,
            "deleted_at": row.get("deleted_at"),
            "body": None,
            "photo_url": None,
            "created_at": row.get("created_at"),
        }
    body = row.get("body")
    if not isinstance(body, str):
        # M24b: fail closed with the uniform RuntimeError envelope, not an
        # AttributeError on None (legacy NULL rows) — same as a bad token.
        raise RuntimeError("message body is missing or not ciphertext — refusing to decrypt")
    return {
        "id": str(row["id"]),
        "thread_id": str(row["thread_id"]),
        "sender_uid": row["sender_uid"],
        "kind": row.get("kind", "text"),
        "deleted": bool(row.get("deleted_at")),
        "body": decrypt_text(body, MESSAGE_KEY_ENV),
        "photo_url": row.get("photo_url"),
        "created_at": row.get("created_at"),
    }


class PostgresMessageRepo:
    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        c = d.get("created_at")
        d["created_at"] = c.isoformat() if hasattr(c, "isoformat") else c
        return d

    def get_or_create_thread(self, listing_id, uid):
        row = self._conn.execute(
            "SELECT * FROM threads WHERE listing_id = %s AND created_by = %s",
            (listing_id, uid)).fetchone()
        if row:
            return self._row(row)
        tid = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO threads (id, listing_id, created_by) VALUES (%s,%s,%s) "
            "ON CONFLICT (listing_id, created_by) DO NOTHING",
            (tid, listing_id, uid))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM threads WHERE listing_id = %s AND created_by = %s",
            (listing_id, uid)).fetchone()
        return self._row(row)

    def get_thread(self, thread_id):
        row = self._conn.execute(
            "SELECT * FROM threads WHERE id = %s", (thread_id,)).fetchone()
        return self._row(row) if row else None

    def list_threads_for(self, uid, owner_listing_ids):
        if owner_listing_ids:
            rows = self._conn.execute(
                "SELECT * FROM threads WHERE created_by = %s OR listing_id = ANY(%s) "
                "ORDER BY created_at DESC",
                (uid, owner_listing_ids)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM threads WHERE created_by = %s ORDER BY created_at DESC",
                (uid,)).fetchall()
        return [self._row(r) for r in rows]

    def add_message(self, thread_id, sender_uid, body, kind="text", photo_url=None):
        mid = str(uuid.uuid4())
        try:
            row = self._conn.execute(
                "INSERT INTO messages (id, thread_id, sender_uid, body, kind, photo_url) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (mid, thread_id, sender_uid, encrypt_text(body, MESSAGE_KEY_ENV),
                 kind, photo_url)).fetchone()
        except psycopg.errors.CheckViolation as exc:
            # H10: belt-and-suspenders behind the API byte bound — a message
            # that somehow exceeds the ciphertext CHECK is a 422, never a 500.
            self._conn.rollback()
            raise HTTPException(422, {"code": "message_too_long",
                                      "message": "Message exceeds the encrypted storage limit"}) from exc
        self._conn.commit()
        return self._row(row)

    def list_messages(self, thread_id, offset, limit):
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE thread_id = %s "
            "ORDER BY created_at, id LIMIT %s OFFSET %s",
            (thread_id, limit + 1, offset)).fetchall()
        return [self._row(r) for r in rows]

    def get_message(self, message_id):
        row = self._conn.execute(
            "SELECT * FROM messages WHERE id = %s", (message_id,)).fetchone()
        return self._row(row) if row else None

    def soft_delete_message(self, message_id):
        row = self._conn.execute(
            "UPDATE messages SET deleted_at = now() "
            "WHERE id = %s AND deleted_at IS NULL RETURNING *",
            (message_id,)).fetchone()
        self._conn.commit()
        return self._row(row) if row else None

    def count_messages(self, thread_id):
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE thread_id = %s",
            (thread_id,)).fetchone()
        return int(row["n"])

    def counts_for_threads(self, thread_ids):
        # M10a: one query for all counts instead of N.
        if not thread_ids:
            return {}
        rows = self._conn.execute(
            "SELECT thread_id, COUNT(*) AS n FROM messages "
            "WHERE thread_id = ANY(%s) GROUP BY thread_id",
            (list(thread_ids),)).fetchall()
        return {str(r["thread_id"]): int(r["n"]) for r in rows}


class MemoryMessageRepo:
    def __init__(self):
        self._threads: dict[str, dict[str, Any]] = {}
        self._by_listing_user: dict[tuple[str, str], str] = {}
        self._messages: dict[str, list[dict[str, Any]]] = {}

    def get_or_create_thread(self, listing_id, uid):
        from .listings import utcnow
        key = (listing_id, uid)
        if key in self._by_listing_user:
            return dict(self._threads[self._by_listing_user[key]])
        tid = str(uuid.uuid4())
        row = {"id": tid, "listing_id": listing_id, "created_by": uid,
               "created_at": utcnow().isoformat()}
        self._threads[tid] = row
        self._by_listing_user[key] = tid
        self._messages[tid] = []
        return dict(row)

    def get_thread(self, thread_id):
        row = self._threads.get(thread_id)
        return dict(row) if row else None

    def list_threads_for(self, uid, owner_listing_ids):
        owned = set(owner_listing_ids)
        rows = [t for t in self._threads.values()
                if t["created_by"] == uid or t["listing_id"] in owned]
        rows.sort(key=lambda t: t["created_at"], reverse=True)
        return [dict(t) for t in rows]

    def add_message(self, thread_id, sender_uid, body, kind="text", photo_url=None):
        from .listings import utcnow
        row = {"id": str(uuid.uuid4()), "thread_id": thread_id,
               "sender_uid": sender_uid,
               "body": encrypt_text(body, MESSAGE_KEY_ENV),
               "kind": kind, "photo_url": photo_url,
               "created_at": utcnow().isoformat()}
        self._messages[thread_id].append(row)
        return dict(row)

    def list_messages(self, thread_id, offset, limit):
        msgs = sorted(self._messages.get(thread_id, []),
                      key=lambda m: (m["created_at"], m["id"]))
        return [dict(m) for m in msgs[offset:offset + limit + 1]]

    def get_message(self, message_id):
        for msgs in self._messages.values():
            for m in msgs:
                if m["id"] == message_id:
                    return dict(m)
        return None

    def soft_delete_message(self, message_id):
        from .listings import utcnow
        for msgs in self._messages.values():
            for m in msgs:
                if m["id"] == message_id and not m.get("deleted_at"):
                    m["deleted_at"] = utcnow().isoformat()
                    return dict(m)
        return None

    def count_messages(self, thread_id):
        return len(self._messages.get(thread_id, []))

    def counts_for_threads(self, thread_ids):
        # M10a: one pass instead of N len() calls across the wire.
        return {tid: len(self._messages.get(tid, [])) for tid in thread_ids}


def get_message_repo(conn=Depends(get_db_conn)) -> MessageRepo:
    return CachedMessageRepo(PostgresMessageRepo(conn))


class ThreadIn(BaseModel):
    listing_id: str = Field(min_length=1)


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class AttachmentIn(BaseModel):
    # C8: the /v1/uploads pipeline key (u/<uid>/<id>.<ext>) — never a URL.
    uploadKey: str = Field(min_length=1, max_length=200)


def _participant_or_403(thread: dict[str, Any] | None,
                        listing: dict[str, Any] | None, uid: str) -> dict[str, Any]:
    if thread is None:
        raise HTTPException(404, {"code": "thread_not_found",
                                  "message": "No such thread"})
    parties = {thread["created_by"]}
    if listing is not None:
        parties.add(listing["owner_uid"])
    if uid not in parties:
        raise HTTPException(403, {"code": "not_a_participant",
                                  "message": "This conversation is private"})
    return thread


@router.post("/threads", status_code=201, tags=["messaging"])
def open_thread(
    data: ThreadIn,
    uid: str = Depends(get_current_uid),
    repo: MessageRepo = Depends(get_message_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    user_repo: UserRepo = Depends(get_user_repo),
) -> dict[str, Any]:
    """Open (or fetch) your thread about a listing. Idempotent per listing."""
    listing = listing_repo.get(data.listing_id)
    if listing is None:
        raise HTTPException(404, {"code": "listing_not_found",
                                  "message": "No such listing"})
    if user_repo.get(uid) is None:
        raise HTTPException(400, {"code": "profile_required",
                                  "message": "Create a profile (POST /v1/users) before messaging"})
    thread = repo.get_or_create_thread(data.listing_id, uid)
    return _serialize_thread(thread, repo.count_messages(thread["id"]), listing)


@router.get("/threads", tags=["messaging"])
def list_threads(
    uid: str = Depends(get_current_uid),
    repo: MessageRepo = Depends(get_message_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
) -> dict[str, Any]:
    """Threads you opened plus threads on your listings."""
    # M10a: one listing query (the owned set) and one COUNT query for all
    # threads. Threads the caller opened on *others'* listings still need
    # one get each — a batched get_many on ListingRepo would remove that,
    # but ListingRepo lives in listings.py (another track owns it).
    owned = listing_repo.list_by_owner(uid)
    owned_ids = [l["id"] for l in owned]
    owned_map = {str(l["id"]): l for l in owned}
    threads = repo.list_threads_for(uid, owned_ids)
    counts = repo.counts_for_threads([str(t["id"]) for t in threads])
    out = []
    for t in threads:
        lid = str(t["listing_id"])
        listing = owned_map.get(lid)
        if listing is None:
            listing = listing_repo.get(lid)
        out.append(_serialize_thread(t, counts.get(str(t["id"]), 0), listing))
    return {"threads": out}


@router.post("/threads/{thread_id}/messages", status_code=201, tags=["messaging"])
def send_message(
    thread_id: str,
    data: MessageIn,
    uid: str = Depends(get_current_uid),
    repo: MessageRepo = Depends(get_message_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
) -> dict[str, Any]:
    """Send a message. Participants only."""
    thread = repo.get_thread(thread_id)
    listing = listing_repo.get(thread["listing_id"]) if thread else None
    _participant_or_403(thread, listing, uid)
    body = data.body.strip()
    if not body:
        raise HTTPException(422, {"code": "empty_message",
                                  "message": "Message body cannot be blank"})
    if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        # H10: the 2000-char pydantic bound counts code points, but Fernet
        # ciphertext grows with UTF-8 bytes — 2000 emoji would blow the DB
        # CHECK and 500. Enforce the byte bound here (422); the
        # CheckViolation catch in PostgresMessageRepo.add_message is the
        # backstop.
        raise HTTPException(422, {"code": "message_too_long",
                                  "message": f"Message body exceeds {MAX_MESSAGE_BYTES} bytes "
                                             "(multibyte characters count toward the limit)"})
    return _serialize_message(repo.add_message(thread_id, uid, body))


@router.post("/threads/{thread_id}/attachments", status_code=201, tags=["messaging"])
def attach_photo(
    thread_id: str,
    data: AttachmentIn,
    uid: str = Depends(get_current_uid),
    repo: MessageRepo = Depends(get_message_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    registry: UploadsRegistry = Depends(get_uploads_registry),
) -> dict[str, Any]:
    """Attach a photo to a thread as a "photo"-kind message. Participants only.

    C8 contract: chat photos MUST come through the ``/v1/uploads`` pipeline
    (sign -> PUT -> finalize) so EXIF GPS stripping applies uniformly. The
    client sends ``uploadKey`` — the ``u/<uid>/<id>.<ext>`` key returned by
    ``POST /v1/uploads/sign`` — never a URL.

    Server-side verification, in order:

    1. Key shape: must match the upload key format. Arbitrary external URLs
       (http(s) or otherwise) are rejected with 422.
    2. Ownership: the uploads registry must show the sender ran this key
       through the finalize pipeline — their own upload, or a dedupe survivor
       key the pipeline handed back for their byte-identical bytes
       (``finalize_upload`` records the pipeline-produced key under the
       sender's uid). A bare uid-segment comparison is stale here: perceptual
       dedupe intentionally shares one stored object across users. 403
       otherwise. As a fallback the key's own uid segment is also accepted,
       so the original uploader keeps working even after a later deduper's
       finalize re-points the single-owner registry row at themselves.
    3. Pipeline: when the key is already finalized, the bytes are proven
       EXIF-clean and the public URL is derived directly — re-running
       ``storage.finalize`` would pointlessly re-upload shared bytes (and trip
       ``_check_key`` on the survivor's foreign uid segment). Otherwise
       ``storage.finalize`` is (re-)run server-side as a backstop: it proves
       the bytes exist, are a real image within the size cap, and writes back
       the GPS-stripped rendition. Missing/corrupt/oversize bytes -> 422;
       unconfigured storage -> 501. The key is then marked finalized in the
       uploads registry (C7) so ``GET /v1/uploads/public/{key}`` serves it.

    The public URL is derived server-side from the finalize metadata and
    stored as the message body (encrypted at rest, as before) and as
    ``photo_url`` — the client never supplies a URL. Participant and support
    visibility are unchanged.
    """
    thread = repo.get_thread(thread_id)
    listing = listing_repo.get(thread["listing_id"]) if thread else None
    _participant_or_403(thread, listing, uid)
    key = data.uploadKey.strip()
    try:
        _check_key_format(key)
    except StorageError:
        raise HTTPException(422, {"code": "invalid_upload_key",
                                  "message": "uploadKey must be a /v1/uploads key "
                                             "(u/<uid>/<id>.<ext>); arbitrary URLs are rejected"})
    if registry.owner_of(key) != uid and key.split("/")[1] != uid:
        raise HTTPException(403, {"code": "not_your_upload",
                                  "message": "Upload key does not belong to the sender"})
    if registry.is_finalized(key):
        # Already EXIF-clean via an earlier finalize (own or deduped): the
        # re-finalize backstop below exists to catch clients that skipped
        # finalize, so there is nothing left for it to prove.
        public_url = get_storage().public_url_for(key)
    else:
        try:
            meta = get_storage().finalize(uid, key)
        except StorageNotConfigured as exc:
            raise HTTPException(501, {"code": "storage_not_configured",
                                      "message": str(exc)})
        except StorageError as exc:
            raise HTTPException(422, {"code": "invalid_upload",
                                      "message": str(exc)})
        # C7 registry: the attach-time finalize proves the bytes are EXIF-clean,
        # so record the key as finalized — otherwise serve_public would 404 it.
        registry.record_raw(key, uid)
        registry.mark_finalized(key)
        public_url = meta["public_url"]
    # L7: public_url is ASCII by construction (scheme://host/path, with any
    # non-ASCII percent-encoded), so storing it as the encrypted body stays
    # well under the ciphertext CHECK — unlike free multibyte text, which is
    # why H10 needs the separate plaintext byte bound.
    return _serialize_message(
        repo.add_message(thread_id, uid, public_url, kind="photo", photo_url=public_url))


@router.delete("/threads/{thread_id}/messages/{message_id}", tags=["messaging"])
def delete_message(
    thread_id: str,
    message_id: str,
    uid: str = Depends(get_current_uid),
    repo: MessageRepo = Depends(get_message_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    images_repo: StoredImagesRepo = Depends(get_images_repo),
    blob_store: GCSBlobStore | None = Depends(get_blob_store_or_none),
) -> dict[str, Any]:
    """Delete your own message. Sender only — participants cannot delete each
    other's messages.

    Soft delete: the message becomes a tombstone (``deleted: true``, content
    hidden) so pagination offsets stay stable for both participants. Photo
    messages release their refcounted GCS object; shared dedupe objects
    survive until their last reference is released. Idempotent: deleting an
    already-deleted message returns its tombstone.
    """
    thread = repo.get_thread(thread_id)
    listing = listing_repo.get(thread["listing_id"]) if thread else None
    _participant_or_403(thread, listing, uid)
    msg = repo.get_message(message_id)
    if msg is None or str(msg["thread_id"]) != thread_id:
        raise HTTPException(404, {"code": "message_not_found",
                                  "message": "No such message in this thread"})
    if msg["sender_uid"] != uid:
        raise HTTPException(403, {"code": "not_your_message",
                                  "message": "Only the sender can delete a message"})
    if msg.get("deleted_at"):
        return _serialize_message(msg)
    deleted = repo.soft_delete_message(message_id)
    if msg.get("kind") == "photo" and msg.get("photo_url"):
        backend = get_storage()
        bucket = backend.bucket if isinstance(backend, GCSStorage) else None
        # Refcounted release: no-op on the local stub; on GCS the object is
        # deleted only when its refcount hits 0 (shared dedupe objects
        # survive for their other referrers).
        release_listing_images([msg["photo_url"]], images_repo=images_repo,
                               blob_store=blob_store, bucket=bucket)
    return _serialize_message(deleted)


@router.get("/threads/{thread_id}/messages", tags=["messaging"])
def read_messages(
    thread_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=PAGE_SIZE, ge=1, le=100),
    uid: str = Depends(get_current_uid),
    repo: MessageRepo = Depends(get_message_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
) -> dict[str, Any]:
    """Read a thread's messages, oldest first. Participants only."""
    thread = repo.get_thread(thread_id)
    listing = listing_repo.get(thread["listing_id"]) if thread else None
    _participant_or_403(thread, listing, uid)
    offset = _decode_cursor(cursor)
    rows = repo.list_messages(thread_id, offset, limit)
    has_more = len(rows) > limit
    return {
        "messages": [_serialize_message(m) for m in rows[:limit]],
        "next_cursor": _encode_cursor(offset + limit) if has_more else None,
    }


@router.get("/support/threads/{thread_id}/messages", tags=["support"])
def support_read_messages(
    thread_id: str,
    reason: SupportViewReason = Query(
        description="Controlled-vocabulary justification for the review "
                    "(written into the moderation_views audit log)"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    uid: str = Depends(get_current_uid),
    repo: MessageRepo = Depends(get_message_repo),
    listing_repo: ListingRepo = Depends(get_listing_repo),
    mod_repo: ModerationRepo = Depends(get_moderation_repo),
    view_repo: ModerationViewRepo = Depends(get_moderation_view_repo),
) -> dict[str, Any]:
    """Support dashboard: decrypted thread view for moderation.

    Support staff only (``SUPPORT_UIDS`` — 403 otherwise, fail closed).
    Not participant-scoped — that is the point — but H2-scoped: a thread is
    readable only while a report names its listing or one of its
    participants, or an open dispute references the listing (403
    ``no_open_case`` otherwise). ``reason`` is a controlled-vocabulary enum
    (free text was self-reported and unauditable). Paginated: ``limit`` <=
    100 with the same opaque cursor as the participant path (M2 — no more
    10k-message decrypt-and-commit storms). Every message returned writes
    one audit row (viewer, thread, message, reason) into
    ``moderation_views`` in a single batched insert.

    Android follow-ups (client-side, not implementable here): in-app copy
    disclosing "Staff may review reported chats", and a user-visible
    "viewed by support" notice on reviewed threads.
    """
    require_support(uid)
    thread = repo.get_thread(thread_id)
    if thread is None:
        raise HTTPException(404, {"code": "thread_not_found",
                                  "message": "No such thread"})
    listing = listing_repo.get(str(thread["listing_id"]))
    participants = {thread["created_by"]}
    if listing is not None:
        participants.add(listing["owner_uid"])
    if not mod_repo.thread_has_open_case(str(thread["listing_id"]),
                                         sorted(participants)):
        raise HTTPException(403, {"code": "no_open_case",
                                  "message": "Support reads are limited to threads "
                                             "linked to an open report or dispute"})
    offset = _decode_cursor(cursor)
    rows = repo.list_messages(thread_id, offset, limit)
    has_more = len(rows) > limit
    page = rows[:limit]
    view_repo.log_views_batch([
        {"viewer_uid": uid, "thread_id": thread_id,
         "message_id": str(m["id"]), "reason": reason.value}
        for m in page
    ])
    return {
        "thread_id": thread_id,
        "messages": [_serialize_message(m, reveal_deleted=True) for m in page],  # decrypts; fail closed
        "next_cursor": _encode_cursor(offset + limit) if has_more else None,
    }
