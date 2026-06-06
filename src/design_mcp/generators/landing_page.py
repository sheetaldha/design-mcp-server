"""Landing Page family — return-prompts pattern.

This module no longer calls the Anthropic API. Instead, it assembles a
"design brief" (instructions + contract + manifest JSON schema) that the
caller's Claude (claude.ai web/mobile or Claude Code) uses to generate
the HTML + manifest. The caller then POSTs the result back via the
`submit_design` MCP tool, where it gets validated and committed.

The `_render_html` + `_e` helpers are retained because they are useful for
fallback / preview rendering and for verifying that a submitted manifest
can be losslessly re-rendered when needed.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from ..manifest import (
    FeatureCard,
    LandingPageManifest,
)
from ._brief_template import LANDING_PAGE_DEFAULTS, ClarifyingField, field, render_brief

log = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "landing_page.yaml"


# ---------------------------------------------------------------------------
# Return-prompts pattern — build the brief the caller's Claude will act on
# ---------------------------------------------------------------------------

def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text())


_CLARIFYING_FIELDS: list[ClarifyingField] = [
    # 1. Page intent — scope-based routing (new microsite / enhancement / replica)
    field(
        "page_intent",
        "What kind of work is this?",
        "New microsite landing page",
        "Enhancement to an existing landing page",
        "Replica of an existing landing page",
    ),
    # 2. Brand / site name
    field(
        "site_name",
        "Brand / site name to append after the page title (e.g. \"HealthBoost\")? Derive from the brief if obvious; otherwise ask.",
    ),
    # 3. Site brief — front-loaded for the brief-first / skip-answered pattern
    field(
        "site_brief",
        "Upload or paste your brief — images, copy, wireframes, reference URLs, "
        "anything. The more you share, the fewer questions I'll ask. I'll skip "
        "any remaining questions already answered in your brief.",
    ),
    # 4. Primary CTA
    field(
        "primary_cta",
        "Single action you want a visitor to take?",
        "Book a consultation/demo",
        "Request a quote",
        "Sign up / create account",
        "Download / get the guide",
        "Contact us",
    ),
    # 5. Review checkpoint — pseudo-field; no data collected
    field(
        "review_checkpoint",
        "Summary of everything collected so far + remaining questions — confirm or change?",
        is_checkpoint=True,
    ),
    # 6. Palette
    field("palette", "Brand colours / fonts / page to match? Say \"you pick\" and I'll choose."),
    # 7. Benefits
    field("benefits", "Top 2 or 3 benefits or proof points? (numbers, badges, testimonials)"),
    # 8. Tone
    field(
        "tone",
        "Tone of voice?",
        "Friendly + casual",
        "Professional + clinical",
        "Playful + bold",
        "Authoritative + premium",
    ),
    # 9. GTM tag
    field(
        "gtm_tag",
        "Google Tag Manager container ID to embed? Paste it (e.g. GTM-XXXXXXX) or skip.",
    ),
    # 10. References to avoid
    field("references_to_avoid", "Anything to avoid? (competitor styles, forbidden words, imagery)"),
    # 11. Optional sections
    field(
        "optional_sections_content",
        "Testimonials, FAQ, or trust badges? If yes: 2-6 testimonials "
        "(quote+author+location), 3-10 FAQs (Q&A), 3-8 trust badges (label+detail). Or skip.",
    ),
]

_CONTRACT_NOTES = (
    "Landing Page contract: self-contained HTML5 with Tailwind v4 via the CDN script, Option Y+ theming "
    "(CSS variables in :root plus /tokens.css), exactly one <h1> in the hero, exactly three feature cards "
    "in the manifest, hero LCP image with fetchpriority=\"high\" loading=\"eager\", every other <img> with "
    "loading=\"lazy\" plus width, height and non-empty alt. Lead form posts to /api/add-lead "
    "(generic micrositebackend lead endpoint). Font from the contract's font_allowlist. "
    "Manifest seo.title is the bare title (≤ 60 chars); also supply seo.site_name (3-50 chars, the brand "
    "name — ask the user if not derivable from the brief, e.g. brief mentions \"HealthBoost\" → "
    "site_name=\"HealthBoost\"). The rendered <title> MUST be \"{title} | {site_name}\" (the brand suffix "
    "lives ONLY in <title>). og:title, twitter:title and JSON-LD `name`/`headline` stay BARE (no suffix). "
    "Also emit <meta property=\"og:url\" content=\"{canonical_url}\"> in the head and include "
    "\"url\": \"{canonical_url}\" inside the JSON-LD WebPage object alongside `name` and `description`. "
    "Optional sections: if `optional_sections` contains 'testimonials'/'faq'/'trust_badges', populate the "
    "matching manifest field (testimonials 2-6, faq 3-10, trust_badges 3-8) and render the HTML FROM that "
    "same data so manifest and HTML match — orphan flag-or-data fails validation. "
    "Tools available to the caller: submit_design, update_design, get_design_status, cancel_design, get_preview_url, fetch_url_screenshots."
)

# Static items rendered into the brief's STEP-4 sanity-check line.
# (The brief is constructed before a manifest exists, so this list stays
# manifest-agnostic; per-section data items are added at run time by
# `sanity_check_items_for_manifest` once the manifest is in hand.)
_SANITY_CHECK_ITEMS = [
    "seo.title ≤60 chars (bare)",
    "site_name present · <title>={title} | {site_name}",
    "og:url present · JSON-LD url present",
    "hero <img> preload + LCP pri",
    "3 feature cards",
    "lead form posts to /api/add-lead",
    "all imgs have width/height/alt",
    "one <h1> in hero",
    "optional section data populated (testimonials 2-6, faq 3-10, trust_badges 3-8)",
]


def sanity_check_items_for_manifest(manifest: LandingPageManifest) -> list[str]:
    """Return the static sanity-check items PLUS per-section data items
    conditional on `manifest.optional_sections`.

    Used by callers that want the post-generation checklist (STEP 4) to spell
    out the per-section counts. The brief itself uses the static list because
    it is rendered before a manifest exists.
    """
    items = list(_SANITY_CHECK_ITEMS)
    flags = set(manifest.optional_sections)
    if "testimonials" in flags:
        n = len(manifest.testimonials or [])
        items.append(f"testimonials data populated ✓ ({n} items)")
    if "faq" in flags:
        n = len(manifest.faq or [])
        items.append(f"faq data populated ✓ ({n} items)")
    if "trust_badges" in flags:
        n = len(manifest.trust_badges or [])
        items.append(f"trust_badges data populated ✓ ({n} items)")
    return items


def _build_instructions(
    slug_hint: str,
    brief: str,
    references: Optional[list[str]],
) -> str:
    return render_brief(
        family_label="Landing Page",
        brief=brief,
        slug_hint=slug_hint,
        references=references,
        clarifying_fields=_CLARIFYING_FIELDS,
        family_contract_notes=_CONTRACT_NOTES,
        defaults=LANDING_PAGE_DEFAULTS,
        sanity_check_items=_SANITY_CHECK_ITEMS,
        enable_brief_first_branching=True,
        strict_script=True,
    )


def make_design_brief(
    design_id: str,
    brief: str,
    references: Optional[list[str]] = None,
    requested_slug: Optional[str] = None,
) -> dict[str, Any]:
    """Build the structured brief the caller's Claude will act on.

    Returns a dict with: instructions, contract, manifest_schema, slug_hint.
    The MCP tool layer adds design_id / status / expires_at on top.
    """
    slug_hint = requested_slug or _slugify(brief)
    contract = _load_contract()
    manifest_schema = LandingPageManifest.model_json_schema()
    # `design_id` is accepted to keep the function signature stable, but the
    # rendered instructions refer to it symbolically (the caller sees the real
    # id as a sibling field on the MCP response).
    _ = design_id
    instructions = _build_instructions(slug_hint, brief, references)
    return {
        "instructions": instructions,
        "contract": contract,
        "manifest_schema": manifest_schema,
        "slug_hint": slug_hint,
    }


# ---------------------------------------------------------------------------
# Server-driven intake — short directive paired with `next_question` payloads
# ---------------------------------------------------------------------------

# Tight imperative that replaces the long prose `instructions` blob once the
# server owns the question flow. Each `next_question` returned by
# `start_landing_page_intake` / `submit_clarifying_answer` carries the verbatim
# text + options the caller's Claude must surface — this directive only
# tells it WHICH tool to use and WHEN to advance.
INSTRUCTIONS_SHORT = (
    "INTAKE FLOW: This server controls the clarifying-question flow. For each "
    "`next_question` returned:\n"
    "- If `is_checkpoint=false`: call AskUserQuestion with `question_text` and "
    "`options` EXACTLY as given (verbatim, in order, no rephrasing). "
    "If `options` is null, ask as plain text.\n"
    "- If `is_checkpoint=true`: render the `checkpoint_payload` as a ✅/❓ "
    "summary message in chat (NOT AskUserQuestion). Wait for user reply.\n"
    "\n"
    "After the user answers, call "
    "`submit_clarifying_answer(design_id, field_key, answer)`. The response "
    "includes the NEXT `next_question`, or `null` when intake is complete.\n"
    "\n"
    "When `next_question` is `null`, proceed to STEP 2 (outline) per the "
    "existing contract (see `instructions_legacy` for the full runbook)."
)


def landing_page_field_list():
    """Return the canonical clarifying-field list (used by the state machine).

    Module-public accessor so ``server.py`` doesn't have to reach into the
    underscore-prefixed module attribute directly.
    """
    return _CLARIFYING_FIELDS


# ---------------------------------------------------------------------------
# HTML renderer (retained for fallback / preview / future validation use)
# ---------------------------------------------------------------------------

def _render_html(m: LandingPageManifest) -> str:
    """Produce a clean Tailwind v4 landing page from a manifest.

    Used historically by stub mode; retained because it's a useful fallback
    renderer if a submitted HTML ever needs regenerating from manifest alone.
    """
    canonical = str(m.seo.canonical_url) if m.seo.canonical_url else f"https://example.com/{m.slug}"
    og_image = m.seo.og_image_url or m.hero.image_url

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">

  <title>{_e(m.seo.title)} | {_e(m.seo.site_name)}</title>
  <meta name="description" content="{_e(m.seo.meta_description)}">
  <link rel="canonical" href="{_e(canonical)}">

  <meta property="og:type" content="website">
  <meta property="og:title" content="{_e(m.seo.title)}">
  <meta property="og:description" content="{_e(m.seo.meta_description)}">
  <meta property="og:url" content="{_e(canonical)}">
  <meta property="og:image" content="{_e(og_image)}">

  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{_e(m.seo.title)}">
  <meta name="twitter:description" content="{_e(m.seo.meta_description)}">
  <meta name="twitter:image" content="{_e(og_image)}">

  <script type="application/ld+json">
{{"@context":"https://schema.org","@type":"WebPage","name":"{_e(m.seo.title)}","description":"{_e(m.seo.meta_description)}","url":"{_e(canonical)}"}}
  </script>

  <!-- Option Y+ theming: :root bake-in fallback; tokens.css (if loaded) overrides at runtime -->
  <style>
    :root {{
      --color-primary: {m.theme.color_primary};
      --color-accent: {m.theme.color_accent};
      --color-text-body: {m.theme.color_text_body};
      --color-bg-body: {m.theme.color_bg_body};
      --font-heading: {m.theme.font_heading};
      --font-body: {m.theme.font_body};
      --spacing-section: 4rem;
    }}
    body {{ font-family: var(--font-body); color: var(--color-text-body); background: var(--color-bg-body); }}
    h1, h2, h3 {{ font-family: var(--font-heading); }}
  </style>
  <link rel="stylesheet" href="/tokens.css">

  <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
</head>
<body class="antialiased">

  <header class="bg-[var(--color-primary)] text-white">
    <section class="max-w-6xl mx-auto px-6 py-20 md:py-28 grid md:grid-cols-2 gap-10 items-center">
      <div>
        <h1 class="text-4xl md:text-5xl font-bold tracking-tight">{_e(m.hero.headline)}</h1>
        <p class="mt-4 text-lg md:text-xl opacity-90">{_e(m.hero.subheading)}</p>
        <a href="#signup" class="mt-8 inline-block bg-[var(--color-accent)] hover:opacity-90 text-white px-6 py-3 rounded-md font-semibold">{_e(m.hero.cta_label)}</a>
      </div>
      <div>
        <img src="{_e(m.hero.image_url)}" alt="{_e(m.hero.image_alt)}" width="1200" height="800" fetchpriority="high" loading="eager" class="rounded-lg shadow-xl w-full h-auto">
      </div>
    </section>
  </header>

  <main>

    <section class="max-w-6xl mx-auto px-6 py-[var(--spacing-section)]">
      <div class="grid md:grid-cols-3 gap-8">
{''.join(_feature_card(c) for c in m.features)}
      </div>
    </section>

    <section id="signup" class="bg-gray-50 py-[var(--spacing-section)]">
      <div class="max-w-md mx-auto px-6">
        <form class="bg-white shadow-md rounded-lg p-8 space-y-4" action="/api/add-lead" method="post" novalidate>
          <h2 class="text-2xl font-bold text-center">{_e(m.hero.cta_label)}</h2>
          <input type="text"  name="name"  placeholder="Your name"   required minlength="2" class="w-full px-4 py-2 border border-gray-300 rounded">
          <input type="email" name="email" placeholder="Your email"  required class="w-full px-4 py-2 border border-gray-300 rounded">
          <input type="tel"   name="phone" placeholder="Your phone"  required class="w-full px-4 py-2 border border-gray-300 rounded">
          <button type="submit" class="w-full bg-[var(--color-primary)] hover:opacity-90 text-white py-3 rounded font-semibold">{_e(m.form.submit_label)}</button>
          <p class="text-xs text-gray-500 text-center">By submitting, you agree to our <a href="{_e(m.form.privacy_link)}" class="underline">privacy policy</a>.</p>
        </form>
      </div>
    </section>

  </main>

  <footer class="border-t border-gray-200 py-8 text-center text-sm text-gray-500">
    <p>&copy; {date.today().year}. All rights reserved.</p>
  </footer>

</body>
</html>
"""


def _feature_card(c: FeatureCard) -> str:
    return f"""        <div class="bg-white rounded-lg p-6 text-center">
          <img src="{_e(c.image_url)}" alt="{_e(c.image_alt)}" width="400" height="400" loading="lazy" class="mx-auto w-24 h-24 mb-4 rounded-full object-cover">
          <h3 class="font-semibold text-xl mb-2">{_e(c.heading)}</h3>
          <p class="text-gray-600">{_e(c.paragraph)}</p>
        </div>
"""


def _e(s: str) -> str:
    """Minimal HTML escape — quotes, ampersands, angle brackets."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:50] or "untitled-design")
