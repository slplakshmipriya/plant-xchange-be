"""At-rest field encryption (Fernet: AES-128-CBC + HMAC-SHA256).

Values are encrypted in Python before they reach the store (Postgres or the
in-memory fakes) and decrypted at the last responsible moment before use —
e.g. right before ``fuzz_location`` runs, or inside the serializer that
serves a participant. Keys come from environment variables. Cipher objects
are cached per distinct key *value*, so a ``monkeypatch.setenv`` in tests
(or a real rotation) takes effect immediately while hot paths avoid
rebuilding Fernet on every call.

Key versioning (H4):
- Every token emitted by ``encrypt_text`` carries a version prefix,
  ``v{n}:<fernet-token>`` (``n`` = ``KEY_VERSION``). Tokens minted before
  versioning (no prefix) are treated as ``v1`` and still decrypt.
- Decryption is dual-key: the prefix selects the key. The current version's
  key comes from the plain env var (``MESSAGE_ENCRYPTION_KEY``); older
  versions ``k`` come from ``<VAR>_V{k}`` (e.g. ``MESSAGE_ENCRYPTION_KEY_V1``
  after rotating to v2). A version with no configured key fails closed.
- Rotation procedure (keep old keys as ``_V{n}`` during the transition,
  re-encrypt-on-write, then drop the old secret) is documented in
  ``api/docs/secrets.md``. Key loss = permanent data loss for every field
  encrypted under that key (see that doc's backup procedure).

Fail closed everywhere:
- missing or invalid key -> ``RuntimeError`` (refuse to handle secrets)
- tampered token / wrong key / unknown version -> ``RuntimeError``
  (Fernet authentication / fail-closed envelope)

Key registry:
- ``MESSAGE_ENCRYPTION_KEY``: chat message bodies (msg.py)
- ``GEO_ENCRYPTION_KEY``: listing true coordinates (listings.py)

Mint keys with ``generate_key()`` (documented in ``api/docs/secrets.md``) —
never hand-roll a key.

This is application-layer encryption, not end-to-end: the server holds the
keys, so privileged flows (the support moderation dashboard) can still
decrypt. What it defeats is DB-layer exposure — dumps, backup leaks, a
read-only SQL injection, or someone browsing tables in the DB console.
A fully compromised app server defeats it by design.
"""

from __future__ import annotations

import os
import re
import threading

from cryptography.fernet import Fernet, InvalidToken

MESSAGE_KEY_ENV = "MESSAGE_ENCRYPTION_KEY"
GEO_KEY_ENV = "GEO_ENCRYPTION_KEY"

# Current token version. Bump when rotating keys: new writes carry the new
# prefix, reads keep working via the ``<VAR>_V{n}`` dual-key env vars.
KEY_VERSION = 1

_VERSION_RE = re.compile(r"^v(\d+):")

# Cache of built ciphers: (env_name, version) -> (raw_key_value, Fernet).
# Keyed on the *value* so env changes (tests, rotations) rebuild; a missing
# or invalid key is never cached and always raises (fail closed).
_ciphers: dict[tuple[str, int], tuple[str, Fernet]] = {}
_cipher_lock = threading.Lock()


def generate_key() -> str:
    """Mint a fresh Fernet key for ops. Store in env / Secret Manager, never in the repo.

    Usage: ``python -c "from app.crypto import generate_key; print(generate_key())"``
    """
    return Fernet.generate_key().decode("utf-8")


def _env_name_for(env_var: str, version: int) -> str:
    """Env var holding the key for ``version``: the plain var for the current
    version, ``<VAR>_V{n}`` for older versions (dual-key decrypt)."""
    if version == KEY_VERSION:
        return env_var
    if version < KEY_VERSION:
        return f"{env_var}_V{version}"
    raise RuntimeError(
        f"ciphertext is version v{version} but this build only knows v{KEY_VERSION} "
        "— refusing to guess at a key"
    )


def get_cipher(env_var: str, version: int | None = None) -> Fernet:
    """Build (or reuse the cached) cipher for ``env_var`` at ``version``.

    ``version=None`` means the current ``KEY_VERSION`` (resolved at call
    time, not definition time, so a rotation bump takes effect). Missing /
    invalid key raises ``RuntimeError`` (fail closed). The cache is keyed on
    the key *value*, so rotating the env var or monkeypatching it in tests
    takes effect on the next call.
    """
    if version is None:
        version = KEY_VERSION
    name = _env_name_for(env_var, version)
    raw = os.environ.get(name, "")
    if not raw:
        raise RuntimeError(
            f"{name} is not set — refusing to handle encrypted fields without a key"
        )
    with _cipher_lock:
        cached = _ciphers.get((name, version))
        if cached is not None and cached[0] == raw:
            return cached[1]
    try:
        cipher = Fernet(raw.encode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"{name} is not a valid Fernet key") from exc
    with _cipher_lock:
        _ciphers[(name, version)] = (raw, cipher)
    return cipher


def _split_version(token: str) -> tuple[int, str]:
    """``v{n}:<token>`` -> (n, token); unprefixed (legacy) tokens -> (1, token)."""
    m = _VERSION_RE.match(token)
    if m:
        return int(m.group(1)), token[m.end():]
    return 1, token


def encrypt_text(plaintext: str, env_var: str) -> str:
    """Encrypt ``plaintext`` -> ``v{n}:<base64 token>``. Raises on missing key."""
    token = get_cipher(env_var).encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return f"v{KEY_VERSION}:{token}"


def decrypt_text(token: str, env_var: str) -> str:
    """Decrypt a token (versioned or legacy unprefixed). Raises
    ``RuntimeError`` on tampered token, wrong key, or unknown key version."""
    version, bare = _split_version(token)
    try:
        return get_cipher(env_var, version).decrypt(bare.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "ciphertext failed authentication — wrong key or tampered data"
        ) from exc


def encrypt_float(value: float | None, env_var: str) -> str | None:
    """Encrypt an optional coordinate. ``None`` stays ``None`` (no geo)."""
    if value is None:
        return None
    return encrypt_text(repr(float(value)), env_var)


def decrypt_float(token: str | None, env_var: str) -> float | None:
    """Decrypt an optional coordinate. ``None`` stays ``None``; a non-string
    or unauthentic token raises (fail closed). A decryptable token whose
    plaintext is not a float raises the uniform ``RuntimeError`` envelope
    (M24) instead of a bare ``ValueError``."""
    if token is None:
        return None
    if not isinstance(token, str):
        raise RuntimeError("expected ciphertext string for encrypted coordinate")
    try:
        return float(decrypt_text(token, env_var))
    except ValueError as exc:
        raise RuntimeError(
            "encrypted coordinate decrypted but is not a valid float — data corrupted"
        ) from exc
