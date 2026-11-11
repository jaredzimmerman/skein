"""Member tokens: random bearer secrets, stored only as SHA-256 hashes."""

from __future__ import annotations

import hashlib
import hmac
import secrets

PREFIX = "skein_"


def new_token() -> str:
    """A fresh bearer token. Shown to the owner once; only its hash is kept."""
    return PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Hex SHA-256 of a token, as stored in ``members.token_sha256``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hashes_match(a: str, b: str) -> bool:
    """Constant-time comparison of two token hashes."""
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))
