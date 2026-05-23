"""Landing Page family generator — calls Anthropic API to produce HTML + manifest
matching the contracts/landing_page.yaml contract.

Stub mode: when ANTHROPIC_API_KEY starts with 'sk-ant-DUMMY' or 'sk-ant-stub',
returns a hand-crafted sample so the rest of the pipeline (repo push, audit, etc.)
can be tested without burning API credits or needing a live key.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

import yaml
from anthropic import Anthropic

from ..config import DesignConfig
from ..manifest import (
    FeatureCard,
    FormConfig,
    HeroSection,
    LandingPageManifest,
    SeoBlock,
    ThemeTokens,
)

log = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "landing_page.yaml"

# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate(
    cfg: DesignConfig,
    brief: str,
    references: Optional[list[str]] = None,
    requested_slug: Optional[str] = None,
) -> tuple[str, LandingPageManifest, str]:
    """Generate HTML + manifest for a landing page.

    Returns: (html, manifest, chat_summary)
    """
    slug = requested_slug or _slugify(brief)

    if _is_stub_mode(cfg):
        log.info("using STUB mode (no real Anthropic call) — slug=%s", slug)
        return _stub_output(slug, brief)

    # Real Anthropic call
    client = Anthropic(api_key=cfg.anthropic_api_key)
    system_prompt = _build_system_prompt()

    user_message = f"Brief: {brief}\n\nGenerate the HTML + manifest for slug '{slug}'."
    if references:
        user_message += f"\n\nReference URLs/notes:\n" + "\n".join(f"- {r}" for r in references)

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    response_text = msg.content[0].text

    html, manifest_dict = _parse_response(response_text)
    manifest = LandingPageManifest(**manifest_dict)
    chat_summary = _build_chat_summary(brief, references, manifest)
    return html, manifest, chat_summary


# ---------------------------------------------------------------------------
# Stub mode (for testing without API key)
# ---------------------------------------------------------------------------

def _is_stub_mode(cfg: DesignConfig) -> bool:
    return "DUMMY" in cfg.anthropic_api_key or "stub" in cfg.anthropic_api_key.lower()


def _stub_output(slug: str, brief: str) -> tuple[str, LandingPageManifest, str]:
    """Hand-crafted minimal valid output for testing the pipeline."""
    manifest = LandingPageManifest(
        slug=slug,
        intent=brief[:200],
        seo=SeoBlock(
            title=f"{slug.replace('-', ' ').title()} — stub for testing",
            meta_description=f"Stub landing page for {slug}. Replace with real Anthropic-generated content when API key is configured.",
        ),
        hero=HeroSection(
            headline=f"Welcome to {slug.replace('-', ' ').title()}",
            subheading="This is a stub-mode hero. Provide a real ANTHROPIC_API_KEY to generate real content.",
            cta_label="Get Started",
            image_url=f"https://picsum.photos/seed/{slug}-hero/1600/900",
            image_alt=f"{slug} hero image placeholder",
        ),
        features=[
            FeatureCard(
                heading=f"Feature {i}",
                paragraph=f"Stub paragraph for feature {i}. Real content comes from Anthropic.",
                image_url=f"https://picsum.photos/seed/{slug}-f{i}/400/400",
                image_alt=f"Feature {i} illustration",
            )
            for i in range(1, 4)
        ],
        form=FormConfig(submit_label="Get Started"),
        theme=ThemeTokens(),
    )
    html = _render_html(manifest)
    chat_summary = _build_chat_summary(brief, None, manifest, stub=True)
    return html, manifest, chat_summary


# ---------------------------------------------------------------------------
# HTML renderer (used by stub mode AND as a fallback when LLM returns invalid HTML)
# ---------------------------------------------------------------------------

def _render_html(m: LandingPageManifest) -> str:
    """Produce a clean Tailwind v4 landing page from a manifest. Implements:
    - Option Y+ theming (CSS vars + :root bake-in fallback)
    - Mandatory regions: hero / features / form / footer
    - Full SEO head block
    - All <img> with alt + width + height + loading
    - Single <h1>
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


# ---------------------------------------------------------------------------
# Real-LLM helpers (used when ANTHROPIC_API_KEY is real)
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    return f"""You are a senior front-end engineer + designer producing landing pages for the Acquirely platform.

OUTPUT FORMAT (strict — your message body must be EXACTLY two fenced blocks, nothing else):

```html
<!doctype html>
<html lang="en">
...full self-contained landing page...
</html>
```

```yaml
family: landing-page
version: 1
slug: <kebab-case>
intent: <one-paragraph statement of who/what>
seo:
  title: ...
  meta_description: ...
hero:
  headline: ...
  subheading: ...
  cta_label: ...
  image_url: ...
  image_alt: ...
features:
  - heading: ...
    paragraph: ...
    image_url: ...
    image_alt: ...
  - ... (exactly 3 cards)
form:
  submit_label: ...
theme:
  color_primary: <hex>
  color_accent: <hex>
optional_sections: []
```

CONTRACT (you MUST satisfy every rule):

{yaml.dump(contract, sort_keys=False, default_flow_style=False)}

CRITICAL:
- Tailwind v4 via CDN script only — no other CSS framework
- Single <h1> in hero, semantic <main>/<section>/<footer>, no <div> soup
- Every <img> needs src + alt (non-empty for content imgs) + width + height
- LCP image (hero) needs fetchpriority="high" loading="eager"
- All other images loading="lazy"
- Form posts to /api/handle_Client_Lead_Submission (the Acquirely backend handles routing)
- Use CSS variables for colors/fonts (bake :root fallback into <style>)
- Use Lorem Picsum URLs (https://picsum.photos/seed/<slug>-<region>/<w>/<h>) for placeholder images
- NO jQuery, Bootstrap, dead CDNs, inline <script> blobs, dangerouslySetInnerHTML patterns
"""


def _parse_response(text: str) -> tuple[str, dict]:
    """Pull the HTML and YAML blocks out of the LLM response."""
    html_match = re.search(r"```html\s*\n(.*?)\n```", text, re.DOTALL)
    yaml_match = re.search(r"```yaml\s*\n(.*?)\n```", text, re.DOTALL)
    if not html_match or not yaml_match:
        raise RuntimeError(f"LLM response did not contain both ```html and ```yaml blocks. Got:\n{text[:500]}...")
    html = html_match.group(1).strip()
    manifest_dict = yaml.safe_load(yaml_match.group(1))
    return html, manifest_dict


def _build_chat_summary(brief: str, references: Optional[list[str]], m: LandingPageManifest, stub: bool = False) -> str:
    parts = [
        f"# Design chat — {m.slug}",
        "",
        "## Brief",
        brief,
        "",
    ]
    if references:
        parts.extend(["## References", *(f"- {r}" for r in references), ""])
    if stub:
        parts.append("> ⚠️ STUB mode — no real Anthropic call. Set a real ANTHROPIC_API_KEY in .env to generate live.\n")
    parts.extend([
        "## Output",
        f"- family: {m.family}",
        f"- slug: {m.slug}",
        f"- intent: {m.intent}",
        f"- features: {len(m.features)}",
        f"- optional sections: {', '.join(m.optional_sections) or '(none)'}",
        f"- theme: primary={m.theme.color_primary}, accent={m.theme.color_accent}",
    ])
    return "\n".join(parts) + "\n"


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:50] or "untitled-design")
