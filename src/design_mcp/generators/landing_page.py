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

log = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "landing_page.yaml"


# ---------------------------------------------------------------------------
# Return-prompts pattern — build the brief the caller's Claude will act on
# ---------------------------------------------------------------------------

def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text())


def _build_instructions(slug_hint: str, brief: str, references: Optional[list[str]]) -> str:
    parts = [
        "You are generating a Landing Page microsite for the Acquirely platform.",
        "",
        f"BRIEF:\n{brief}",
        "",
        f"SUGGESTED SLUG: {slug_hint}  (kebab-case; override if a better one fits the brief)",
        "",
    ]
    if references:
        parts.append("REFERENCE URLs / inspiration notes:")
        parts.extend(f"- {r}" for r in references)
        parts.append("")
    parts.extend([
        "STEP 1 — Read the `contract` field carefully. Every mandatory_section, SEO",
        "requirement, image attribute and forbidden pattern in it is binding.",
        "",
        "STEP 2 — Read the `manifest_schema` field. Your manifest MUST validate",
        "against that JSON schema (it is the Pydantic schema for LandingPageManifest).",
        "",
        "STEP 3 — Produce two artefacts:",
        "  (a) a single self-contained HTML5 document — Tailwind v4 via CDN script,",
        "      Option Y+ theming (CSS vars + :root fallback in <style>), exactly one",
        "      <h1>, hero LCP image with fetchpriority=\"high\" loading=\"eager\",",
        "      all other <img> loading=\"lazy\" + width + height + non-empty alt.",
        "      Form posts to /api/handle_Client_Lead_Submission.",
        "  (b) a manifest dict matching `manifest_schema`. Exactly 3 feature cards.",
        "",
        "STEP 4 — Call the `submit_design` MCP tool with:",
        "    submit_design(",
        "        design_id='<the design_id from this response>',",
        "        html='<your full HTML>',",
        "        manifest=<your manifest dict>,",
        "    )",
        "",
        "The server will Pydantic-validate the manifest, sanity-check the HTML,",
        "and commit to the microsite-design-skills repo. If validation fails you",
        "will get a structured error back — fix and call submit_design again.",
        "",
        "If the user wants refinements after seeing a draft, call",
        "`update_design(design_id, instructions=...)` to receive iteration",
        "instructions, regenerate, then submit_design again.",
    ])
    return "\n".join(parts)


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
    instructions = _build_instructions(slug_hint, brief, references)
    return {
        "instructions": instructions,
        "contract": contract,
        "manifest_schema": manifest_schema,
        "slug_hint": slug_hint,
    }


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

  <title>{_e(m.seo.title)}</title>
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
        <form class="bg-white shadow-md rounded-lg p-8 space-y-4" action="/api/handle_Client_Lead_Submission" method="post" novalidate>
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
