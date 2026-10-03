"""Pre-publish text moderation for chat messages.

A message is scored on its plaintext in ``POST /v1/threads/{id}/messages``
BEFORE ``MessageRepo.add_message`` — a blocked message is never inserted,
never encrypted, never cached, and never fanned out to push. The client gets
``422 {"code": "message_inappropriate"}`` and shows a popup; the text stays
in the sender's input box so they can rewrite it.

Scorer: Google Cloud Natural Language ``documents:moderateText`` — the same
cloud the app already runs on, service-account auth (ADC), no API key, no
new heavyweight dependency (REST over urllib + google-auth, lazily imported
like ``google-cloud-storage`` in storage.py). A message is blocked when any
of ``BLOCK_CATEGORIES`` scores >= ``MODERATION_THRESHOLD`` (default 0.8;
NL scores are contextual, so garden talk like "kill the aphids" or "grab
your hoe" stays well under the bar while direct abuse trips it).

Outage posture: if the NL call fails (API down, disabled in the project, no
credentials), the gate degrades to a small local wordlist instead of taking
chat down with it. The wordlist is an outage fallback only — deliberately
narrow (word-boundary tokens, no substrings, so "hoe"/"weeds"/"Scunthorpe"
class false positives can't fire), not the primary scorer.

Privacy: category scores are logged for threshold tuning; message bodies
are NEVER logged, stored, or attached to the verdict.

Disable with ``MODERATION_ENABLED=0`` (tests/local debugging); that swaps in
a moderator that blocks nothing.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

_NL_URL = "https://language.googleapis.com/v2/documents:moderateText"
_NL_SCOPE = "https://www.googleapis.com/auth/cloud-language"
_NL_TIMEOUT_SECONDS = 3.0

# Categories that block a message when their confidence meets the threshold.
# "Violent" is included for direct threats; NL is contextual, so pest/tool
# talk ("kill the aphids", "weed killer") does not score as violent content.
BLOCK_CATEGORIES = ("Toxic", "Insult", "Profanity", "Derogatory", "Sexual",
                    "Violent")

# Outage fallback only (see module docstring). Word-boundary token matching
# after lowercasing; kept deliberately narrow — the NL API is the scorer.
_FALLBACK_WORDS = frozenset({
    "fuck", "fucking", "fucked", "fucker", "motherfucker", "shit", "shitty",
    "bitch", "bitches", "asshole", "assholes", "dickhead", "cunt", "whore",
    "slut", "pussy", "nigger", "nigga", "faggot", "fag", "spic", "kike",
    "chink", "wetback", "retard", "retarded", "cocksucker", "dick", "cock",
    "bastard", "porn", "rape", "rapist", "pedophile", "nazi",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class ModerationVerdict:
    blocked: bool
    source: str  # "nl" | "wordlist" | "disabled"
    scores: dict[str, float] = field(default_factory=dict)


class TextModerator(Protocol):
    def moderate(self, text: str) -> ModerationVerdict: ...


class NullModerator:
    """MODERATION_ENABLED=0 — blocks nothing."""

    def moderate(self, text: str) -> ModerationVerdict:
        return ModerationVerdict(blocked=False, source="disabled")


def _wordlist_hit(text: str) -> bool:
    return any(tok in _FALLBACK_WORDS for tok in _TOKEN_RE.findall(text.lower()))


class WordlistModerator:
    """Local fallback: the gate's behavior whenever the NL API is unreachable."""

    def moderate(self, text: str) -> ModerationVerdict:
        return ModerationVerdict(blocked=_wordlist_hit(text), source="wordlist")


def _fetch_nl_scores(text: str, timeout: float = _NL_TIMEOUT_SECONDS) -> dict[str, float]:
    """One moderateText call; returns {category: confidence}. Raises on any
    failure (no credentials, API disabled/down, timeout) — the caller falls
    back to the wordlist. Lazily imports google-auth so dev/CI without
    credentials installed keep working."""
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(scopes=[_NL_SCOPE])
    credentials.refresh(google.auth.transport.requests.Request())
    payload = json.dumps(
        {"document": {"type": "PLAIN_TEXT", "content": text}}).encode("utf-8")
    request = urllib.request.Request(
        _NL_URL, data=payload, method="POST",
        headers={"Authorization": f"Bearer {credentials.token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return {str(cat["name"]): float(cat["confidence"])
            for cat in data.get("moderationCategories", [])}


class GoogleNLModerator:
    """NL moderateText with wordlist degradation. The NL transport is
    injectable for tests; an unreachable NL latches off for the life of the
    instance so a dead API costs one probe, not one timeout per message."""

    def __init__(self, threshold: float,
                 fetch_scores=_fetch_nl_scores) -> None:
        self._threshold = threshold
        self._fetch_scores = fetch_scores
        self._nl_available = True
        self._wordlist = WordlistModerator()

    def moderate(self, text: str) -> ModerationVerdict:
        if self._nl_available:
            try:
                scores = self._fetch_scores(text)
            except Exception as exc:  # noqa: BLE001 — any failure degrades
                self._nl_available = False
                logger.warning(
                    "text_moderation: NL unavailable (%s); wordlist fallback",
                    type(exc).__name__)
            else:
                blocked = any(scores.get(cat, 0.0) >= self._threshold
                              for cat in BLOCK_CATEGORIES)
                return ModerationVerdict(blocked=blocked, source="nl",
                                         scores=scores)
        return self._wordlist.moderate(text)


_moderator_cache: tuple[tuple[bool, float], TextModerator] | None = None


def get_text_moderator() -> TextModerator:
    """FastAPI dependency. Cached per (enabled, threshold) so the NL
    availability latch survives across requests; tests override this
    dependency (or monkeypatch env + clear the cache) instead of calling
    the real API."""
    global _moderator_cache
    settings: Settings = get_settings()
    key = (settings.moderation_enabled, settings.moderation_threshold)
    if _moderator_cache is None or _moderator_cache[0] != key:
        moderator: TextModerator = (
            GoogleNLModerator(settings.moderation_threshold)
            if settings.moderation_enabled else NullModerator())
        _moderator_cache = (key, moderator)
    return _moderator_cache[1]
