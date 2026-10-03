"""Pre-publish message moderation gate (NL moderateText + wordlist fallback).

A blocked message must never reach the repo: no insert, no encryption, no
cache entry — the thread simply never sees it. These tests pin that, the
422 contract the Android popup keys on, and the scorer's own behavior
(threshold boundary, outage degradation, garden-talk false positives).
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def mem_mod(client, monkeypatch):
    from app import listings as listings_mod
    from app import msg as msg_mod
    from app import notify as notify_mod
    from app import users as users_mod
    from app import wantlist as wantlist_mod
    from app.text_moderation import GoogleNLModerator, NullModerator
    from conftest import wire_credit_repo
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
    wire_credit_repo(client)

    def fake(token: str) -> dict:
        if token == "good-token":
            return {"uid": "alice", "phone_number": "+15551234567"}
        if token == "bob-token":
            return {"uid": "bob"}
        if token == "mallory-token":
            return {"uid": "mallory"}
        raise ValueError("bad token")

    monkeypatch.setattr(auth_mod, "verify_id_token", fake)

    def use_moderator(moderator):
        client.app.dependency_overrides[msg_mod.get_text_moderator] = lambda: moderator

    def use_nl_scores(scores_or_exc):
        def fetch(_text):
            if isinstance(scores_or_exc, Exception):
                raise scores_or_exc
            return scores_or_exc
        use_moderator(GoogleNLModerator(threshold=0.8, fetch_scores=fetch))

    return client, mrepo, use_moderator, use_nl_scores, NullModerator


ALICE = {"Authorization": "Bearer good-token"}
BOB = {"Authorization": "Bearer bob-token"}


def _thread(client):
    for headers, name in ((ALICE, "Alice"), (BOB, "Bob")):
        r = client.post("/v1/users", json={"display_name": name,
                                           "age_attestation": True},
                        headers=headers)
        assert r.status_code == 200, r.text
    from datetime import datetime, timedelta, timezone
    r = client.post("/v1/listings", json={
        "type": "seedling", "photos": ["https://example.com/t.jpg"],
        "variety": "Basil", "quantity": 6, "unit": "starts",
        "credit_cost": 1, "spray_disclosure": "unsprayed", "status": "live",
        "geo_lat": 33.4152, "geo_lon": -111.8315,
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(days=30)).isoformat(),
    }, headers=ALICE)
    assert r.status_code == 201, r.text
    return client.post("/v1/threads", json={"listing_id": r.json()["id"]},
                       headers=BOB).json()["id"]


def _send(client, tid, body, headers=BOB):
    return client.post(f"/v1/threads/{tid}/messages",
                       json={"body": body}, headers=headers)


def _count(mem_mod, tid):
    return mem_mod[1].count_messages(tid)


# ------------------------------------------------------------- gate behavior

def test_blocked_message_is_never_persisted(mem_mod):
    client, _, use_moderator, _, _ = mem_mod
    use_moderator(_BlockingModerator())
    tid = _thread(client)

    r = _send(client, tid, "you are a worthless idiot")
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "message_inappropriate"

    # Nothing in the repo, nothing readable back — as if never typed.
    assert _count(mem_mod, tid) == 0
    page = client.get(f"/v1/threads/{tid}/messages", headers=BOB).json()
    assert page["messages"] == []


def test_allowed_message_flows_through(mem_mod):
    client, _, _, use_nl_scores, _ = mem_mod
    use_nl_scores({"Toxic": 0.05, "Profanity": 0.1, "Insult": 0.02})
    tid = _thread(client)

    r = _send(client, tid, "hi! is the basil still available?")
    assert r.status_code == 201, r.text
    assert _count(mem_mod, tid) == 1


def test_nl_threshold_boundary(mem_mod):
    client, _, _, use_nl_scores, _ = mem_mod
    tid = _thread(client)

    use_nl_scores({"Toxic": 0.79})
    assert _send(client, tid, "borderline text").status_code == 201

    use_nl_scores({"Toxic": 0.8})  # >= threshold blocks
    r = _send(client, tid, "borderline text")
    assert r.status_code == 422
    assert r.json()["code"] == "message_inappropriate"
    assert _count(mem_mod, tid) == 1  # only the allowed one landed


def test_nl_outage_degrades_to_wordlist(mem_mod):
    client, _, _, use_nl_scores, _ = mem_mod
    use_nl_scores(RuntimeError("nl down"))
    tid = _thread(client)

    # Clean text still sends while NL is down...
    assert _send(client, tid, "can I pick up Saturday?").status_code == 201
    # ...but blatant profanity is still caught by the fallback.
    r = _send(client, tid, "this is fucking ridiculous")
    assert r.status_code == 422
    assert _count(mem_mod, tid) == 1


def test_disabled_gate_blocks_nothing(mem_mod):
    client, _, use_moderator, _, null_cls = mem_mod
    use_moderator(null_cls())
    tid = _thread(client)
    assert _send(client, tid, "this is fucking ridiculous").status_code == 201


def test_participant_check_precedes_moderation(mem_mod):
    # A non-participant gets 403 and moderation never runs for them — the
    # gate only ever sees text from actual conversation participants.
    client, _, use_moderator, _, _ = mem_mod
    calls = []

    class RecordingModerator:
        def moderate(self, text):
            calls.append(text)
            from app.text_moderation import ModerationVerdict
            return ModerationVerdict(blocked=True, source="nl",
                                     scores={"Toxic": 0.99})

    use_moderator(RecordingModerator())
    tid = _thread(client)
    r = client.post(f"/v1/threads/{tid}/messages",
                    json={"body": "sneaking in"},
                    headers={"Authorization": "Bearer mallory-token"})
    assert r.status_code == 403, r.text
    assert calls == []


# ------------------------------------------------------------ scorer behavior

def test_wordlist_matching_is_word_boundary():
    from app.text_moderation import WordlistModerator
    mod = WordlistModerator()
    assert mod.moderate("this is fucking ridiculous").blocked
    assert mod.moderate("You BITCH").blocked  # case-insensitive
    # Garden vocabulary and near-miss spellings must not trip the fallback.
    assert not mod.moderate("grab your hoe, we're weeding the beds").blocked
    assert not mod.moderate("I use weed killer on the gravel path").blocked
    assert not mod.moderate("damn aphids ate all my kale").blocked
    assert not mod.moderate("the grapevines need pruning").blocked
    assert not mod.moderate("fresh Scunthorpe manure for the beds").blocked


def test_nl_moderator_latches_after_failure():
    from app.text_moderation import GoogleNLModerator
    calls = {"n": 0}

    def boom(_text):
        calls["n"] += 1
        raise RuntimeError("down")

    mod = GoogleNLModerator(threshold=0.8, fetch_scores=boom)
    assert not mod.moderate("clean hello").blocked
    assert not mod.moderate("another clean one").blocked
    assert calls["n"] == 1  # one probe, then wordlist-only


class _BlockingModerator:
    def moderate(self, _text):
        from app.text_moderation import ModerationVerdict
        return ModerationVerdict(blocked=True, source="nl",
                                 scores={"Toxic": 0.99})
