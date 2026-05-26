"""OAuth 2.1 Authorization Server provider for the design-mcp-server.

Implements `mcp.server.auth.provider.OAuthAuthorizationServerProvider` against
PostgreSQL on DO PG 17 (the same `acquirely_rel` DB that holds invite
tokens). The flow that hangs together:

  1. claude.ai POSTs /register (DCR, RFC 7591)
        -> register_client() stores a row in design_mcp_oauth_clients
        -> returns OAuthClientInformationFull (raw secret shown once)

  2. claude.ai redirects browser to /authorize?...
        -> SDK's AuthorizationHandler calls authorize() which returns the
           URL of OUR HTML login form (custom_route /authorize/login)
           with the OAuth params stuffed into a signed `oauth_state` query
           param so the form can round-trip them without trusting the user.

  3. Browser submits the form with the user's invite token.
        -> Our /authorize/login handler validates the invite token against
           design_mcp_tokens, mints an authorization code, persists it in
           design_mcp_oauth_codes, then 302s back to client.redirect_uri
           with ?code=...&state=...

  4. claude.ai POSTs /token (grant_type=authorization_code)
        -> SDK's TokenHandler authenticates the client (client_secret_post),
           checks PKCE code_verifier vs stored code_challenge (S256),
           calls exchange_authorization_code() which mints + persists
           access + refresh tokens and marks the auth code consumed.

  5. claude.ai uses the access token as Bearer for /mcp.
        -> load_access_token() looks up in design_mcp_oauth_access_tokens;
           falls back to design_mcp_tokens so the existing invite-token
           direct-curl workflow keeps working.

Opaque tokens everywhere — 32-byte hex (64 chars), only SHA-256 hashes
stored. Helpers live in design_mcp.auth (SSOT).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from . import auth as auth_mod
from .auth import AuthError, hash_token, new_opaque_token
from .db import get_conn

log = logging.getLogger(__name__)

ACCESS_TOKEN_TTL_SECONDS = 60 * 60          # 1 hour
REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
AUTH_CODE_TTL_SECONDS = 60 * 10              # 10 minutes


# ---------------------------------------------------------------------------
# Sync DB helpers — run inside asyncio.to_thread so the event loop stays free
# ---------------------------------------------------------------------------

def _redirect_uri_allowed(uri: str) -> bool:
    """OAuth 2.1 redirect-uri policy: https only, plus http://localhost(:port).

    Loopback is allowed for local dev clients (mcp-inspector etc.); everything
    else MUST be https.
    """
    if uri.startswith("https://"):
        return True
    if uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1"):
        return True
    return False


def _store_client(client: OAuthClientInformationFull) -> None:
    redirect_uris = [str(u) for u in (client.redirect_uris or [])]
    secret_hash = hash_token(client.client_secret) if client.client_secret else ""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO design_mcp_oauth_clients
                (client_id, client_secret, client_secret_hash, client_name,
                 redirect_uris, grant_types, response_types,
                 token_endpoint_auth_method, scope)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                client.client_id,
                client.client_secret or "",
                secret_hash,
                client.client_name,
                redirect_uris,
                list(client.grant_types or []),
                list(client.response_types or []),
                client.token_endpoint_auth_method or "client_secret_post",
                client.scope,
            ),
        )


def _load_client(client_id: str) -> OAuthClientInformationFull | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT client_id, client_secret, client_name, redirect_uris,
                   grant_types, response_types, token_endpoint_auth_method,
                   scope, created_at
              FROM design_mcp_oauth_clients
             WHERE client_id = %s
            """,
            (client_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return OAuthClientInformationFull(
        client_id=row["client_id"],
        client_secret=row["client_secret"] or None,
        client_id_issued_at=int(row["created_at"].timestamp()) if row["created_at"] else None,
        client_secret_expires_at=None,
        redirect_uris=[AnyUrl(u) for u in row["redirect_uris"]],
        grant_types=row["grant_types"],
        response_types=row["response_types"],
        token_endpoint_auth_method=row["token_endpoint_auth_method"],
        scope=row["scope"],
        client_name=row["client_name"],
    )


def _store_auth_code(
    *,
    raw_code: str,
    client_id: str,
    user_email: str,
    redirect_uri: str,
    redirect_uri_explicit: bool,
    code_challenge: str,
    code_challenge_method: str,
    scopes: list[str],
    ttl_seconds: int = AUTH_CODE_TTL_SECONDS,
) -> datetime:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO design_mcp_oauth_codes
                (code_hash, client_id, user_email, redirect_uri,
                 redirect_uri_explicit, code_challenge, code_challenge_method,
                 scopes, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                hash_token(raw_code),
                client_id,
                user_email,
                redirect_uri,
                redirect_uri_explicit,
                code_challenge,
                code_challenge_method,
                scopes,
                expires_at,
            ),
        )
    return expires_at


def _load_auth_code(raw_code: str) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT code_hash, client_id, user_email, redirect_uri,
                   redirect_uri_explicit, code_challenge, code_challenge_method,
                   scopes, expires_at, consumed_at
              FROM design_mcp_oauth_codes
             WHERE code_hash = %s
            """,
            (hash_token(raw_code),),
        )
        return cur.fetchone()


def _consume_auth_code(code_hash: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE design_mcp_oauth_codes SET consumed_at = NOW() WHERE code_hash = %s",
            (code_hash,),
        )


def _store_access_token(
    *, raw_token: str, client_id: str, user_email: str, scopes: list[str],
    ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
) -> datetime:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO design_mcp_oauth_access_tokens
                (token_hash, client_id, user_email, scopes, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (hash_token(raw_token), client_id, user_email, scopes, expires_at),
        )
    return expires_at


def _load_access_token_row(raw_token: str) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT token_hash, client_id, user_email, scopes, expires_at, revoked_at
              FROM design_mcp_oauth_access_tokens
             WHERE token_hash = %s
            """,
            (hash_token(raw_token),),
        )
        return cur.fetchone()


def _store_refresh_token(
    *, raw_token: str, client_id: str, user_email: str, scopes: list[str],
    ttl_seconds: int = REFRESH_TOKEN_TTL_SECONDS,
) -> datetime:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO design_mcp_oauth_refresh_tokens
                (token_hash, client_id, user_email, scopes, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (hash_token(raw_token), client_id, user_email, scopes, expires_at),
        )
    return expires_at


def _load_refresh_token_row(raw_token: str) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT token_hash, client_id, user_email, scopes, expires_at, revoked_at
              FROM design_mcp_oauth_refresh_tokens
             WHERE token_hash = %s
            """,
            (hash_token(raw_token),),
        )
        return cur.fetchone()


def _revoke_oauth_token(token_hash: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE design_mcp_oauth_access_tokens SET revoked_at = NOW() WHERE token_hash = %s AND revoked_at IS NULL",
            (token_hash,),
        )
        cur.execute(
            "UPDATE design_mcp_oauth_refresh_tokens SET revoked_at = NOW() WHERE token_hash = %s AND revoked_at IS NULL",
            (token_hash,),
        )


# ---------------------------------------------------------------------------
# State signing — wraps the OAuth /authorize params into a single opaque
# `oauth_state` blob that we hand to our HTML form and verify on POST so the
# user can't tamper with client_id / redirect_uri / code_challenge / scopes.
# ---------------------------------------------------------------------------

import base64
import hmac
import json
import os
import hashlib as _hashlib


def _state_signing_key() -> bytes:
    # Reuse the token-DB password as HMAC key — it's already a server-side
    # secret managed by ops. Anything in env that's stable across PM2
    # restarts works; this avoids adding a new env var.
    key = os.environ.get("OAUTH_STATE_SIGNING_KEY") or os.environ.get("TOKEN_DB_PASSWORD")
    if not key:
        raise RuntimeError("OAUTH_STATE_SIGNING_KEY or TOKEN_DB_PASSWORD must be set")
    return key.encode("utf-8")


def sign_oauth_state(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    sig = hmac.new(_state_signing_key(), body, _hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
    return f"{body.decode()}.{sig_b64.decode()}"


def verify_oauth_state(blob: str) -> dict:
    try:
        body_b64, sig_b64 = blob.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed oauth_state") from exc
    body = body_b64.encode()
    sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    expected = hmac.new(_state_signing_key(), body, _hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("oauth_state signature mismatch")
    return json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))


# ---------------------------------------------------------------------------
# The provider itself
# ---------------------------------------------------------------------------

class OAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """OAuth 2.1 AS backed by DO PG 17 + the existing invite-token table."""

    def __init__(self, public_url: str, default_scopes: list[str]):
        self.public_url = public_url.rstrip("/")
        self.default_scopes = list(default_scopes)

    # --- DCR ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await asyncio.to_thread(_load_client, client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # SDK already minted client_id + client_secret; we just persist.
        for u in (client_info.redirect_uris or []):
            if not _redirect_uri_allowed(str(u)):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=(
                        f"redirect_uri {str(u)!r} must use https or http://localhost"
                    ),
                )
        if not client_info.scope:
            client_info.scope = " ".join(self.default_scopes)
        await asyncio.to_thread(_store_client, client_info)
        log.info(
            "oauth client registered id=%s name=%s redirect_uris=%s",
            client_info.client_id, client_info.client_name, client_info.redirect_uris,
        )

    # --- /authorize → our login form --------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        state_payload = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_explicit": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "code_challenge_method": "S256",
            "scopes": params.scopes or self.default_scopes,
            "state": params.state,
            "resource": params.resource,
        }
        blob = sign_oauth_state(state_payload)
        from urllib.parse import urlencode
        return f"{self.public_url}/authorize/login?{urlencode({'oauth_state': blob})}"

    # --- Authorization code lifecycle -------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = await asyncio.to_thread(_load_auth_code, authorization_code)
        if row is None:
            return None
        if row["consumed_at"] is not None:
            log.warning("oauth: auth code already consumed client_id=%s", row["client_id"])
            return None
        if row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"],
            expires_at=row["expires_at"].timestamp(),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=row["redirect_uri_explicit"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        row = await asyncio.to_thread(_load_auth_code, authorization_code.code)
        if row is None or row["consumed_at"] is not None:
            from mcp.server.auth.provider import TokenError
            raise TokenError(error="invalid_grant", error_description="code already used")
        user_email = row["user_email"]
        scopes = list(authorization_code.scopes)

        access_raw = new_opaque_token()
        refresh_raw = new_opaque_token()

        await asyncio.to_thread(
            _store_access_token,
            raw_token=access_raw, client_id=client.client_id,
            user_email=user_email, scopes=scopes,
        )
        await asyncio.to_thread(
            _store_refresh_token,
            raw_token=refresh_raw, client_id=client.client_id,
            user_email=user_email, scopes=scopes,
        )
        await asyncio.to_thread(_consume_auth_code, row["code_hash"])

        log.info("oauth token issued client_id=%s user=%s", client.client_id, user_email)
        return OAuthToken(
            access_token=access_raw,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_raw,
        )

    # --- Refresh ----------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = await asyncio.to_thread(_load_refresh_token_row, refresh_token)
        if row is None or row["revoked_at"] is not None:
            return None
        if row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"].timestamp()),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate both: revoke old refresh, issue new pair.
        await asyncio.to_thread(_revoke_oauth_token, hash_token(refresh_token.token))
        row = await asyncio.to_thread(_load_refresh_token_row, refresh_token.token)
        user_email = row["user_email"] if row else "unknown"

        access_raw = new_opaque_token()
        refresh_raw = new_opaque_token()
        await asyncio.to_thread(
            _store_access_token,
            raw_token=access_raw, client_id=client.client_id,
            user_email=user_email, scopes=scopes,
        )
        await asyncio.to_thread(
            _store_refresh_token,
            raw_token=refresh_raw, client_id=client.client_id,
            user_email=user_email, scopes=scopes,
        )
        return OAuthToken(
            access_token=access_raw,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_raw,
        )

    # --- Access token verification ----------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Accept BOTH OAuth-issued access tokens AND original invite tokens.

        The SDK uses this method (via ProviderTokenVerifier) for every
        authenticated MCP call, so we keep the legacy curl-with-invite-token
        workflow working by falling back to design_mcp_tokens.
        """
        # 1. OAuth-issued access token
        row = await asyncio.to_thread(_load_access_token_row, token)
        if row is not None:
            if row["revoked_at"] is not None:
                return None
            if row["expires_at"] < datetime.now(timezone.utc):
                return None
            return AccessToken(
                token=token,
                client_id=row["client_id"],
                scopes=row["scopes"],
                expires_at=int(row["expires_at"].timestamp()),
            )

        # 2. Fallback: original invite token in design_mcp_tokens.
        try:
            info = await asyncio.to_thread(auth_mod.validate_token, token)
        except AuthError as exc:
            log.warning("auth fail: %s", exc)
            return None
        except Exception:
            log.exception("auth fail: unexpected error validating invite token")
            return None
        return AccessToken(
            token=token,
            client_id=f"design-mcp-token:{info.id}",
            scopes=["design:write"],
            expires_at=None,
        )

    # --- Revocation -------------------------------------------------------

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await asyncio.to_thread(_revoke_oauth_token, hash_token(token.token))
        log.info("oauth token revoked client_id=%s", token.client_id)


# ---------------------------------------------------------------------------
# /authorize/login HTML form — minimal, no JS, no external assets.
# Mounted by server.py via mcp.custom_route().
# ---------------------------------------------------------------------------

_LOGIN_FORM_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Acquirely Design MCP — Authorize</title>
<style>
 body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: #f4f5f7; margin: 0; padding: 0; color: #1d2026; }}
 .card {{ max-width: 460px; margin: 80px auto; background: #fff; border-radius: 10px;
         padding: 36px 36px 28px; box-shadow: 0 4px 24px rgba(0,0,0,.06); }}
 h1 {{ margin: 0 0 6px; font-size: 22px; }}
 .subtle {{ color: #5f6b7a; font-size: 14px; margin: 0 0 24px; }}
 .client-name {{ font-weight: 600; color: #1d2026; }}
 label {{ display: block; font-size: 13px; font-weight: 600; margin: 18px 0 6px; }}
 input[type=password] {{ width: 100%; box-sizing: border-box; padding: 10px 12px;
        font-family: monospace; font-size: 13px; border: 1px solid #d3d6dd;
        border-radius: 6px; }}
 button {{ margin-top: 22px; width: 100%; padding: 11px 14px; background: #1f6feb;
          color: #fff; border: 0; border-radius: 6px; font-size: 15px; cursor: pointer; }}
 button:hover {{ background: #1858bf; }}
 .err {{ background: #fdecea; color: #842029; border: 1px solid #f5c2c7;
        border-radius: 6px; padding: 10px 12px; font-size: 13px; margin-top: 16px; }}
 .scopes {{ background: #f7f9fc; border: 1px solid #e3e7ee; border-radius: 6px;
            padding: 10px 12px; font-size: 13px; color: #3a4453; }}
 footer {{ text-align: center; font-size: 12px; color: #8a96a7; margin-top: 22px; }}
</style>
</head>
<body>
<div class="card">
 <h1>Acquirely Design MCP</h1>
 <p class="subtle">
   <span class="client-name">{client_name}</span> is requesting access to your
   Acquirely Design MCP account.
 </p>
 <div class="scopes">Scopes requested: <code>{scopes}</code></div>
 {error_block}
 <form method="POST" action="/authorize/login">
   <input type="hidden" name="oauth_state" value="{oauth_state}">
   <label for="invite_token">Paste your invite token</label>
   <input type="password" id="invite_token" name="invite_token" autocomplete="off"
          spellcheck="false" autofocus required>
   <button type="submit">Authorize</button>
 </form>
 <footer>design-mcp.leadloom.com.au</footer>
</div>
</body>
</html>
"""


def render_login_form(*, client_name: str, scopes: list[str], oauth_state: str,
                      error: str | None = None) -> str:
    import html as _html
    error_block = (
        f'<div class="err">{_html.escape(error)}</div>' if error else ""
    )
    return _LOGIN_FORM_HTML.format(
        client_name=_html.escape(client_name or "an OAuth client"),
        scopes=_html.escape(" ".join(scopes)),
        oauth_state=_html.escape(oauth_state),
        error_block=error_block,
    )
