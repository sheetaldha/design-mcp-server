"""Image + icon sourcing for the design MCP.

Stops Claude from fabricating Unsplash / Pexels photo URLs (the prod issue:
hallucinated football-stadium photos for "lead generation") and from writing
inline `<svg>` markup for icons. The server controls both sources:

- ``search_stock_images`` → Pexels API (free tier 200 req/h, 20k/month).
  Returns 6 real candidates with verified CDN URLs + photographer credit so
  Claude can surface them as AskUserQuestion options.
- ``fetch_icons`` / ``search_icons`` → Iconify API (no auth). Returns real
  SVG markup pre-coloured / pre-sized so Claude embeds it verbatim instead of
  inventing icon shapes.

Both layers are HTTP wrappers; the MCP tools live in ``server.py`` and call
these helpers. Tests mock ``httpx`` directly via respx so no live HTTP runs.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pexels
# ---------------------------------------------------------------------------

_PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
_UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"
# Both providers accept the same three orientation values, but Unsplash spells
# "square" differently ("squarish"); we normalize on the wire in _unsplash_search.
_ALLOWED_ORIENTATIONS = {"landscape", "portrait", "square"}
# Back-compat alias — older code / tests referenced the Pexels-specific name.
_PEXELS_ALLOWED_ORIENTATIONS = _ALLOWED_ORIENTATIONS
_ALLOWED_SOURCES = {"pexels", "unsplash", "both"}

_PEXELS_ATTRIBUTION_NOTE = (
    "Pexels requires linking to Pexels.com somewhere on the live page; "
    "render in footer fine print."
)
# Unsplash's API guidelines require crediting the photographer + Unsplash with
# UTM-tagged links. The UTM params below are mandated by Unsplash.
_UNSPLASH_UTM = "?utm_source=design_mcp&utm_medium=referral"
_UNSPLASH_ATTRIBUTION_NOTE = (
    "Unsplash requires crediting the photographer AND Unsplash with UTM-tagged "
    'links: render `Photo by <a href="{photographer_url}">{photographer}</a> on '
    '<a href="https://unsplash.com' + _UNSPLASH_UTM + '">Unsplash</a>` near the '
    "image or in footer fine print. photographer_url + source already carry "
    "the UTM params."
)


class ImagesError(RuntimeError):
    """Raised when a Pexels / Unsplash / Iconify call fails or returns no usable data."""


def _pexels_key() -> str:
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        raise ImagesError(
            "PEXELS_API_KEY is not configured on the server. Tell Sheetal to "
            "add it to /home/ubuntu/design-mcp-server/.env and restart PM2."
        )
    return key


def _unsplash_key() -> str:
    key = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
    if not key:
        raise ImagesError(
            "UNSPLASH_ACCESS_KEY is not configured on the server. Tell Sheetal to "
            "add it to /home/ubuntu/design-mcp-server/.env and restart PM2."
        )
    return key


def _clamp_stock_count(count: int) -> int:
    """Clamp count into the shared stock-photo range (1-15 for our purposes)."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 6
    return max(1, min(15, n))


# Back-compat alias — older code / tests referenced the Pexels-specific name.
_clamp_pexels_count = _clamp_stock_count


def _pexels_search(query: str, n: int, orientation: str) -> list[dict[str, Any]]:
    """Call the Pexels search API and return normalized photo dicts."""
    key = _pexels_key()
    try:
        resp = httpx.get(
            _PEXELS_SEARCH_URL,
            params={
                "query": query,
                "per_page": n,
                "orientation": orientation,
            },
            headers={"Authorization": key},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        # Line-1100 logging idiom: log the actual exception + a short
        # actionable message so PM2 logs show what to fix.
        log.error(
            "search_stock_images: pexels HTTP failure for query=%r: %s",
            query, exc,
        )
        raise ImagesError(f"Pexels API request failed: {exc}") from exc
    except ValueError as exc:
        log.error(
            "search_stock_images: pexels returned non-JSON for query=%r: %s",
            query, exc,
        )
        raise ImagesError(f"Pexels API returned invalid JSON: {exc}") from exc

    photos = data.get("photos") or []
    results: list[dict[str, Any]] = []
    for photo in photos:
        try:
            src = photo.get("src") or {}
            results.append(
                {
                    "id": photo.get("id"),
                    "url_large": src.get("large"),
                    "url_medium": src.get("medium"),
                    "photographer": photo.get("photographer") or "Unknown",
                    "photographer_url": photo.get("photographer_url") or "",
                    "alt": (photo.get("alt") or "").strip() or query,
                    "source": photo.get("url") or "",
                    "provider": "pexels",
                }
            )
        except (AttributeError, TypeError) as exc:
            # Pexels payload is well-defined but be defensive — never let a
            # single malformed row poison the whole response.
            log.warning(
                "search_stock_images: skipping malformed pexels row for query=%r: %s",
                query, exc,
            )
            continue
    return results


def _unsplash_search(query: str, n: int, orientation: str) -> list[dict[str, Any]]:
    """Call the Unsplash search API and normalize into the Pexels dict shape.

    Unsplash uses ``Authorization: Client-ID <key>`` (not a bare key or Bearer),
    spells the square orientation ``squarish``, and nests the data under
    ``results[]``. Output rows match _pexels_search exactly, with UTM-tagged
    attribution links Unsplash's API guidelines require and a ``provider`` tag.
    """
    key = _unsplash_key()
    # Unsplash's orientation enum spells square as "squarish".
    uns_orientation = "squarish" if orientation == "square" else orientation
    try:
        resp = httpx.get(
            _UNSPLASH_SEARCH_URL,
            params={
                "query": query,
                "per_page": n,
                "orientation": uns_orientation,
            },
            headers={"Authorization": f"Client-ID {key}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        log.error(
            "search_stock_images: unsplash HTTP failure for query=%r: %s",
            query, exc,
        )
        raise ImagesError(f"Unsplash API request failed: {exc}") from exc
    except ValueError as exc:
        log.error(
            "search_stock_images: unsplash returned non-JSON for query=%r: %s",
            query, exc,
        )
        raise ImagesError(f"Unsplash API returned invalid JSON: {exc}") from exc

    photos = data.get("results") or []
    results: list[dict[str, Any]] = []
    for photo in photos:
        try:
            urls = photo.get("urls") or {}
            user = photo.get("user") or {}
            user_links = user.get("links") or {}
            photo_links = photo.get("links") or {}
            # Append UTM params Unsplash mandates to the author + photo links.
            author_html = (user_links.get("html") or "").strip()
            photographer_url = author_html + _UNSPLASH_UTM if author_html else ""
            photo_html = (photo_links.get("html") or "").strip()
            source = photo_html + _UNSPLASH_UTM if photo_html else ""
            results.append(
                {
                    "id": photo.get("id"),
                    "url_large": urls.get("regular"),
                    "url_medium": urls.get("small"),
                    "photographer": user.get("name") or "Unknown",
                    "photographer_url": photographer_url,
                    "alt": (photo.get("alt_description") or "").strip() or query,
                    "source": source,
                    "provider": "unsplash",
                }
            )
        except (AttributeError, TypeError) as exc:
            log.warning(
                "search_stock_images: skipping malformed unsplash row for query=%r: %s",
                query, exc,
            )
            continue
    return results


def _interleave_dedupe(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    cap: int,
) -> list[dict[str, Any]]:
    """Interleave two result lists (primary first), dedupe by url_large, cap total."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a, b in zip(primary, secondary):
        for row in (a, b):
            key = row.get("url_large") or ""
            if key and key not in seen:
                seen.add(key)
                out.append(row)
    # Drain whichever list was longer.
    for row in primary[len(secondary):] + secondary[len(primary):]:
        key = row.get("url_large") or ""
        if key and key not in seen:
            seen.add(key)
            out.append(row)
    return out[:cap]


def search_stock_images(
    query: str,
    count: int = 6,
    orientation: str = "landscape",
    source: str = "pexels",
) -> dict[str, Any]:
    """Search free stock photos. Returns the public payload shape.

    ``source`` selects the provider: ``"pexels"`` (default), ``"unsplash"``, or
    ``"both"`` (query each, interleave + dedupe, cap at ``count``). All providers
    return the SAME normalized row shape (id / url_large / url_medium /
    photographer / photographer_url / alt / source / provider).

    Raises ImagesError on bad args, missing key, transport failure, or non-JSON.
    The caller (the MCP tool) surfaces the error string back to Claude.
    """
    if not query or not query.strip():
        raise ImagesError("query is required (got empty string)")
    if orientation not in _ALLOWED_ORIENTATIONS:
        raise ImagesError(
            f"orientation must be one of {sorted(_ALLOWED_ORIENTATIONS)}, "
            f"got {orientation!r}"
        )
    if source not in _ALLOWED_SOURCES:
        raise ImagesError(
            f"source must be one of {sorted(_ALLOWED_SOURCES)}, got {source!r}"
        )

    q = query.strip()
    n = _clamp_stock_count(count)

    if source == "pexels":
        results = _pexels_search(q, n, orientation)
        note = _PEXELS_ATTRIBUTION_NOTE
    elif source == "unsplash":
        results = _unsplash_search(q, n, orientation)
        note = _UNSPLASH_ATTRIBUTION_NOTE
    else:  # both — query each, interleave + dedupe, cap at n
        pexels_rows = _pexels_search(q, n, orientation)
        unsplash_rows = _unsplash_search(q, n, orientation)
        results = _interleave_dedupe(pexels_rows, unsplash_rows, cap=n)
        note = {
            "pexels": _PEXELS_ATTRIBUTION_NOTE,
            "unsplash": _UNSPLASH_ATTRIBUTION_NOTE,
        }

    return {
        "query": q,
        "source": source,
        "results": results,
        "attribution_note": note,
    }


# ---------------------------------------------------------------------------
# Iconify (no auth required)
# ---------------------------------------------------------------------------

_ICONIFY_SEARCH_URL = "https://api.iconify.design/search"
_ICONIFY_SVG_URL_TMPL = "https://api.iconify.design/{icon_id}.svg"


def _clamp_icon_count(count: int, ceiling: int) -> int:
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 8
    return max(1, min(ceiling, n))


def _hex_for_iconify(color: str) -> str:
    """Return the URL-encoded form of `#RRGGBB` for Iconify's `color=` param.

    Iconify accepts `color=%23RRGGBB`. We accept either `#RRGGBB` or `RRGGBB`
    on the way in and always emit the percent-encoded form.
    """
    raw = (color or "").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if not raw:
        raise ImagesError("color is required (e.g. '#0F2A4A')")
    # Iconify accepts 3 / 6 / 8 hex digits. Validate loosely so we don't
    # double-URL-encode garbage. Letters can be either case.
    if not all(c in "0123456789abcdefABCDEF" for c in raw):
        raise ImagesError(f"color must be hex digits, got {color!r}")
    return f"%23{raw}"


def _iconify_search(query: str, count: int, prefix: str = "lucide") -> list[str]:
    """Search Iconify and return up to ``count`` icon_ids matching the prefix."""
    if not query or not query.strip():
        raise ImagesError("query is required (got empty string)")
    n = _clamp_icon_count(count, ceiling=20)
    try:
        resp = httpx.get(
            _ICONIFY_SEARCH_URL,
            params={
                "query": query.strip(),
                "limit": n,
                "prefixes": prefix,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        log.error(
            "iconify search HTTP failure for query=%r: %s", query, exc,
        )
        raise ImagesError(f"Iconify search failed: {exc}") from exc
    except ValueError as exc:
        log.error(
            "iconify search returned non-JSON for query=%r: %s", query, exc,
        )
        raise ImagesError(f"Iconify search returned invalid JSON: {exc}") from exc

    icons = data.get("icons") or []
    # Iconify returns `["lucide:shield-check", ...]`. Keep only well-formed
    # `prefix:name` strings; the search endpoint occasionally returns blank
    # entries when no matches are found.
    cleaned = [s for s in icons if isinstance(s, str) and ":" in s]
    return cleaned[:n]


def _iconify_svg(icon_id: str, color_hex: str, size: int) -> str:
    """Fetch the raw SVG markup for a single Iconify icon."""
    try:
        size_int = int(size)
    except (TypeError, ValueError):
        size_int = 48
    size_int = max(8, min(512, size_int))
    url = _ICONIFY_SVG_URL_TMPL.format(icon_id=icon_id)
    # We pass color as a raw query string segment because httpx would
    # percent-encode the leading `%23` again into `%2523` otherwise.
    full_url = f"{url}?width={size_int}&color={color_hex}"
    try:
        resp = httpx.get(full_url, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.error(
            "iconify SVG fetch failed for icon_id=%r: %s", icon_id, exc,
        )
        raise ImagesError(
            f"Iconify SVG fetch failed for {icon_id}: {exc}"
        ) from exc
    text = resp.text.strip()
    if not text.startswith("<svg"):
        # Iconify returns a 404 page or a 1x1 transparent placeholder for
        # unknown ids; surface a clear error rather than embedding garbage.
        log.error(
            "iconify SVG response not <svg> markup for icon_id=%r (got %r)",
            icon_id, text[:80],
        )
        raise ImagesError(
            f"Iconify did not return SVG markup for {icon_id}"
        )
    return text


def fetch_icons(
    slots: dict[str, str],
    color: str = "#0F2A4A",
    size: int = 48,
) -> dict[str, Any]:
    """Resolve a dict of slot→keyword into slot→{icon_id, svg}.

    Iconify is called twice per slot: once for `_iconify_search` (top match)
    and once for `_iconify_svg` (the actual markup with color + size baked
    in). Slots that fail to resolve are returned with `svg=null` and an
    `error` string so the caller can decide whether to retry or fall back.
    """
    if not isinstance(slots, dict) or not slots:
        raise ImagesError("slots must be a non-empty dict of slot_name -> keyword")

    color_hex = _hex_for_iconify(color)
    out: dict[str, dict[str, Any]] = {}
    for slot_name, keyword in slots.items():
        try:
            matches = _iconify_search(keyword, count=1, prefix="lucide")
            if not matches:
                raise ImagesError(
                    f"no Lucide icons matched keyword {keyword!r}"
                )
            icon_id = matches[0]
            svg = _iconify_svg(icon_id, color_hex, size)
            out[slot_name] = {"icon_id": icon_id, "svg": svg}
        except ImagesError as exc:
            log.warning(
                "fetch_icons: slot=%r keyword=%r failed: %s",
                slot_name, keyword, exc,
            )
            out[slot_name] = {
                "icon_id": None,
                "svg": None,
                "error": str(exc),
            }

    return {"icons": out}


def search_icons(query: str, count: int = 8) -> dict[str, Any]:
    """Return a list of Iconify candidates with preview URLs + raw SVG markup.

    Used during iteration when the user asks to swap a specific icon — the
    caller surfaces these as AskUserQuestion options so the user picks one.
    """
    n = _clamp_icon_count(count, ceiling=20)
    icon_ids = _iconify_search(query, count=n, prefix="lucide")
    color_hex = _hex_for_iconify("#0F2A4A")
    results: list[dict[str, Any]] = []
    for icon_id in icon_ids:
        try:
            svg = _iconify_svg(icon_id, color_hex, size=48)
        except ImagesError as exc:
            log.warning(
                "search_icons: icon_id=%r SVG fetch failed: %s",
                icon_id, exc,
            )
            continue
        results.append(
            {
                "icon_id": icon_id,
                "preview_url": (
                    f"https://api.iconify.design/{icon_id}.svg?width=24"
                ),
                "svg": svg,
            }
        )
    return {"results": results}


__all__ = [
    "ImagesError",
    "fetch_icons",
    "search_icons",
    "search_stock_images",
]
