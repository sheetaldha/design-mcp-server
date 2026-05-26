"""Tests for BearerTokenVerifier + the SDK's bearer-auth middleware path.

We don't spin a real DB here — `design_mcp.auth.validate_token` is patched.
The middleware-level test exercises the same `BearerAuthBackend` that FastMCP
wires when `auth=` + `token_verifier=` are both set, so we get end-to-end
coverage of "no header / bad token / good token" without booting a server.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("TOKEN_DB_PASSWORD", "test-only-not-used")

from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.authentication import requires  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.authentication import AuthenticationMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from design_mcp import auth as auth_mod  # noqa: E402
from design_mcp.auth import AuthError, TokenInfo  # noqa: E402
from design_mcp.token_verifier import (  # noqa: E402
    DESIGN_WRITE_SCOPE,
    BearerTokenVerifier,
)


VALID = "a" * 64
INVALID = "b" * 64


def _fake_info(token_id: int = 1, email: str = "u@example.com") -> TokenInfo:
    return TokenInfo(
        id=token_id,
        user_email=email,
        note=None,
        created_at=datetime.now(timezone.utc),
        last_used_at=None,
        usage_count=0,
        revoked_at=None,
    )


# ---------------------------------------------------------------------------
# Unit — BearerTokenVerifier.verify_token
# ---------------------------------------------------------------------------

class TestBearerTokenVerifier:
    def test_valid_token_returns_access_token(self, monkeypatch):
        monkeypatch.setattr(auth_mod, "validate_token", lambda tok: _fake_info(7, "sheetal@x"))
        verifier = BearerTokenVerifier()
        result = asyncio.run(verifier.verify_token(VALID))
        assert result is not None
        assert result.token == VALID
        assert result.client_id == "design-mcp-token:7"
        assert DESIGN_WRITE_SCOPE in result.scopes

    def test_unknown_token_returns_none(self, monkeypatch):
        def boom(_tok):
            raise AuthError("unknown token")

        monkeypatch.setattr(auth_mod, "validate_token", boom)
        result = asyncio.run(BearerTokenVerifier().verify_token(INVALID))
        assert result is None

    def test_revoked_token_returns_none(self, monkeypatch):
        def boom(_tok):
            raise AuthError("token revoked at ...")

        monkeypatch.setattr(auth_mod, "validate_token", boom)
        result = asyncio.run(BearerTokenVerifier().verify_token(VALID))
        assert result is None

    def test_unexpected_error_returns_none(self, monkeypatch):
        def boom(_tok):
            raise RuntimeError("db down")

        monkeypatch.setattr(auth_mod, "validate_token", boom)
        result = asyncio.run(BearerTokenVerifier().verify_token(VALID))
        assert result is None


# ---------------------------------------------------------------------------
# Integration — BearerAuthBackend wrapped in Starlette mirrors FastMCP's wiring
# ---------------------------------------------------------------------------

@requires(DESIGN_WRITE_SCOPE)
async def _protected(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "user": request.user.username})


def _build_app(verifier: BearerTokenVerifier) -> Starlette:
    return Starlette(
        routes=[Route("/mcp", _protected, methods=["GET", "POST"])],
        middleware=[
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(verifier)),
        ],
    )


class TestBearerAuthMiddleware:
    def test_no_authorization_header_returns_401(self, monkeypatch):
        monkeypatch.setattr(auth_mod, "validate_token", lambda tok: _fake_info())
        client = TestClient(_build_app(BearerTokenVerifier()))
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 403  # Starlette `requires` returns 403 w/o auth

    def test_invalid_bearer_returns_401(self, monkeypatch):
        def boom(_tok):
            raise AuthError("unknown token")

        monkeypatch.setattr(auth_mod, "validate_token", boom)
        client = TestClient(_build_app(BearerTokenVerifier()))
        resp = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {INVALID}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert resp.status_code == 403

    def test_valid_bearer_reaches_handler(self, monkeypatch):
        monkeypatch.setattr(auth_mod, "validate_token", lambda tok: _fake_info(42, "ok@x"))
        client = TestClient(_build_app(BearerTokenVerifier()))
        resp = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {VALID}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "user": "design-mcp-token:42"}


# ---------------------------------------------------------------------------
# Smoke — server module imports + FastMCP is constructed with auth wired in
# ---------------------------------------------------------------------------

class TestServerAuthWiring:
    def test_server_module_constructs_with_auth(self):
        from design_mcp import server as server_mod

        assert server_mod.mcp.settings.auth is not None
        assert server_mod.mcp.settings.auth.required_scopes == [DESIGN_WRITE_SCOPE]

    def test_unauthenticated_streamable_http_returns_401(self, monkeypatch):
        # `requires(...)` returns 401 (not 403) when used through FastMCP because
        # the RequireAuthMiddleware short-circuits before Starlette's decorator.
        from design_mcp import server as server_mod

        app = server_mod.mcp.streamable_http_app()
        client = TestClient(app)
        resp = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        assert resp.status_code == 401

    def test_invalid_bearer_streamable_http_returns_401(self, monkeypatch):
        def boom(_tok):
            raise AuthError("unknown token")

        monkeypatch.setattr(auth_mod, "validate_token", boom)
        # The OAuthProvider.load_access_token first checks the oauth tokens
        # table; stub it out so no DB connection is needed in CI.
        from design_mcp import oauth_provider as _op
        monkeypatch.setattr(_op, "_load_access_token_row", lambda _t: None)
        from design_mcp import server as server_mod

        app = server_mod.mcp.streamable_http_app()
        client = TestClient(app)
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {INVALID}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
        )
        assert resp.status_code == 401

    def test_well_known_protected_resource_route_exists(self):
        from design_mcp import server as server_mod

        app = server_mod.mcp.streamable_http_app()
        client = TestClient(app)
        # RFC 9728 metadata — should be reachable without auth.
        resp = client.get("/.well-known/oauth-protected-resource")
        assert resp.status_code == 200
        body = resp.json()
        assert "resource" in body
        assert "authorization_servers" in body
