"""Tests for design_mcp.images — Pexels stock photo + Iconify icon sourcing.

Coverage:
- search_stock_images: shape, error on missing key, error on bad orientation,
  HTTP failure surfaces as ImagesError, attribution_note always present.
- fetch_icons: returns slot -> {icon_id, svg}, search + SVG fetched per slot,
  malformed slot keeps the response shape (svg=None + error string).
- search_icons: returns a list of {icon_id, preview_url, svg}.
- The new IMAGE & ICON RULES block lands in the rendered landing brief.
- The new images_choice field is in the landing-page clarifying field list at
  the correct position, with the three documented options verbatim.

Pexels + Iconify HTTP is mocked via respx so no live network is required.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import httpx
import pytest
import respx

# DB import side effect — same trick as test_screenshots.py.
os.environ.setdefault("TOKEN_DB_PASSWORD", "test-only-not-used")

from mcp.server.auth.middleware.auth_context import (  # noqa: E402
    auth_context_var,
)
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402

from design_mcp import drafts  # noqa: E402
from design_mcp import images as images_mod  # noqa: E402
from design_mcp.images import (  # noqa: E402
    ImagesError,
    fetch_icons,
    search_icons,
    search_stock_images,
)


# ---------------------------------------------------------------------------
# Auth-context helpers — mirrors tests/test_day3_refactor.py so we don't
# need a live PG / OAuth path to run the brief-rendering tests.
# ---------------------------------------------------------------------------

_DEFAULT_USER = "sheetal@acquirely.com.au"


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
def _default_user_context():
    """Run every test in this module inside the default auth context so the
    server-level tools (start_landing_page_intake) don't bail out on missing
    bearer-token auth."""
    drafts._reset_for_tests()
    with _set_user(_DEFAULT_USER):
        yield
    drafts._reset_for_tests()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pexels_key(monkeypatch):
    """Set the PEXELS_API_KEY so search_stock_images doesn't bail out early."""
    monkeypatch.setenv("PEXELS_API_KEY", "test-pexels-key")


def _pexels_payload(query: str, n: int = 6) -> dict[str, Any]:
    """Mimic a real Pexels /v1/search response — minus fields we ignore."""
    photos = []
    for i in range(n):
        photos.append(
            {
                "id": 1000 + i,
                "src": {
                    "large": f"https://images.pexels.com/photos/{1000 + i}/large.jpg",
                    "medium": f"https://images.pexels.com/photos/{1000 + i}/medium.jpg",
                    "small": f"https://images.pexels.com/photos/{1000 + i}/small.jpg",
                },
                "photographer": f"Photographer {i}",
                "photographer_url": f"https://pexels.com/@photo-{i}",
                "alt": f"{query} sample photo {i}",
                "url": f"https://pexels.com/photo/{1000 + i}",
            }
        )
    return {"photos": photos, "total_results": n}


def _iconify_search_payload(icons: list[str]) -> dict[str, Any]:
    return {"icons": icons, "total": len(icons)}


_DUMMY_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
    'viewBox="0 0 24 24" fill="none" stroke="#0F2A4A" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>'
    "</svg>"
)


# ---------------------------------------------------------------------------
# search_stock_images
# ---------------------------------------------------------------------------

class TestSearchStockImages:
    def test_returns_six_results_with_expected_shape(self, pexels_key):
        query = "lead generation marketing"
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.pexels.com/v1/search").mock(
                return_value=httpx.Response(200, json=_pexels_payload(query, n=6))
            )
            out = search_stock_images(query=query, count=6, orientation="landscape")
        assert out["query"] == query
        assert len(out["results"]) == 6
        first = out["results"][0]
        for key in (
            "id", "url_large", "url_medium", "photographer",
            "photographer_url", "alt", "source",
        ):
            assert key in first, f"missing key {key!r}"
        # URLs point at the real Pexels CDN (the actual prod domain).
        assert first["url_large"].startswith("https://images.pexels.com/")
        # Attribution note is always present.
        assert "Pexels" in out["attribution_note"]

    def test_clamps_count_to_pexels_max(self, pexels_key):
        captured: dict[str, Any] = {}

        def _capture(req: httpx.Request) -> httpx.Response:
            captured["per_page"] = req.url.params.get("per_page")
            return httpx.Response(200, json=_pexels_payload("x", n=15))

        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.pexels.com/v1/search").mock(side_effect=_capture)
            search_stock_images(query="x", count=999)
        # 999 should clamp to 15 (the documented per-page max we expose).
        assert captured["per_page"] == "15"

    def test_uses_authorization_header_without_bearer_prefix(self, pexels_key):
        captured: dict[str, Any] = {}

        def _capture(req: httpx.Request) -> httpx.Response:
            captured["authz"] = req.headers.get("authorization")
            return httpx.Response(200, json=_pexels_payload("x"))

        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.pexels.com/v1/search").mock(side_effect=_capture)
            search_stock_images(query="x")
        # Pexels rejects "Bearer ..." — the raw key must be passed verbatim.
        assert captured["authz"] == "test-pexels-key"

    def test_missing_key_raises_images_error(self, monkeypatch):
        monkeypatch.delenv("PEXELS_API_KEY", raising=False)
        with pytest.raises(ImagesError, match="PEXELS_API_KEY"):
            search_stock_images(query="x")

    def test_empty_query_raises(self, pexels_key):
        with pytest.raises(ImagesError, match="query"):
            search_stock_images(query="")

    def test_bad_orientation_raises(self, pexels_key):
        with pytest.raises(ImagesError, match="orientation"):
            search_stock_images(query="x", orientation="diagonal")

    def test_http_failure_raises_images_error(self, pexels_key):
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.pexels.com/v1/search").mock(
                return_value=httpx.Response(500, text="boom")
            )
            with pytest.raises(ImagesError, match="Pexels API request failed"):
                search_stock_images(query="x")

    def test_empty_results_still_returns_attribution_note(self, pexels_key):
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.pexels.com/v1/search").mock(
                return_value=httpx.Response(200, json={"photos": []})
            )
            out = search_stock_images(query="x")
        assert out["results"] == []
        assert "Pexels" in out["attribution_note"]


# ---------------------------------------------------------------------------
# fetch_icons
# ---------------------------------------------------------------------------

class TestFetchIcons:
    def test_returns_svg_for_each_slot(self):
        slots = {
            "hero_badge": "verified secure",
            "feature_1": "fast delivery",
        }
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.iconify.design/search").mock(
                side_effect=lambda req: httpx.Response(
                    200,
                    json=_iconify_search_payload(
                        ["lucide:shield-check", "lucide:check-circle"]
                    ),
                )
            )
            router.get(
                url__regex=r"https://api\.iconify\.design/lucide:[^/]+\.svg",
            ).mock(return_value=httpx.Response(200, text=_DUMMY_SVG))

            out = fetch_icons(slots, color="#0F2A4A", size=48)

        assert set(out["icons"].keys()) == {"hero_badge", "feature_1"}
        for slot_name, entry in out["icons"].items():
            assert entry["icon_id"] is not None
            assert entry["icon_id"].startswith("lucide:")
            assert entry["svg"].startswith("<svg")

    def test_empty_search_records_error_but_keeps_shape(self):
        """If a slot's keyword returns zero icons, surface an error string so
        the caller can decide to fall back rather than crash on KeyError."""
        slots = {"orphan_slot": "this matches nothing"}
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.iconify.design/search").mock(
                return_value=httpx.Response(
                    200, json=_iconify_search_payload([])
                )
            )
            out = fetch_icons(slots, color="#0F2A4A", size=48)
        assert "orphan_slot" in out["icons"]
        entry = out["icons"]["orphan_slot"]
        assert entry["svg"] is None
        assert entry["icon_id"] is None
        assert "no Lucide icons" in entry["error"]

    def test_bakes_color_into_request(self):
        captured: dict[str, Any] = {}

        def _capture_svg(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, text=_DUMMY_SVG)

        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.iconify.design/search").mock(
                return_value=httpx.Response(
                    200,
                    json=_iconify_search_payload(["lucide:shield-check"]),
                )
            )
            router.get(
                url__regex=r"https://api\.iconify\.design/lucide:[^/]+\.svg.*",
            ).mock(side_effect=_capture_svg)
            fetch_icons({"slot": "shield"}, color="#0F2A4A", size=48)

        # Iconify expects `color=%230F2A4A` — the leading # is URL-encoded.
        assert "color=%230F2A4A" in captured["url"]
        assert "width=48" in captured["url"]

    def test_bad_color_raises(self):
        with pytest.raises(ImagesError, match="color"):
            fetch_icons({"slot": "shield"}, color="not-a-hex", size=48)

    def test_empty_slots_raises(self):
        with pytest.raises(ImagesError, match="slots"):
            fetch_icons({}, color="#0F2A4A", size=48)


# ---------------------------------------------------------------------------
# search_icons
# ---------------------------------------------------------------------------

class TestSearchIcons:
    def test_returns_candidates_with_svg_and_preview(self):
        icons = [
            "lucide:shield-check",
            "lucide:check-circle",
            "lucide:badge-check",
        ]
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.iconify.design/search").mock(
                return_value=httpx.Response(
                    200, json=_iconify_search_payload(icons)
                )
            )
            router.get(
                url__regex=r"https://api\.iconify\.design/lucide:[^/]+\.svg.*",
            ).mock(return_value=httpx.Response(200, text=_DUMMY_SVG))
            out = search_icons(query="verified", count=3)

        assert len(out["results"]) == 3
        for entry in out["results"]:
            assert entry["icon_id"].startswith("lucide:")
            assert entry["preview_url"].startswith("https://api.iconify.design/")
            assert entry["svg"].startswith("<svg")

    def test_clamps_count(self):
        captured: dict[str, Any] = {}

        def _capture(req: httpx.Request) -> httpx.Response:
            captured["limit"] = req.url.params.get("limit")
            return httpx.Response(
                200, json=_iconify_search_payload([])
            )

        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.iconify.design/search").mock(side_effect=_capture)
            search_icons(query="x", count=999)
        assert captured["limit"] == "20"

    def test_empty_query_raises(self):
        with pytest.raises(ImagesError, match="query"):
            search_icons(query="")


# ---------------------------------------------------------------------------
# Brief template — IMAGE & ICON RULES block + images_choice flow steering.
# ---------------------------------------------------------------------------

class TestBriefRendersImageRules:
    def _brief(self) -> str:
        from design_mcp.server import start_landing_page_intake
        return start_landing_page_intake(brief="anything")["instructions"]

    def test_image_icon_rules_block_present(self):
        text = self._brief()
        # The block header is uppercase so it's hard to miss in a long brief.
        assert "IMAGE & ICON RULES" in text
        assert "NEVER FABRICATE" in text
        # Names all three new tools so Claude knows what to call.
        assert "fetch_icons" in text
        assert "search_icons" in text
        assert "search_stock_images" in text

    def test_forbids_inline_svg_and_fabricated_photo_urls(self):
        text = self._brief()
        # The two prod failures we're trying to prevent.
        assert "NEVER write inline" in text and "<svg>" in text
        assert "NEVER fabricate" in text and "Pexels" in text and "Unsplash" in text

    def test_describes_three_images_choice_branches(self):
        text = self._brief()
        # All three images_choice option strings appear verbatim — so the
        # rules block can document the per-branch flow without paraphrasing.
        assert "Yes — I'll paste image URLs in chat now" in text
        assert "Yes — search free Pexels stock photos for me" in text
        assert "No — clean modern look with icons + gradients only" in text

    def test_pexels_attribution_documented(self):
        text = self._brief()
        # Footer fine-print is the path of least resistance for compliance.
        assert "Photos via" in text or "photographer" in text.lower()


# ---------------------------------------------------------------------------
# Landing-page field list — images_choice position + option set.
# ---------------------------------------------------------------------------

class TestImagesChoiceField:
    def test_field_at_index_4(self):
        from design_mcp.generators.landing_page import _CLARIFYING_FIELDS
        assert _CLARIFYING_FIELDS[4].key == "images_choice"

    def test_field_options_verbatim_and_in_order(self):
        from design_mcp.generators.landing_page import _CLARIFYING_FIELDS
        opts = _CLARIFYING_FIELDS[4].suggested_options
        assert opts == (
            "Yes — I'll paste image URLs in chat now",
            "Yes — search free Pexels stock photos for me",
            "No — clean modern look with icons + gradients only",
        )

    def test_review_checkpoint_shifted_to_index_5(self):
        from design_mcp.generators.landing_page import _CLARIFYING_FIELDS
        assert _CLARIFYING_FIELDS[5].key == "review_checkpoint"
        assert _CLARIFYING_FIELDS[5].is_checkpoint is True

    def test_total_field_count_is_twelve(self):
        from design_mcp.generators.landing_page import _CLARIFYING_FIELDS
        assert len(_CLARIFYING_FIELDS) == 12
