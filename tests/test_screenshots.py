"""Tests for design_mcp.screenshots — multi-provider URL screenshot orchestrator.

Coverage:
- validate_url accepts public http(s) URLs
- validate_url rejects localhost, 127.0.0.1, 169.254.169.254, private IPs, non-http schemes
- fetch_screenshots calls all 3 viewports in parallel (HTTP mocked via respx)
- Microlink succeeds → returns URL with provider="microlink" for all 3
- Microlink fails → falls back to ApiFlash → ScreenshotMachine
- All providers fail → raises ScreenshotError
- Cache hit returns same result without HTTP calls; cached=True
- Cache expiry after 24h triggers fresh fetch
- _url_hash is deterministic
- fetch_url_screenshots MCP tool returns the correct dict shape
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import pytest
import respx

# Make sure the screenshot module imports cleanly without a live DB.
os.environ.setdefault("TOKEN_DB_PASSWORD", "test-only-not-used")

from design_mcp import screenshots as scr  # noqa: E402
from design_mcp.screenshots import (  # noqa: E402
    ScreenshotError,
    ScreenshotResult,
    Viewport,
    _url_hash,
    fetch_screenshots,
    validate_url,
)


# ---------------------------------------------------------------------------
# Fixtures — stub the PG-backed cache so we don't need a live DB.
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_store(monkeypatch):
    """In-memory dict standing in for the design_mcp_screenshot_cache table.

    Key = url_hash, value = dict of column → value (matching the row shape
    that `_cache_get` reads, plus 'fetched_at' as a float epoch we can rewind
    to simulate the 24h expiry).
    """
    import time
    store: dict[str, dict[str, Any]] = {}

    def fake_cache_get(url: str):
        h = _url_hash(url)
        row = store.get(h)
        if not row:
            return None
        # 24h expiry — same window the SQL uses.
        if time.time() - row["fetched_at"] > 86400:
            return None
        return {
            "mobile":  ScreenshotResult("mobile",  row["mobile_url"],  row["mobile_provider"],  cached=True),
            "ipad":    ScreenshotResult("ipad",    row["ipad_url"],    row["ipad_provider"],    cached=True),
            "desktop": ScreenshotResult("desktop", row["desktop_url"], row["desktop_provider"], cached=True),
        }

    def fake_cache_put(url: str, results: dict[str, ScreenshotResult]) -> None:
        store[_url_hash(url)] = {
            "mobile_url":       results["mobile"].url,
            "ipad_url":         results["ipad"].url,
            "desktop_url":      results["desktop"].url,
            "mobile_provider":  results["mobile"].provider,
            "ipad_provider":    results["ipad"].provider,
            "desktop_provider": results["desktop"].provider,
            "fetched_at":       time.time(),
        }

    monkeypatch.setattr(scr, "_cache_get", fake_cache_get)
    monkeypatch.setattr(scr, "_cache_put", fake_cache_put)
    return store


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch):
    """Default — neither paid provider has a key; only Microlink runs.

    Tests that need ApiFlash / ScreenshotMachine override the env explicitly.
    """
    monkeypatch.delenv("APIFLASH_ACCESS_KEY", raising=False)
    monkeypatch.delenv("SCREENSHOTMACHINE_KEY", raising=False)


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """Make validate_url DNS-resolution deterministic and offline.

    Real DNS hits during unit tests are slow + flaky. Stub
    socket.gethostbyname so example.com / acquirely.com.au resolve to a
    public IP and any explicit private/loopback hostname resolves to its
    matching family of address.
    """
    import socket as _socket

    def fake_gethostbyname(host: str) -> str:
        # The blocked-host short-circuit in validate_url catches localhost /
        # 127.0.0.1 / 169.254.169.254 / 0.0.0.0 by string match BEFORE DNS,
        # so this only handles other names. Tests that need a private-IP
        # resolution use an explicit hostname that we map here.
        if host == "intranet.example.com":
            return "10.0.0.5"        # private — should be rejected
        if host == "linklocal.example.com":
            return "169.254.0.42"    # link-local — should be rejected
        if host == "nope.invalid":
            raise _socket.gaierror("does not resolve")
        # Any other host → a stable public IP.
        return "93.184.216.34"

    monkeypatch.setattr(scr.socket, "gethostbyname", fake_gethostbyname)


# ---------------------------------------------------------------------------
# validate_url
# ---------------------------------------------------------------------------

class TestValidateUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "https://example.com/path?x=1",
            "https://www.acquirely.com.au/",
            "http://example.com:8080/page",
        ],
    )
    def test_accepts_public_http_urls(self, url):
        assert validate_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/admin",
            "http://127.0.0.1:8080",
            "http://169.254.169.254/latest/meta-data",  # AWS instance metadata
            "http://0.0.0.0/",
        ],
    )
    def test_rejects_blocked_hosts(self, url):
        with pytest.raises(ValueError):
            validate_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://intranet.example.com/secret",   # private IP per fake DNS
            "http://linklocal.example.com/",        # link-local per fake DNS
        ],
    )
    def test_rejects_private_or_link_local_ip_resolutions(self, url):
        with pytest.raises(ValueError, match="private|internal|link"):
            validate_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://example.com",
            "javascript:alert(1)",
            "ftp://example.com/file",
        ],
    )
    def test_rejects_non_http_schemes(self, url):
        with pytest.raises(ValueError, match="scheme"):
            validate_url(url)

    def test_rejects_unresolvable_host(self):
        with pytest.raises(ValueError, match="does not resolve"):
            validate_url("http://nope.invalid/")


# ---------------------------------------------------------------------------
# _url_hash
# ---------------------------------------------------------------------------

class TestUrlHash:
    def test_deterministic(self):
        # Same input → same hash (across calls).
        a = _url_hash("https://example.com/")
        b = _url_hash("https://example.com/")
        assert a == b

    def test_different_inputs_produce_different_hashes(self):
        assert _url_hash("https://a.example.com/") != _url_hash("https://b.example.com/")

    def test_hash_is_hex_of_expected_length(self):
        h = _url_hash("https://example.com/")
        assert len(h) == 64  # sha256 hex
        int(h, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# fetch_screenshots — orchestration + provider fallback
# ---------------------------------------------------------------------------

def _microlink_success_payload(target_url: str, viewport: Viewport) -> dict:
    """Build the Microlink JSON envelope a real success returns."""
    return {
        "status": "success",
        "data": {
            "screenshot": {
                "url": f"https://cdn.microlink.io/{viewport.label}/{target_url}.png",
            },
        },
    }


def _microlink_failure_payload() -> dict:
    return {"status": "fail", "data": {}}


class TestFetchScreenshots:
    @pytest.mark.asyncio
    async def test_microlink_success_returns_three_viewports(self, cache_store):
        url = "https://example.com/landing"
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.microlink.io/").mock(
                side_effect=lambda req: httpx.Response(
                    200,
                    json=_microlink_success_payload(
                        url,
                        # Pull the viewport off the request's query params for variety.
                        next(
                            vp for vp in Viewport
                            if str(vp.width) == req.url.params["viewport.width"]
                        ),
                    ),
                )
            )

            results = await fetch_screenshots(url)

        assert set(results.keys()) == {"mobile", "ipad", "desktop"}
        for vp_label, res in results.items():
            assert res.provider == "microlink"
            assert res.cached is False
            assert vp_label in res.url

    @pytest.mark.asyncio
    async def test_falls_back_to_apiflash_when_microlink_fails(
        self, monkeypatch, cache_store,
    ):
        monkeypatch.setenv("APIFLASH_ACCESS_KEY", "test-apiflash-key")
        url = "https://example.com/landing"
        with respx.mock(assert_all_called=False) as router:
            # Microlink always returns "fail" status.
            router.get("https://api.microlink.io/").mock(
                return_value=httpx.Response(200, json=_microlink_failure_payload())
            )
            # ApiFlash succeeds for every viewport.
            router.get("https://api.apiflash.com/v1/urltoimage").mock(
                side_effect=lambda req: httpx.Response(
                    200,
                    json={"url": f"https://cdn.apiflash.com/{req.url.params['width']}.png"},
                )
            )

            results = await fetch_screenshots(url)

        for res in results.values():
            assert res.provider == "apiflash"
            assert "apiflash.com" in res.url

    @pytest.mark.asyncio
    async def test_falls_back_to_screenshotmachine_when_microlink_and_apiflash_fail(
        self, monkeypatch, cache_store,
    ):
        # ApiFlash key absent → that provider returns None without HTTP.
        # ScreenshotMachine key present → it returns a constructed URL.
        monkeypatch.setenv("SCREENSHOTMACHINE_KEY", "sm-test-key")
        url = "https://example.com/landing"
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.microlink.io/").mock(
                return_value=httpx.Response(200, json=_microlink_failure_payload())
            )
            # No apiflash route — env var is unset so it returns None pre-HTTP.

            results = await fetch_screenshots(url)

        for res in results.values():
            assert res.provider == "screenshotmachine"
            assert "screenshotmachine.com" in res.url
            assert "sm-test-key" in res.url

    @pytest.mark.asyncio
    async def test_all_providers_fail_raises_screenshot_error(self, cache_store):
        url = "https://example.com/landing"
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.microlink.io/").mock(
                return_value=httpx.Response(200, json=_microlink_failure_payload())
            )
            # No paid-provider keys set → those return None without HTTP.

            with pytest.raises(ScreenshotError, match="All providers failed"):
                await fetch_screenshots(url)

    @pytest.mark.asyncio
    async def test_cache_hit_returns_results_without_any_http(self, cache_store):
        url = "https://example.com/cached"
        # Prime the cache with a fake successful fetch.
        cache_store[_url_hash(url)] = {
            "mobile_url":       "https://cdn.example.com/m.png",
            "ipad_url":         "https://cdn.example.com/i.png",
            "desktop_url":      "https://cdn.example.com/d.png",
            "mobile_provider":  "microlink",
            "ipad_provider":    "microlink",
            "desktop_provider": "microlink",
            "fetched_at":       __import__("time").time(),
        }

        # respx with assert_all_mocked=True will FAIL if any HTTP slips through.
        with respx.mock(assert_all_mocked=True, assert_all_called=False):
            results = await fetch_screenshots(url)

        assert results["mobile"].url == "https://cdn.example.com/m.png"
        assert results["mobile"].cached is True
        assert results["ipad"].cached is True
        assert results["desktop"].cached is True

    @pytest.mark.asyncio
    async def test_expired_cache_triggers_fresh_fetch(self, cache_store):
        url = "https://example.com/stale"
        # Cache row from 25h ago (> 24h window).
        cache_store[_url_hash(url)] = {
            "mobile_url": "stale", "ipad_url": "stale", "desktop_url": "stale",
            "mobile_provider": "microlink", "ipad_provider": "microlink", "desktop_provider": "microlink",
            "fetched_at": __import__("time").time() - 90000,  # 25h ago
        }
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.microlink.io/").mock(
                side_effect=lambda req: httpx.Response(
                    200,
                    json=_microlink_success_payload(
                        url,
                        next(
                            vp for vp in Viewport
                            if str(vp.width) == req.url.params["viewport.width"]
                        ),
                    ),
                )
            )

            results = await fetch_screenshots(url)

        # Fresh fetch → cached=False, URL is the freshly-built CDN URL.
        for res in results.values():
            assert res.cached is False
            assert "cdn.microlink.io" in res.url

    @pytest.mark.asyncio
    async def test_validate_url_runs_before_any_http(self, cache_store):
        """Bad scheme → ValueError before any HTTP / cache lookup."""
        with respx.mock(assert_all_mocked=True, assert_all_called=False):
            with pytest.raises(ValueError):
                await fetch_screenshots("file:///etc/passwd")


# ---------------------------------------------------------------------------
# fetch_url_screenshots — the MCP tool wrapper. Shape + error wiring.
# ---------------------------------------------------------------------------

class TestFetchUrlScreenshotsTool:
    @pytest.mark.asyncio
    async def test_tool_returns_expected_dict_shape(self, monkeypatch, cache_store):
        from design_mcp.server import fetch_url_screenshots

        # Stub the auth context so resolve_user_email() doesn't hit DB.
        monkeypatch.setattr(
            "design_mcp.server.resolve_user_email",
            lambda: "test@example.com",
        )

        url = "https://example.com/landing"
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.microlink.io/").mock(
                side_effect=lambda req: httpx.Response(
                    200,
                    json=_microlink_success_payload(
                        url,
                        next(
                            vp for vp in Viewport
                            if str(vp.width) == req.url.params["viewport.width"]
                        ),
                    ),
                )
            )

            result = await fetch_url_screenshots(url)

        assert result["url"] == url
        assert result["cached"] is False
        for key, expected_dim in (
            ("mobile",  "390x844"),
            ("ipad",    "820x1180"),
            ("desktop", "1440x900"),
        ):
            assert key in result
            assert result[key]["viewport"] == expected_dim
            assert result[key]["provider"] == "microlink"
            assert result[key]["url"].startswith("https://")

    @pytest.mark.asyncio
    async def test_tool_raises_value_error_on_bad_url(self, monkeypatch, cache_store):
        from design_mcp.server import fetch_url_screenshots
        monkeypatch.setattr(
            "design_mcp.server.resolve_user_email",
            lambda: "test@example.com",
        )
        with pytest.raises(ValueError, match="URL invalid"):
            await fetch_url_screenshots("http://localhost/admin")

    @pytest.mark.asyncio
    async def test_tool_raises_runtime_error_when_all_providers_fail(
        self, monkeypatch, cache_store,
    ):
        from design_mcp.server import fetch_url_screenshots
        monkeypatch.setattr(
            "design_mcp.server.resolve_user_email",
            lambda: "test@example.com",
        )
        url = "https://example.com/landing"
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.microlink.io/").mock(
                return_value=httpx.Response(200, json=_microlink_failure_payload())
            )
            with pytest.raises(RuntimeError, match="All screenshot providers failed"):
                await fetch_url_screenshots(url)


# ---------------------------------------------------------------------------
# Concurrency — the orchestrator gathers all 3 viewports in parallel.
# ---------------------------------------------------------------------------

class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_three_viewports_run_concurrently(self, monkeypatch, cache_store):
        """Each viewport's provider call simulates a 100ms latency.
        Serial = 300ms+, parallel = ~100ms. We assert wall-clock <= 250ms.
        """
        import time

        async def slow_microlink(url, viewport, client):  # noqa: ARG001
            await asyncio.sleep(0.1)
            return f"https://cdn.example.com/{viewport.label}.png"

        # Patch only the microlink adapter — keep the orchestrator as-is.
        monkeypatch.setattr(scr, "_PROVIDERS", [("microlink", slow_microlink)])

        t0 = time.monotonic()
        results = await fetch_screenshots("https://example.com/parallel")
        elapsed = time.monotonic() - t0
        assert elapsed < 0.25, f"viewports didn't run in parallel (elapsed={elapsed:.3f}s)"
        assert len(results) == 3
