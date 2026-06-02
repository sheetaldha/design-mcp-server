"""Multi-provider URL → screenshot orchestrator.

Microlink is the default (no API key required, 50 req/day free).
ApiFlash + ScreenshotMachine are fallbacks IF the respective env vars
are set (APIFLASH_ACCESS_KEY, SCREENSHOTMACHINE_KEY). They're tried in
order; first one to return a valid image URL wins.

Results are cached per-URL for 24 hours in the design_mcp_screenshot_cache
table so a repeat fetch of the same URL inside the cache window does no
provider HTTP at all.

URLs are validated up-front with an SSRF guard (no localhost / loopback /
private / link-local IPs, and only http/https schemes).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


class Viewport(Enum):
    MOBILE = ("mobile", 390, 844)
    IPAD = ("ipad", 820, 1180)
    DESKTOP = ("desktop", 1440, 900)

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def width(self) -> int:
        return self.value[1]

    @property
    def height(self) -> int:
        return self.value[2]


@dataclass(frozen=True)
class ScreenshotResult:
    viewport: str    # 'mobile' / 'ipad' / 'desktop'
    url: str         # the screenshot URL (provider's CDN)
    provider: str    # which provider succeeded
    cached: bool     # True if returned from the local 24h cache


class ScreenshotError(Exception):
    """Raised when all configured providers fail for a given URL+viewport."""


# ---------------------------------------------------------------------------
# URL validation (SSRF protection)
# ---------------------------------------------------------------------------

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "169.254.169.254", "0.0.0.0"}


def validate_url(url: str) -> str:
    """Return the URL unchanged if safe; raise ValueError otherwise.

    SSRF guard — blocks anything that could let the screenshot provider be
    coerced into hitting our internal network on the user's behalf:
      - non-http(s) schemes (file:, gopher:, javascript:, etc.)
      - bare loopback / metadata-service hostnames
      - hostnames that resolve to a private / loopback / link-local IP
      - hostnames that don't resolve at all
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme must be http or https, got: {parsed.scheme!r}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL has no host component")
    if host in _BLOCKED_HOSTS:
        raise ValueError(f"URL host {host!r} is blocked")
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except socket.gaierror as exc:
        raise ValueError(f"URL host {host!r} does not resolve") from exc
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        raise ValueError(f"URL resolves to private/internal IP: {ip}")
    return url


# ---------------------------------------------------------------------------
# Provider adapters — each returns the screenshot URL or None on failure.
# ---------------------------------------------------------------------------

async def microlink_screenshot(
    url: str,
    viewport: Viewport,
    client: httpx.AsyncClient,
) -> Optional[str]:
    """Microlink — no API key, 50/day free. Returns screenshot URL or None."""
    try:
        resp = await client.get(
            "https://api.microlink.io/",
            params={
                "url": url,
                "screenshot": "true",
                "meta": "false",
                "viewport.width": viewport.width,
                "viewport.height": viewport.height,
                "fullPage": "true",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return data["data"]["screenshot"]["url"]
        return None
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("microlink screenshot failed for %s @ %s: %s", url, viewport.label, exc)
        return None


async def apiflash_screenshot(
    url: str,
    viewport: Viewport,
    client: httpx.AsyncClient,
) -> Optional[str]:
    """ApiFlash — needs APIFLASH_ACCESS_KEY env var. 100/month free."""
    key = os.getenv("APIFLASH_ACCESS_KEY")
    if not key:
        return None
    try:
        resp = await client.get(
            "https://api.apiflash.com/v1/urltoimage",
            params={
                "access_key": key,
                "url": url,
                "width": viewport.width,
                "height": viewport.height,
                "full_page": "true",
                "format": "png",
                "response_type": "json",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("url")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("apiflash screenshot failed for %s @ %s: %s", url, viewport.label, exc)
        return None


async def screenshotmachine_screenshot(
    url: str,
    viewport: Viewport,
    client: httpx.AsyncClient,  # noqa: ARG001 — kept for signature symmetry
) -> Optional[str]:
    """ScreenshotMachine — needs SCREENSHOTMACHINE_KEY env var. 100/month free.

    ScreenshotMachine returns the image bytes directly at the GET URL; there
    is no JSON metadata endpoint. We construct the signed URL and return it
    as-is — the client downloads / displays it.
    """
    key = os.getenv("SCREENSHOTMACHINE_KEY")
    if not key:
        return None
    return (
        f"https://api.screenshotmachine.com/?key={key}"
        f"&url={url}&dimension={viewport.width}x{viewport.height}"
        f"&format=png&device=desktop&zoom=100"
    )


_PROVIDERS = [
    ("microlink", microlink_screenshot),
    ("apiflash", apiflash_screenshot),
    ("screenshotmachine", screenshotmachine_screenshot),
]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def fetch_screenshots(url: str) -> dict[str, ScreenshotResult]:
    """Return a dict mapping viewport.label → ScreenshotResult for each of the 3 viewports.

    Tries providers in order per viewport. Raises ScreenshotError if all
    providers fail for any viewport. Raises ValueError if the URL is unsafe.
    Checks the 24h PG cache before doing any HTTP.
    """
    validate_url(url)

    cached = _cache_get(url)
    if cached is not None:
        return cached

    async with httpx.AsyncClient() as client:
        async def one_viewport(vp: Viewport) -> ScreenshotResult:
            for provider_name, fn in _PROVIDERS:
                result_url = await fn(url, vp, client)
                if result_url:
                    return ScreenshotResult(
                        viewport=vp.label,
                        url=result_url,
                        provider=provider_name,
                        cached=False,
                    )
            raise ScreenshotError(
                f"All providers failed for {vp.label} viewport of {url}"
            )

        results = await asyncio.gather(*[one_viewport(vp) for vp in Viewport])

    result_dict = {r.viewport: r for r in results}
    _cache_put(url, result_dict)
    return result_dict


# ---------------------------------------------------------------------------
# 24h PG-backed cache for cross-restart persistence
# ---------------------------------------------------------------------------

def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_get(url: str) -> Optional[dict[str, ScreenshotResult]]:
    """Return cached results if within 24h, else None."""
    from . import db
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT mobile_url, ipad_url, desktop_url,
                   mobile_provider, ipad_provider, desktop_provider
              FROM design_mcp_screenshot_cache
             WHERE url_hash = %s
               AND fetched_at > now() - interval '24 hours'
            """,
            (_url_hash(url),),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "mobile":  ScreenshotResult("mobile",  row["mobile_url"],  row["mobile_provider"],  cached=True),
        "ipad":    ScreenshotResult("ipad",    row["ipad_url"],    row["ipad_provider"],    cached=True),
        "desktop": ScreenshotResult("desktop", row["desktop_url"], row["desktop_provider"], cached=True),
    }


def _cache_put(url: str, results: dict[str, ScreenshotResult]) -> None:
    from . import db
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO design_mcp_screenshot_cache
                (url_hash, url,
                 mobile_url, ipad_url, desktop_url,
                 mobile_provider, ipad_provider, desktop_provider,
                 fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (url_hash) DO UPDATE SET
                mobile_url       = EXCLUDED.mobile_url,
                ipad_url         = EXCLUDED.ipad_url,
                desktop_url      = EXCLUDED.desktop_url,
                mobile_provider  = EXCLUDED.mobile_provider,
                ipad_provider    = EXCLUDED.ipad_provider,
                desktop_provider = EXCLUDED.desktop_provider,
                fetched_at       = now()
            """,
            (
                _url_hash(url), url,
                results["mobile"].url, results["ipad"].url, results["desktop"].url,
                results["mobile"].provider, results["ipad"].provider, results["desktop"].provider,
            ),
        )
        # commit happens in the get_conn context manager exit
