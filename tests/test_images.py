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


@pytest.fixture
def unsplash_key(monkeypatch):
    """Set the UNSPLASH_ACCESS_KEY so the Unsplash branch doesn't bail out."""
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "test-unsplash-key")


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


def _unsplash_payload(query: str, n: int = 6) -> dict[str, Any]:
    """Mimic a real Unsplash /search/photos response — minus fields we ignore."""
    results = []
    for i in range(n):
        results.append(
            {
                "id": f"unsplash-{2000 + i}",
                "alt_description": f"{query} unsplash sample {i}",
                "urls": {
                    "regular": f"https://images.unsplash.com/photo-{2000 + i}?w=1080",
                    "small": f"https://images.unsplash.com/photo-{2000 + i}?w=400",
                },
                "user": {
                    "name": f"Unsplash Shooter {i}",
                    "links": {"html": f"https://unsplash.com/@shooter-{i}"},
                },
                "links": {"html": f"https://unsplash.com/photos/{2000 + i}"},
            }
        )
    return {"results": results, "total": n, "total_pages": 1}


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
# search_stock_images — Unsplash provider + source="both"
# ---------------------------------------------------------------------------

class TestUnsplashProvider:
    def test_unsplash_returns_normalized_shape(self, unsplash_key):
        query = "solar panels rooftop"
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.unsplash.com/search/photos").mock(
                return_value=httpx.Response(200, json=_unsplash_payload(query, n=6))
            )
            out = search_stock_images(
                query=query, count=6, orientation="landscape", source="unsplash"
            )
        assert out["query"] == query
        assert out["source"] == "unsplash"
        assert len(out["results"]) == 6
        first = out["results"][0]
        # EXACT same keys Pexels returns, plus the provider tag.
        for key in (
            "id", "url_large", "url_medium", "photographer",
            "photographer_url", "alt", "source", "provider",
        ):
            assert key in first, f"missing key {key!r}"
        assert first["provider"] == "unsplash"
        # urls.regular -> url_large, urls.small -> url_medium.
        assert first["url_large"].startswith("https://images.unsplash.com/")
        assert "w=1080" in first["url_large"]
        assert "w=400" in first["url_medium"]
        # user.name -> photographer; user.links.html -> photographer_url (UTM-tagged).
        assert first["photographer"] == "Unsplash Shooter 0"
        assert first["photographer_url"].startswith("https://unsplash.com/@shooter-0")
        assert "utm_source=design_mcp" in first["photographer_url"]
        assert "utm_medium=referral" in first["photographer_url"]
        # photo links.html -> source, also UTM-tagged.
        assert first["source"].startswith("https://unsplash.com/photos/")
        assert "utm_source=design_mcp" in first["source"]
        # Attribution note names Unsplash + the required UTM credit.
        assert "Unsplash" in out["attribution_note"]
        assert "utm_source=design_mcp" in out["attribution_note"]

    def test_unsplash_uses_client_id_auth_header(self, unsplash_key):
        captured: dict[str, Any] = {}

        def _capture(req: httpx.Request) -> httpx.Response:
            captured["authz"] = req.headers.get("authorization")
            return httpx.Response(200, json=_unsplash_payload("x"))

        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.unsplash.com/search/photos").mock(
                side_effect=_capture
            )
            search_stock_images(query="x", source="unsplash")
        # Unsplash requires "Client-ID <key>", NOT a bare key or "Bearer".
        assert captured["authz"] == "Client-ID test-unsplash-key"

    def test_unsplash_maps_square_to_squarish(self, unsplash_key):
        captured: dict[str, Any] = {}

        def _capture(req: httpx.Request) -> httpx.Response:
            captured["orientation"] = req.url.params.get("orientation")
            return httpx.Response(200, json=_unsplash_payload("x"))

        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.unsplash.com/search/photos").mock(
                side_effect=_capture
            )
            search_stock_images(query="x", source="unsplash", orientation="square")
        # Unsplash's enum spells square as "squarish".
        assert captured["orientation"] == "squarish"

    def test_unsplash_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
        with pytest.raises(ImagesError, match="UNSPLASH_ACCESS_KEY"):
            search_stock_images(query="x", source="unsplash")

    def test_unsplash_http_failure_raises(self, unsplash_key):
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.unsplash.com/search/photos").mock(
                return_value=httpx.Response(500, text="boom")
            )
            with pytest.raises(ImagesError, match="Unsplash API request failed"):
                search_stock_images(query="x", source="unsplash")

    def test_bad_source_raises(self, pexels_key):
        with pytest.raises(ImagesError, match="source"):
            search_stock_images(query="x", source="flickr")

    def test_default_source_is_pexels(self, pexels_key):
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.pexels.com/v1/search").mock(
                return_value=httpx.Response(200, json=_pexels_payload("x", n=3))
            )
            out = search_stock_images(query="x")
        assert out["source"] == "pexels"
        assert all(r["provider"] == "pexels" for r in out["results"])

    def test_source_both_interleaves_and_caps(self, pexels_key, unsplash_key):
        query = "lead generation"
        with respx.mock(assert_all_called=False) as router:
            router.get("https://api.pexels.com/v1/search").mock(
                return_value=httpx.Response(200, json=_pexels_payload(query, n=6))
            )
            router.get("https://api.unsplash.com/search/photos").mock(
                return_value=httpx.Response(200, json=_unsplash_payload(query, n=6))
            )
            out = search_stock_images(query=query, count=6, source="both")
        assert out["source"] == "both"
        # Capped at requested count.
        assert len(out["results"]) == 6
        providers = {r["provider"] for r in out["results"]}
        # Both providers represented in the interleave.
        assert providers == {"pexels", "unsplash"}
        # Interleave order: pexels first, then unsplash, alternating.
        assert out["results"][0]["provider"] == "pexels"
        assert out["results"][1]["provider"] == "unsplash"
        # attribution_note is per-source when source="both".
        assert isinstance(out["attribution_note"], dict)
        assert "Pexels" in out["attribution_note"]["pexels"]
        assert "Unsplash" in out["attribution_note"]["unsplash"]


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
        assert "Yes — search free stock photos (Pexels + Unsplash) for me" in text
        assert "No — clean modern look with icons + gradients only" in text

    def test_pexels_attribution_documented(self):
        text = self._brief()
        # Footer fine-print is the path of least resistance for compliance.
        assert "Photos via" in text or "photographer" in text.lower()

    def test_stock_branch_shows_inline_gallery_before_asking(self):
        text = self._brief()
        # AskUserQuestion options are text-only — the brief MUST tell Claude to
        # render the candidates as an inline markdown-image gallery FIRST so the
        # user actually sees the photos before choosing.
        assert "INLINE NUMBERED MARKDOWN-IMAGE GALLERY" in text
        # Uses url_medium (the ~350px thumbnail) as the inline preview src.
        assert "url_medium" in text
        # Example markdown-image form is spelled out verbatim.
        assert "![Photo by {photographer}]({url_medium})" in text


# ---------------------------------------------------------------------------
# Landing-page field list — images_choice position + option set.
# ---------------------------------------------------------------------------

class TestImagesChoiceField:
    def test_field_at_index_7(self):
        from design_mcp.generators.landing_page import _CLARIFYING_FIELDS
        assert _CLARIFYING_FIELDS[7].key == "images_choice"

    def test_field_options_verbatim_and_in_order(self):
        from design_mcp.generators.landing_page import _CLARIFYING_FIELDS
        opts = _CLARIFYING_FIELDS[7].suggested_options
        assert opts == (
            "Yes — I'll paste image URLs in chat now",
            "Yes — search free stock photos (Pexels + Unsplash) for me",
            "No — clean modern look with icons + gradients only",
        )

    def test_review_checkpoint_shifted_to_index_15(self):
        from design_mcp.generators.landing_page import _CLARIFYING_FIELDS
        assert _CLARIFYING_FIELDS[15].key == "review_checkpoint"
        assert _CLARIFYING_FIELDS[15].is_checkpoint is True

    def test_total_field_count_is_sixteen(self):
        from design_mcp.generators.landing_page import _CLARIFYING_FIELDS
        assert len(_CLARIFYING_FIELDS) == 16
