"""Signed-URL preview links for in-progress design drafts.

Generates and verifies HMAC-signed preview URLs so a user can open the
generated HTML in any browser (including mobile) before approving
``submit_design``. URLs look like::

    https://design-mcp.leadloom.com.au/preview/<design_id>?exp=<unix_ts>&sig=<hex_hmac>

The HMAC reuses the same signing key the OAuth flow uses for ``oauth_state``
(``TOKEN_DB_PASSWORD`` / ``OAUTH_STATE_SIGNING_KEY``) so we don't introduce
a new ops-managed secret. The signed message binds three values together::

    <design_id>|<user_email>|<exp>

so the link can't be re-used for another draft, another user, or beyond
its expiry. The route that consumes these URLs only needs ``design_id``,
``exp`` and ``sig`` — it recovers ``user_email`` from the draft row and
re-signs to verify, which means a tampered query string fails closed.
"""

from __future__ import annotations

import hmac
import os
import time
from hashlib import sha256
from urllib.parse import urlencode

from .oauth_provider import _state_signing_key

DEFAULT_TTL_SECONDS = 60 * 60  # 1 hour


def _public_url() -> str:
    return os.getenv("PUBLIC_URL", "https://design-mcp.leadloom.com.au").rstrip("/")


def _sig_for(design_id: str, user_email: str, exp: int) -> str:
    msg = f"{design_id}|{user_email}|{exp}".encode("utf-8")
    return hmac.new(_state_signing_key(), msg, sha256).hexdigest()


def signed_preview_url(
    design_id: str,
    user_email: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Return a signed preview URL that expires after ``ttl_seconds``."""
    if not design_id:
        raise ValueError("design_id is required")
    if not user_email:
        raise ValueError("user_email is required")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    exp = int(time.time()) + int(ttl_seconds)
    sig = _sig_for(design_id, user_email, exp)
    qs = urlencode({"exp": exp, "sig": sig})
    return f"{_public_url()}/preview/{design_id}?{qs}"


def verify_preview_signature(
    design_id: str,
    exp: int,
    sig: str,
    user_email: str,
) -> bool:
    """Constant-time verify a preview URL's signature + freshness.

    Returns True iff ``sig`` matches the HMAC over (design_id, user_email,
    exp) AND ``exp`` is in the future. The caller supplies ``user_email``
    because it's the authority — it comes from the draft row, not the URL,
    so a tampered URL can't pivot to a different account.
    """
    if not design_id or not sig or not user_email:
        return False
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_int < int(time.time()):
        return False
    expected = _sig_for(design_id, user_email, exp_int)
    return hmac.compare_digest(expected, str(sig))
