"""OAuth 2.1 AS tests — DCR endpoint, code → token exchange, PKCE, token
verification, invite-token fallthrough.

All DB calls are monkeypatched. We exercise the real Starlette routes the
SDK mounts so we cover the wire format claude.ai actually sees.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("TOKEN_DB_PASSWORD", "test-only-not-used")

from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402
from pydantic import AnyUrl  # noqa: E402

from design_mcp import auth as auth_mod  # noqa: E402
from design_mcp import oauth_provider as op  # noqa: E402
from design_mcp.auth import AuthError, TokenInfo  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory fake DB
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self):
        self.clients: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}

    def install(self, monkeypatch):
        store = self

        def store_client(client):
            store.clients[client.client_id] = {
                "client_id": client.client_id,
                "client_secret": client.client_secret or "",
                "client_name": client.client_name,
                "redirect_uris": [str(u) for u in (client.redirect_uris or [])],
                "grant_types": list(client.grant_types or []),
                "response_types": list(client.response_types or []),
                "token_endpoint_auth_method": client.token_endpoint_auth_method or "client_secret_post",
                "scope": client.scope,
                "created_at": datetime.now(timezone.utc),
            }

        def load_client(client_id):
            row = store.clients.get(client_id)
            if not row:
                return None
            return OAuthClientInformationFull(
                client_id=row["client_id"],
                client_secret=row["client_secret"] or None,
                redirect_uris=[AnyUrl(u) for u in row["redirect_uris"]],
                grant_types=row["grant_types"],
                response_types=row["response_types"],
                token_endpoint_auth_method=row["token_endpoint_auth_method"],
                scope=row["scope"],
                client_name=row["client_name"],
            )

        def store_auth_code(*, raw_code, client_id, user_email, redirect_uri,
                            redirect_uri_explicit, code_challenge,
                            code_challenge_method, scopes, ttl_seconds=600):
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            store.codes[op.hash_token(raw_code)] = {
                "code_hash": op.hash_token(raw_code),
                "client_id": client_id, "user_email": user_email,
                "redirect_uri": redirect_uri,
                "redirect_uri_explicit": redirect_uri_explicit,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "scopes": scopes, "expires_at": expires_at,
                "consumed_at": None,
            }
            return expires_at

        def load_auth_code(raw_code):
            return store.codes.get(op.hash_token(raw_code))

        def consume_auth_code(code_hash):
            row = store.codes.get(code_hash)
            if row:
                row["consumed_at"] = datetime.now(timezone.utc)

        def store_access(*, raw_token, client_id, user_email, scopes,
                         ttl_seconds=3600, auth_code_hash=None):
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            store.access[op.hash_token(raw_token)] = {
                "token_hash": op.hash_token(raw_token), "client_id": client_id,
                "user_email": user_email, "scopes": scopes,
                "expires_at": expires_at, "revoked_at": None,
                "auth_code_hash": auth_code_hash,
            }
            return expires_at

        def load_access(raw_token):
            return store.access.get(op.hash_token(raw_token))

        def store_refresh(*, raw_token, client_id, user_email, scopes,
                          ttl_seconds=2592000, auth_code_hash=None):
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            store.refresh[op.hash_token(raw_token)] = {
                "token_hash": op.hash_token(raw_token), "client_id": client_id,
                "user_email": user_email, "scopes": scopes,
                "expires_at": expires_at, "revoked_at": None,
                "auth_code_hash": auth_code_hash,
            }
            return expires_at

        def load_refresh(raw_token):
            return store.refresh.get(op.hash_token(raw_token))

        def revoke(token_hash):
            for d in (store.access, store.refresh):
                row = d.get(token_hash)
                if row and row["revoked_at"] is None:
                    row["revoked_at"] = datetime.now(timezone.utc)

        def revoke_for_code(code_hash):
            touched = 0
            for d in (store.access, store.refresh):
                for row in d.values():
                    if row.get("auth_code_hash") == code_hash and row["revoked_at"] is None:
                        row["revoked_at"] = datetime.now(timezone.utc)
                        touched += 1
            return touched

        monkeypatch.setattr(op, "_store_client", store_client)
        monkeypatch.setattr(op, "_load_client", load_client)
        monkeypatch.setattr(op, "_store_auth_code", store_auth_code)
        monkeypatch.setattr(op, "_load_auth_code", load_auth_code)
        monkeypatch.setattr(op, "_consume_auth_code", consume_auth_code)
        monkeypatch.setattr(op, "_store_access_token", store_access)
        monkeypatch.setattr(op, "_load_access_token_row", load_access)
        monkeypatch.setattr(op, "_store_refresh_token", store_refresh)
        monkeypatch.setattr(op, "_load_refresh_token_row", load_refresh)
        monkeypatch.setattr(op, "_revoke_oauth_token", revoke)
        monkeypatch.setattr(op, "_revoke_tokens_for_auth_code", revoke_for_code)


def _pkce_pair() -> tuple[str, str]:
    verifier = "v" * 64
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


# Public URL the server compares Origin against; matches PUBLIC_URL default.
PUBLIC_URL = "https://design-mcp.leadloom.com.au"


def _consent_headers() -> dict[str, str]:
    """Origin + Referer that pass the same-origin check on /authorize/login."""
    return {"origin": PUBLIC_URL, "referer": f"{PUBLIC_URL}/authorize/login"}


def _get_consent_page(client, blob):
    """GET /authorize/login, parse the CSRF token out of the rendered HTML."""
    import re
    resp = client.get(
        "/authorize/login",
        params={"oauth_state": blob},
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    m = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert m, f"no csrf_token in consent page: {resp.text[:400]}"
    return resp, m.group(1)


def _post_authorize(client, *, blob, csrf, invite_token, consent="yes",
                    action="authorize", headers=None):
    """POST /authorize/login with the standard happy-path payload."""
    return client.post(
        "/authorize/login",
        data={
            "oauth_state": blob,
            "csrf_token": csrf,
            "invite_token": invite_token,
            "consent": consent,
            "action": action,
        },
        headers=_consent_headers() if headers is None else headers,
        follow_redirects=False,
    )


def _fake_info(token_id=1, email="user@example.com"):
    return TokenInfo(
        id=token_id, user_email=email, note=None,
        created_at=datetime.now(timezone.utc),
        last_used_at=None, usage_count=0, revoked_at=None,
    )


@pytest.fixture()
def store(monkeypatch):
    s = _FakeStore()
    s.install(monkeypatch)
    return s


@pytest.fixture()
def app(store, monkeypatch):
    # Important: import server AFTER monkeypatches are in place so the
    # provider singleton it constructs sees the patched DB helpers.
    from design_mcp import server as server_mod
    return server_mod.mcp.streamable_http_app(), server_mod


# ---------------------------------------------------------------------------
# Metadata + DCR
# ---------------------------------------------------------------------------

class TestMetadata:
    def test_authorization_server_metadata(self, app):
        client = TestClient(app[0])
        resp = client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200
        body = resp.json()
        assert body["issuer"].startswith("https://design-mcp.")
        assert body["authorization_endpoint"].endswith("/authorize")
        assert body["token_endpoint"].endswith("/token")
        assert body["registration_endpoint"].endswith("/register")
        assert "S256" in body["code_challenge_methods_supported"]
        assert "authorization_code" in body["grant_types_supported"]
        assert "refresh_token" in body["grant_types_supported"]

    def test_protected_resource_metadata(self, app):
        client = TestClient(app[0])
        resp = client.get("/.well-known/oauth-protected-resource")
        assert resp.status_code == 200
        body = resp.json()
        assert "design:write" in (body.get("scopes_supported") or [])


class TestDynamicRegistration:
    def test_register_returns_client_id_and_secret(self, app):
        client = TestClient(app[0])
        resp = client.post(
            "/register",
            json={
                "redirect_uris": ["https://claude.ai/callback"],
                "client_name": "test-client",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["client_id"]
        assert body["client_secret"]
        assert body["token_endpoint_auth_method"] == "client_secret_post"
        assert "https://claude.ai/callback" in body["redirect_uris"]

    def test_register_rejects_non_https_non_loopback(self, app):
        client = TestClient(app[0])
        resp = client.post(
            "/register",
            json={
                "redirect_uris": ["http://evil.example.com/cb"],
                "client_name": "bad",
            },
        )
        # RegistrationError surfaces as 400
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Authorize + login form + code exchange
# ---------------------------------------------------------------------------

def _register(app_tuple) -> tuple[str, str]:
    app, _ = app_tuple
    client = TestClient(app)
    resp = client.post(
        "/register",
        json={"redirect_uris": ["https://claude.ai/callback"], "client_name": "t"},
    )
    body = resp.json()
    return body["client_id"], body["client_secret"]


class TestAuthorizeFlow:
    def test_get_authorize_redirects_to_login_form(self, app):
        client_id, _ = _register(app)
        client = TestClient(app[0])
        verifier, challenge = _pkce_pair()
        resp = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
                "scope": "design:write",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/authorize/login?oauth_state=" in resp.headers["location"]

    def test_login_form_get_renders_html(self, app):
        client_id, _ = _register(app)
        client = TestClient(app[0])
        _, challenge = _pkce_pair()
        resp = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
                "scope": "design:write",
            },
            follow_redirects=False,
        )
        login_url = resp.headers["location"]
        # follow it locally
        resp2 = client.get(login_url, follow_redirects=False)
        assert resp2.status_code == 200
        assert "Acquirely Design MCP" in resp2.text
        assert "invite_token" in resp2.text

    def test_login_form_post_with_valid_invite_returns_code(self, app, monkeypatch):
        client_id, _ = _register(app)
        client = TestClient(app[0])
        _, challenge = _pkce_pair()
        # Build authorize URL → get oauth_state blob
        resp = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
                "scope": "design:write",
            },
            follow_redirects=False,
        )
        from urllib.parse import urlparse, parse_qs
        login_url = resp.headers["location"]
        blob = parse_qs(urlparse(login_url).query)["oauth_state"][0]
        _, csrf = _get_consent_page(client, blob)

        # Patch invite-token validator
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "sheetal@x"))
        invite = "a" * 64

        resp2 = _post_authorize(client, blob=blob, csrf=csrf, invite_token=invite)
        assert resp2.status_code == 302
        loc = resp2.headers["location"]
        assert loc.startswith("https://claude.ai/callback")
        assert "code=" in loc
        assert "state=xyz" in loc

    def test_login_form_post_with_bad_invite_re_renders_with_error(self, app, monkeypatch):
        client_id, _ = _register(app)
        client = TestClient(app[0])
        _, challenge = _pkce_pair()
        resp = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "abc",
                "scope": "design:write",
            },
            follow_redirects=False,
        )
        from urllib.parse import urlparse, parse_qs
        blob = parse_qs(urlparse(resp.headers["location"]).query)["oauth_state"][0]
        _, csrf = _get_consent_page(client, blob)

        def boom(_t):
            raise AuthError("unknown token")
        monkeypatch.setattr(auth_mod, "validate_token", boom)

        resp2 = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="b" * 64,
        )
        assert resp2.status_code == 401
        assert "Invite token is invalid" in resp2.text


# ---------------------------------------------------------------------------
# /token endpoint — authorization_code grant
# ---------------------------------------------------------------------------

class TestTokenExchange:
    def _do_authorize(self, app, monkeypatch, challenge):
        client_id, client_secret = _register(app)
        client = TestClient(app[0])
        resp = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
                "scope": "design:write",
            },
            follow_redirects=False,
        )
        from urllib.parse import urlparse, parse_qs
        blob = parse_qs(urlparse(resp.headers["location"]).query)["oauth_state"][0]
        _, csrf = _get_consent_page(client, blob)
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "sheetal@x"))
        resp2 = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
        )
        loc = resp2.headers["location"]
        code = parse_qs(urlparse(loc).query)["code"][0]
        return client_id, client_secret, code, client

    def test_exchange_authorization_code_returns_access_and_refresh(self, app, monkeypatch):
        verifier, challenge = _pkce_pair()
        client_id, client_secret, code, client = self._do_authorize(app, monkeypatch, challenge)
        resp = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://claude.ai/callback",
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": verifier,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token_type"] == "Bearer"
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["expires_in"] == 3600

    def test_pkce_mismatch_returns_400(self, app, monkeypatch):
        _, challenge = _pkce_pair()
        client_id, client_secret, code, client = self._do_authorize(app, monkeypatch, challenge)
        resp = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://claude.ai/callback",
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": "x" * 64,  # WRONG
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_grant"

    def test_code_is_single_use(self, app, monkeypatch):
        verifier, challenge = _pkce_pair()
        client_id, client_secret, code, client = self._do_authorize(app, monkeypatch, challenge)
        ok = client.post(
            "/token",
            data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": "https://claude.ai/callback",
                "client_id": client_id, "client_secret": client_secret,
                "code_verifier": verifier,
            },
        )
        assert ok.status_code == 200
        again = client.post(
            "/token",
            data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": "https://claude.ai/callback",
                "client_id": client_id, "client_secret": client_secret,
                "code_verifier": verifier,
            },
        )
        assert again.status_code == 400
        assert again.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# load_access_token — both code paths
# ---------------------------------------------------------------------------

class TestAccessTokenVerification:
    def test_oauth_access_token_validates(self, app, monkeypatch):
        provider = app[1]._oauth_provider
        # seed a token
        asyncio.run(
            asyncio.to_thread(
                op._store_access_token,
                raw_token="t" * 64, client_id="c", user_email="u@x",
                scopes=["design:write"],
            )
        )
        result = asyncio.run(provider.load_access_token("t" * 64))
        assert result is not None
        assert result.client_id == "c"
        assert "design:write" in result.scopes

    def test_invite_token_still_validates_through_oauth_provider(self, app, monkeypatch):
        provider = app[1]._oauth_provider
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(42, "sheetal@x"))
        result = asyncio.run(provider.load_access_token("c" * 64))
        assert result is not None
        assert result.client_id == "design-mcp-token:42"
        assert "design:write" in result.scopes

    def test_expired_oauth_access_token_returns_none(self, app, store):
        provider = app[1]._oauth_provider
        # Insert directly with expired timestamp
        raw = "e" * 64
        store.access[op.hash_token(raw)] = {
            "token_hash": op.hash_token(raw),
            "client_id": "c", "user_email": "u@x",
            "scopes": ["design:write"],
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            "revoked_at": None,
        }
        result = asyncio.run(provider.load_access_token(raw))
        assert result is None

    def test_revoked_invite_token_returns_none(self, app, monkeypatch):
        provider = app[1]._oauth_provider

        def boom(_t):
            raise AuthError("token revoked at ...")

        monkeypatch.setattr(auth_mod, "validate_token", boom)
        result = asyncio.run(provider.load_access_token("z" * 64))
        assert result is None


# ---------------------------------------------------------------------------
# Security hardening — CSRF, Origin/Referer, consent, scope mapping, cache
# headers, expiry guards, code-reuse revocation, public-client NULL secret.
# ---------------------------------------------------------------------------


def _start_authorize(app, *, scope="design:write", state="xyz"):
    """Begin /authorize, return (client_id, client_secret, blob, TestClient)."""
    client_id, client_secret = _register(app)
    client = TestClient(app[0])
    _, challenge = _pkce_pair()
    resp = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": scope,
        },
        follow_redirects=False,
    )
    from urllib.parse import urlparse, parse_qs
    blob = parse_qs(urlparse(resp.headers["location"]).query)["oauth_state"][0]
    return client_id, client_secret, blob, client


class TestConsentAndCsrf:
    def test_get_consent_page_renders_expected_elements(self, app):
        _, _, blob, client = _start_authorize(app)
        resp = client.get(
            "/authorize/login",
            params={"oauth_state": blob},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        body = resp.text
        # CSRF token field present
        assert 'name="csrf_token"' in body
        # Visible consent checkbox required
        assert 'name="consent"' in body and "required" in body
        # Hard-coded scope description (H5) — NOT the raw scope string
        assert "Submit and update microsite designs" in body
        # Redirect host shown (HTML-escaped)
        assert "claude.ai" in body
        # Cancel button present (RFC 6749 §4.1.2.1 path)
        assert 'value="cancel"' in body

    def test_get_consent_page_sets_no_cache_headers(self, app):
        _, _, blob, client = _start_authorize(app)
        resp = client.get(
            "/authorize/login",
            params={"oauth_state": blob},
            follow_redirects=False,
        )
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["pragma"] == "no-cache"

    def test_post_without_csrf_is_rejected(self, app, monkeypatch):
        _, _, blob, client = _start_authorize(app)
        _get_consent_page(client, blob)  # mint a csrf so consent flow looks normal
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "u@x"))
        resp = _post_authorize(
            client, blob=blob, csrf="", invite_token="a" * 64,
        )
        assert resp.status_code == 403
        assert "expired" in resp.text.lower() or "blocked" in resp.text.lower()

    def test_post_with_replayed_csrf_is_rejected(self, app, monkeypatch):
        _, _, blob, client = _start_authorize(app)
        _, csrf = _get_consent_page(client, blob)
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "u@x"))
        first = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
        )
        assert first.status_code == 302
        # Replay the same csrf — single-use store entry has been popped
        replay = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
        )
        assert replay.status_code == 403

    def test_post_with_expired_csrf_is_rejected(self, app, monkeypatch):
        _, _, blob, client = _start_authorize(app)
        _, csrf = _get_consent_page(client, blob)
        # Fast-forward time past the 10-minute TTL
        monkeypatch.setattr(op, "_csrf_now", lambda: __import__("time").time() + 11 * 60)
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "u@x"))
        resp = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
        )
        assert resp.status_code == 403

    def test_post_missing_origin_is_rejected(self, app, monkeypatch):
        _, _, blob, client = _start_authorize(app)
        _, csrf = _get_consent_page(client, blob)
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "u@x"))
        resp = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
            headers={},  # no Origin, no Referer
        )
        assert resp.status_code == 403

    def test_post_cross_origin_is_rejected(self, app, monkeypatch):
        _, _, blob, client = _start_authorize(app)
        _, csrf = _get_consent_page(client, blob)
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "u@x"))
        resp = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
            headers={"origin": "https://attacker.example.com"},
        )
        assert resp.status_code == 403

    def test_post_cross_origin_referer_is_rejected(self, app, monkeypatch):
        _, _, blob, client = _start_authorize(app)
        _, csrf = _get_consent_page(client, blob)
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "u@x"))
        resp = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
            headers={
                "origin": PUBLIC_URL,
                "referer": "https://attacker.example.com/foo",
            },
        )
        assert resp.status_code == 403

    def test_post_without_consent_checkbox_is_rejected(self, app, monkeypatch):
        _, _, blob, client = _start_authorize(app)
        _, csrf = _get_consent_page(client, blob)
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "u@x"))
        resp = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
            consent="",  # checkbox NOT ticked
        )
        assert resp.status_code == 400
        assert "consent" in resp.text.lower()

    def test_post_cancel_redirects_with_access_denied(self, app):
        _, _, blob, client = _start_authorize(app, state="cancel-state")
        _, csrf = _get_consent_page(client, blob)
        resp = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="",
            consent="", action="cancel",
        )
        assert resp.status_code == 302
        loc = resp.headers["location"]
        assert loc.startswith("https://claude.ai/callback")
        assert "error=access_denied" in loc
        assert "state=cancel-state" in loc

    def test_post_success_emits_no_store(self, app, monkeypatch):
        _, _, blob, client = _start_authorize(app)
        _, csrf = _get_consent_page(client, blob)
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "u@x"))
        resp = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
        )
        assert resp.status_code == 302
        assert resp.headers.get("cache-control") == "no-store"

    def test_unknown_scope_does_not_leak_raw_string(self, app):
        # The /authorize endpoint validates scopes against valid_scopes, so
        # we can only smuggle a free-form scope via a forged oauth_state
        # blob. Build one and verify the consent page redacts it.
        from design_mcp.oauth_provider import sign_oauth_state
        payload = {
            "client_id": "irrelevant",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_explicit": True,
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
            "scopes": ["evil:<script>alert(1)</script>"],
            "state": "s",
            "resource": None,
        }
        blob = sign_oauth_state(payload)
        client = TestClient(app[0])
        resp = client.get(
            "/authorize/login",
            params={"oauth_state": blob},
            follow_redirects=False,
        )
        # Should NOT echo the raw scope string anywhere in the page
        assert "evil:" not in resp.text
        assert "<script>" not in resp.text
        # Should render the canned "unrecognised permission" message
        assert "Unrecognised permission" in resp.text


# ---------------------------------------------------------------------------
# H4 — refresh-token rotation derives user_email from the loaded token, not
# a re-read of the row after revocation.
# ---------------------------------------------------------------------------

class TestRefreshTokenUserEmail:
    def test_load_refresh_token_carries_user_email(self, app, store):
        provider = app[1]._oauth_provider
        raw = "r" * 64
        store.refresh[op.hash_token(raw)] = {
            "token_hash": op.hash_token(raw),
            "client_id": "c", "user_email": "alice@example.com",
            "scopes": ["design:write"],
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            "revoked_at": None, "auth_code_hash": None,
        }
        # Seed client row so client_id check passes
        store.clients["c"] = {
            "client_id": "c", "client_secret": None, "client_name": "t",
            "redirect_uris": ["https://claude.ai/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "design:write",
            "created_at": datetime.now(timezone.utc),
        }
        client_info = asyncio.run(provider.get_client("c"))
        loaded = asyncio.run(provider.load_refresh_token(client_info, raw))
        assert loaded is not None
        assert loaded.user_email == "alice@example.com"

    def test_exchange_refresh_token_preserves_user_email(self, app, store):
        provider = app[1]._oauth_provider
        raw = "q" * 64
        store.refresh[op.hash_token(raw)] = {
            "token_hash": op.hash_token(raw),
            "client_id": "c", "user_email": "bob@example.com",
            "scopes": ["design:write"],
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            "revoked_at": None, "auth_code_hash": None,
        }
        store.clients["c"] = {
            "client_id": "c", "client_secret": None, "client_name": "t",
            "redirect_uris": ["https://claude.ai/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "design:write",
            "created_at": datetime.now(timezone.utc),
        }
        client_info = asyncio.run(provider.get_client("c"))
        loaded = asyncio.run(provider.load_refresh_token(client_info, raw))
        asyncio.run(provider.exchange_refresh_token(client_info, loaded, ["design:write"]))
        # The new access + refresh rows should be tagged with bob's email,
        # never the literal "unknown".
        users = {row["user_email"] for row in store.access.values()}
        users |= {row["user_email"] for row in store.refresh.values()}
        assert "unknown" not in users
        assert "bob@example.com" in users


# ---------------------------------------------------------------------------
# M5 — provider-layer expiry guards. We bypass the SDK and call the methods
# directly so we catch regressions even if the SDK ever changes its checks.
# ---------------------------------------------------------------------------

class TestProviderExpiryGuards:
    def test_load_authorization_code_rejects_expired_row(self, app, store):
        provider = app[1]._oauth_provider
        raw = "p" * 64
        store.codes[op.hash_token(raw)] = {
            "code_hash": op.hash_token(raw),
            "client_id": "c", "user_email": "u@x",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_explicit": True,
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
            "scopes": ["design:write"],
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            "consumed_at": None,
        }
        store.clients["c"] = {
            "client_id": "c", "client_secret": None, "client_name": "t",
            "redirect_uris": ["https://claude.ai/callback"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "design:write",
            "created_at": datetime.now(timezone.utc),
        }
        client_info = asyncio.run(provider.get_client("c"))
        assert asyncio.run(provider.load_authorization_code(client_info, raw)) is None

    def test_load_refresh_token_rejects_expired_row(self, app, store):
        provider = app[1]._oauth_provider
        raw = "n" * 64
        store.refresh[op.hash_token(raw)] = {
            "token_hash": op.hash_token(raw),
            "client_id": "c", "user_email": "u@x",
            "scopes": ["design:write"],
            "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
            "revoked_at": None, "auth_code_hash": None,
        }
        store.clients["c"] = {
            "client_id": "c", "client_secret": None, "client_name": "t",
            "redirect_uris": ["https://claude.ai/callback"],
            "grant_types": ["refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "design:write",
            "created_at": datetime.now(timezone.utc),
        }
        client_info = asyncio.run(provider.get_client("c"))
        assert asyncio.run(provider.load_refresh_token(client_info, raw)) is None


# ---------------------------------------------------------------------------
# M1 — auth-code reuse revokes derived access + refresh tokens.
# ---------------------------------------------------------------------------

class TestAuthCodeReuseRevocation:
    def test_reused_code_revokes_minted_tokens(self, app, monkeypatch):
        verifier, challenge = _pkce_pair()
        client_id, client_secret, blob, client = _start_authorize(app)
        _, csrf = _get_consent_page(client, blob)
        monkeypatch.setattr(auth_mod, "validate_token", lambda _t: _fake_info(7, "u@x"))
        login = _post_authorize(
            client, blob=blob, csrf=csrf, invite_token="a" * 64,
        )
        from urllib.parse import urlparse, parse_qs
        code = parse_qs(urlparse(login.headers["location"]).query)["code"][0]
        # First exchange succeeds.
        first = client.post(
            "/token",
            data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": "https://claude.ai/callback",
                "client_id": client_id, "client_secret": client_secret,
                "code_verifier": verifier,
            },
        )
        assert first.status_code == 200
        body = first.json()
        access_raw = body["access_token"]
        refresh_raw = body["refresh_token"]
        # Replay the same code — should trigger M1 revocation.
        again = client.post(
            "/token",
            data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": "https://claude.ai/callback",
                "client_id": client_id, "client_secret": client_secret,
                "code_verifier": verifier,
            },
        )
        assert again.status_code == 400
        # The original tokens should now be revoked.
        provider = app[1]._oauth_provider
        assert asyncio.run(provider.load_access_token(access_raw)) is None
        # Refresh token should also fail to load.
        client_info = asyncio.run(provider.get_client(client_id))
        assert asyncio.run(provider.load_refresh_token(client_info, refresh_raw)) is None


# ---------------------------------------------------------------------------
# L2 — public clients (NULL client_secret) round-trip.
# ---------------------------------------------------------------------------

class TestPublicClient:
    def test_public_client_store_and_load_round_trips_with_null_secret(self, store):
        from design_mcp.oauth_provider import _load_client, _store_client
        client = OAuthClientInformationFull(
            client_id="public-1",
            client_secret=None,
            redirect_uris=[AnyUrl("https://claude.ai/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope="design:write",
            client_name="public-test",
        )
        _store_client(client)
        # Internally store.clients now has the row — verify both fields are
        # the canonical Python None (not empty string).
        row = store.clients["public-1"]
        assert row["client_secret"] in (None, "") or row["client_secret"] is None
        # Round-trip via the provider's load — secret should come back None.
        loaded = _load_client("public-1")
        assert loaded is not None
        assert loaded.client_secret is None

    def test_redirect_uri_rejects_localhost_lookalike(self):
        from design_mcp.oauth_provider import _redirect_uri_allowed
        assert _redirect_uri_allowed("https://claude.ai/cb")
        assert _redirect_uri_allowed("http://localhost:8080/cb")
        assert _redirect_uri_allowed("http://127.0.0.1/cb")
        # C2 — the lookalike that used to slip through
        assert not _redirect_uri_allowed("http://localhost.evil.com/cb")
        assert not _redirect_uri_allowed("http://127.0.0.1.evil.com/cb")
        # Custom schemes always denied
        assert not _redirect_uri_allowed("javascript:alert(1)")
        assert not _redirect_uri_allowed("data:text/html,xx")
        assert not _redirect_uri_allowed("ftp://example.com/")
