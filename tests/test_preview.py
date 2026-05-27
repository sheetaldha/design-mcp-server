"""Tests for the signed-URL preview module + /preview route + get_preview_url tool.

The HMAC signing key reuses TOKEN_DB_PASSWORD (same key OAuth uses for
oauth_state). Tests set it to a fixed value below.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("TOKEN_DB_PASSWORD", "test-only-not-used")

from mcp.server.auth.middleware.auth_context import auth_context_var  # noqa: E402
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from design_mcp import drafts  # noqa: E402
from design_mcp import preview as preview_mod  # noqa: E402
from design_mcp import server as server_mod  # noqa: E402
from design_mcp.preview import (  # noqa: E402
    signed_preview_url,
    verify_preview_signature,
)
from design_mcp.server import (  # noqa: E402
    AuthContextError,
    design_landing_page,
    get_preview_url,
)


DEFAULT_USER = "sheetal@acquirely.com.au"
OTHER_USER = "evil@example.com"


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_day3_refactor.py)
# ---------------------------------------------------------------------------

@contextmanager
def _set_user(email: str) -> Iterator[None]:
    fake_token = AccessToken(
        token="t" * 64,
        client_id=f"test-client:{email}",
        scopes=["design:write"],
        expires_at=None,
    )
    try:
        object.__setattr__(fake_token, "__user_email", email)
    except Exception:
        pass
    fake_user = AuthenticatedUser(fake_token)
    handle = auth_context_var.set(fake_user)
    try:
        yield
    finally:
        auth_context_var.reset(handle)


@pytest.fixture(autouse=True)
def _reset_drafts():
    drafts._reset_for_tests()
    yield
    drafts._reset_for_tests()


@pytest.fixture(autouse=True)
def _default_user_context():
    with _set_user(DEFAULT_USER):
        yield


# ---------------------------------------------------------------------------
# signed_preview_url / verify_preview_signature
# ---------------------------------------------------------------------------

class TestSignedPreviewUrl:
    def test_url_contains_exp_and_sig(self):
        url = signed_preview_url("abc-123", DEFAULT_USER, ttl_seconds=60)
        parts = urlsplit(url)
        assert parts.path == "/preview/abc-123"
        qs = parse_qs(parts.query)
        assert "exp" in qs and "sig" in qs
        assert int(qs["exp"][0]) > int(time.time())
        # HMAC-SHA256 hex = 64 chars
        assert len(qs["sig"][0]) == 64

    def test_url_includes_public_host(self):
        url = signed_preview_url("d-1", DEFAULT_USER, ttl_seconds=60)
        assert url.startswith("https://") and "/preview/d-1?" in url

    def test_missing_design_id_raises(self):
        with pytest.raises(ValueError):
            signed_preview_url("", DEFAULT_USER)

    def test_missing_user_email_raises(self):
        with pytest.raises(ValueError):
            signed_preview_url("d", "")

    def test_non_positive_ttl_raises(self):
        with pytest.raises(ValueError):
            signed_preview_url("d", DEFAULT_USER, ttl_seconds=0)
        with pytest.raises(ValueError):
            signed_preview_url("d", DEFAULT_USER, ttl_seconds=-1)


class TestVerifyPreviewSignature:
    def test_valid_signature_passes(self):
        url = signed_preview_url("did-1", DEFAULT_USER, ttl_seconds=60)
        qs = parse_qs(urlsplit(url).query)
        assert verify_preview_signature(
            "did-1", int(qs["exp"][0]), qs["sig"][0], user_email=DEFAULT_USER,
        )

    def test_tampered_signature_rejected(self):
        url = signed_preview_url("did-1", DEFAULT_USER, ttl_seconds=60)
        qs = parse_qs(urlsplit(url).query)
        bad_sig = "0" * 64
        assert not verify_preview_signature(
            "did-1", int(qs["exp"][0]), bad_sig, user_email=DEFAULT_USER,
        )

    def test_expired_exp_rejected(self):
        # Mint with TTL=60, then forge an exp in the past with a valid sig.
        past_exp = int(time.time()) - 10
        sig = preview_mod._sig_for("did-1", DEFAULT_USER, past_exp)
        assert not verify_preview_signature(
            "did-1", past_exp, sig, user_email=DEFAULT_USER,
        )

    def test_wrong_user_email_rejected(self):
        url = signed_preview_url("did-1", DEFAULT_USER, ttl_seconds=60)
        qs = parse_qs(urlsplit(url).query)
        # Same sig verified against a different user_email must fail.
        assert not verify_preview_signature(
            "did-1", int(qs["exp"][0]), qs["sig"][0], user_email=OTHER_USER,
        )

    def test_wrong_design_id_rejected(self):
        url = signed_preview_url("did-1", DEFAULT_USER, ttl_seconds=60)
        qs = parse_qs(urlsplit(url).query)
        assert not verify_preview_signature(
            "did-2", int(qs["exp"][0]), qs["sig"][0], user_email=DEFAULT_USER,
        )

    def test_garbage_exp_rejected(self):
        assert not verify_preview_signature("d", "notanint", "abcd", user_email=DEFAULT_USER)


# ---------------------------------------------------------------------------
# get_preview_url MCP tool
# ---------------------------------------------------------------------------

def _seed_draft_with_html(user: str = DEFAULT_USER) -> str:
    """Create a draft and stash a non-empty html column on it. Returns design_id."""
    record = drafts.create(
        user_email=user, family="landing-page",
        brief="preview test", slug_hint="preview-test",
    )
    drafts.update(
        record.design_id, user,
        html="<!doctype html><html><head><title>x</title></head><body><h1>x</h1></body></html>",
    )
    return record.design_id


class TestGetPreviewUrlTool:
    def test_requires_auth_context(self):
        # Tear down the auto-installed user context entirely.
        handle = auth_context_var.set(None)
        try:
            with pytest.raises(AuthContextError):
                get_preview_url(design_id="anything")
        finally:
            auth_context_var.reset(handle)

    def test_returns_not_found_for_unknown_id(self):
        result = get_preview_url(design_id="00000000-0000-0000-0000-000000000000")
        assert result["ok"] is False
        assert any("not owned by this user" in e for e in result["errors"])

    def test_returns_error_when_no_html_yet(self):
        # Fresh draft — no html column populated.
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="x", slug_hint="x",
        )
        result = get_preview_url(design_id=record.design_id)
        assert result["ok"] is False
        assert "hint" in result
        assert any("no html yet" in e for e in result["errors"])

    def test_returns_signed_url_on_valid_draft(self):
        design_id = _seed_draft_with_html()
        result = get_preview_url(design_id=design_id)
        assert result["ok"] is True
        assert result["design_id"] == design_id
        assert result["ttl_seconds"] == 3600
        assert "/preview/" in result["url"]
        assert "exp=" in result["url"] and "sig=" in result["url"]
        assert result["note"].startswith("Open this in any browser")
        # expires_at is ISO-format and in the future.
        from datetime import datetime
        exp_dt = datetime.fromisoformat(result["expires_at"])
        assert exp_dt.timestamp() > time.time()

    def test_cross_user_blocked(self):
        with _set_user("alice@x.com"):
            design_id = _seed_draft_with_html("alice@x.com")
        with _set_user("bob@x.com"):
            result = get_preview_url(design_id=design_id)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# /preview/{design_id} HTTP route
# ---------------------------------------------------------------------------

def _route_client():
    return TestClient(server_mod.mcp.streamable_http_app())


class TestPreviewRoute:
    def test_invalid_signature_returns_403(self):
        design_id = _seed_draft_with_html()
        client = _route_client()
        # exp is in the future so the failure is purely on sig.
        future_exp = int(time.time()) + 600
        resp = client.get(
            f"/preview/{design_id}",
            params={"exp": future_exp, "sig": "0" * 64},
        )
        assert resp.status_code == 403
        assert "expired or invalid" in resp.text.lower()
        assert resp.headers.get("X-Robots-Tag") == "noindex, nofollow"

    def test_expired_signature_returns_403(self):
        design_id = _seed_draft_with_html()
        past_exp = int(time.time()) - 60
        # Mint a valid sig for that past exp — the verify guard rejects on exp.
        sig = preview_mod._sig_for(design_id, DEFAULT_USER, past_exp)
        client = _route_client()
        resp = client.get(f"/preview/{design_id}", params={"exp": past_exp, "sig": sig})
        assert resp.status_code == 403

    def test_garbage_exp_returns_403(self):
        client = _route_client()
        resp = client.get("/preview/anything", params={"exp": "notanint", "sig": "x"})
        assert resp.status_code == 403

    def test_draft_without_html_returns_404(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="no-html", slug_hint="no-html",
        )
        # Mint a valid signature anyway — the route still 404s because html is empty.
        url = signed_preview_url(record.design_id, DEFAULT_USER, ttl_seconds=60)
        parts = urlsplit(url)
        client = _route_client()
        resp = client.get(parts.path, params=dict(parse_qs(parts.query, keep_blank_values=True)))
        assert resp.status_code == 404
        assert "no html yet" in resp.text.lower()

    def test_valid_signature_serves_html(self):
        design_id = _seed_draft_with_html()
        url = signed_preview_url(design_id, DEFAULT_USER, ttl_seconds=60)
        parts = urlsplit(url)
        client = _route_client()
        # Convert parse_qs lists into single-value pairs for httpx.
        qs = {k: v[0] for k, v in parse_qs(parts.query).items()}
        resp = client.get(parts.path, params=qs)
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert ct.startswith("text/html")
        assert "charset=utf-8" in ct.lower()
        assert resp.headers.get("Cache-Control") == "no-store"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Robots-Tag") == "noindex, nofollow"
        assert "<h1>x</h1>" in resp.text
