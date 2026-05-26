"""Token validation + lifecycle for design_mcp_tokens.

Tokens are opaque 32-byte random hex strings (64 hex chars). The DB stores
the SHA-256 hash, never the raw value — so even DB read access can't recover
issued tokens. Re-issue if a user loses theirs.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .db import get_conn

log = logging.getLogger(__name__)


class AuthError(Exception):
    """Token invalid, revoked, or unknown."""


@dataclass
class TokenInfo:
    id: int
    user_email: str
    note: Optional[str]
    created_at: datetime
    last_used_at: Optional[datetime]
    usage_count: int
    revoked_at: Optional[datetime]


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def hash_token(raw_token: str) -> str:
    """Public alias of the SHA-256 hashing used for every opaque secret
    stored in this server (invite tokens, OAuth client secrets, OAuth
    authorization codes, OAuth access/refresh tokens)."""
    return _hash(raw_token)


def new_opaque_token() -> str:
    """Mint a 32-byte (64-hex-char) cryptographically random opaque token.
    Used for invite tokens, OAuth client secrets, authorization codes,
    access tokens and refresh tokens — same shape everywhere."""
    return secrets.token_hex(32)


def issue_token(user_email: str, note: Optional[str] = None) -> tuple[str, TokenInfo]:
    """Create a new token. Returns (raw_token, TokenInfo).

    The raw token is shown ONCE — the caller (CLI) must display it to the user
    and tell them it's not retrievable.
    """
    raw = secrets.token_hex(32)  # 64 hex chars
    h = _hash(raw)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO design_mcp_tokens (token_hash, user_email, note)
            VALUES (%s, %s, %s)
            RETURNING id, user_email, note, created_at, last_used_at, usage_count, revoked_at
            """,
            (h, user_email, note),
        )
        row = cur.fetchone()
    return raw, TokenInfo(**row)


def validate_token(raw_token: str) -> TokenInfo:
    """Look up a token. Raise AuthError if missing or revoked. Update usage stats.

    Returns TokenInfo for callers that want to know who they're talking to.
    """
    if not raw_token or len(raw_token) != 64:
        raise AuthError("token must be 64 hex chars")
    h = _hash(raw_token)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, user_email, note, created_at, last_used_at, usage_count, revoked_at
              FROM design_mcp_tokens
             WHERE token_hash = %s
            """,
            (h,),
        )
        row = cur.fetchone()
        if not row:
            raise AuthError("unknown token")
        if row["revoked_at"] is not None:
            raise AuthError(f"token revoked at {row['revoked_at']}")
        # Update usage stats
        cur.execute(
            """
            UPDATE design_mcp_tokens
               SET last_used_at = NOW(),
                   usage_count  = usage_count + 1
             WHERE id = %s
            """,
            (row["id"],),
        )
    return TokenInfo(**row)


def revoke_token(raw_token: str) -> bool:
    """Mark a token revoked. Returns True if it existed and was newly revoked,
    False if unknown or already revoked.
    """
    if not raw_token or len(raw_token) != 64:
        raise AuthError("token must be 64 hex chars")
    h = _hash(raw_token)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE design_mcp_tokens
               SET revoked_at = NOW()
             WHERE token_hash = %s AND revoked_at IS NULL
            """,
            (h,),
        )
        return cur.rowcount > 0


def list_tokens(include_revoked: bool = False) -> list[TokenInfo]:
    where = "" if include_revoked else " WHERE revoked_at IS NULL"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, user_email, note, created_at, last_used_at, usage_count, revoked_at
              FROM design_mcp_tokens
              {where}
             ORDER BY id DESC
            """
        )
        return [TokenInfo(**row) for row in cur.fetchall()]
