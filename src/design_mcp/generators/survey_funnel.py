"""Survey Funnel family generator.

Sibling of `generators/landing_page.py`. Produces:
  - `make_design_brief(brief, requested_slug=None)` — the payload Agent 1's
    refactored `server.py` hands to the caller's Claude (instructions + the
    full contract YAML + the manifest JSON schema + a slug hint).
  - `_render_html(manifest)` — a deterministic Python renderer used by stub
    output and as a fallback / preview path when the caller's output round-
    trips back through `submit_design`.

Following Day-3's return-prompts shift, this module performs NO LLM calls
itself. All generation runs on the caller's subscription.

Key Survey-Funnel rules enforced here:
  - 1..5 steps (manifest schema clamps this)
  - linear progression only — no `next_step_when` branching DSL in v1
  - OTP is a top-level boolean on the manifest, NOT a step in the array.
    When `otp_enabled=true`, the renderer emits an <section class="otp">
    between the final fieldset and the final submit; UI posts to
    /api/verificationsms (existing backend endpoint).
  - Final step submits to /api/handle_Client_Lead_Submission (shared with
    landing-page family — integrations toggle via CMS, not per-page).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from ..manifest import (
    FormQuestion,
    FormStep,
    HeroSection,
    SeoBlock,
    SurveyFunnelManifest,
    ThemeTokens,
)

log = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "survey_funnel.yaml"


# ---------------------------------------------------------------------------
# make_design_brief — the API Agent 1's refactored server.py calls.
# Returns the payload the caller's Claude needs to generate a Survey Funnel.
# ---------------------------------------------------------------------------

def make_design_brief(
    brief: str,
    references: Optional[list[str]] = None,
    requested_slug: Optional[str] = None,
) -> dict:
    """Build the design-brief payload for a Survey Funnel request.

    Args:
        brief: free-text request (what the funnel sells, audience, tone, etc.)
        references: optional URLs / inspiration / competitor sites
        requested_slug: optional override; default auto-slugified from brief
    """
    slug_hint = requested_slug or _slugify(brief)
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    ref_block = ""
    if references:
        ref_block = "\n\nREFERENCE URLs / inspiration notes:\n" + "\n".join(f"- {r}" for r in references)
    return {
        "instructions": _build_instructions() + ref_block,
        "contract": contract,
        "manifest_schema": SurveyFunnelManifest.model_json_schema(),
        "slug_hint": slug_hint,
    }


def _build_instructions() -> str:
    """Plain-English generation guidelines. Reads as a system prompt for Claude."""
    return """You are a senior front-end engineer + designer producing **Survey Funnel** microsites for the Acquirely platform.

A Survey Funnel is a multi-step lead-capture form. The user lands on the hero, clicks the CTA, then steps through 1..5 fieldsets answering qualifying questions, optionally completes an OTP verification step, then submits. Use cases: insurance / energy / solar quote comparison.

OUTPUT FORMAT (strict — your message body must be EXACTLY two fenced blocks, nothing else):

```html
<!doctype html>
<html lang="en">
...full self-contained survey funnel page...
</html>
```

```yaml
family: survey-funnel
version: 1
slug: <kebab-case>
intent: <one-paragraph who/what this funnel qualifies>
seo:
  title: ...
  meta_description: ...
hero:
  headline: ...
  subheading: ...
  cta_label: ...
  image_url: https://picsum.photos/seed/<slug>-hero/1600/900
  image_alt: ...
steps:
  - id: step-1
    heading: ...
    questions:
      - { name: <snake_case>, type: <text|email|tel|radio|select|checkbox>, label: ..., required: true }
  - id: step-2
    heading: ...
    questions: [ ... ]
  # 1..5 steps total — default 3
otp_enabled: false   # set true only if the brief explicitly asks for OTP / SMS verification
submit_label: ...
optional_sections: []   # any of: progress_indicator, trust_badges, testimonials, sticky_cta_mobile
theme:
  color_primary: <hex>
  color_accent: <hex>
```

GENERATION RULES — you MUST satisfy every one of these (the contract YAML below also enforces them):

1. **Steps**: 1..5 fieldsets. Default to 3 steps unless the brief says otherwise. Each step is a `<fieldset>` containing the step heading (<legend> or <h2>) and the questions. The first step is visible on page load; later steps carry the HTML `hidden` attribute until the user clicks "Next".

2. **Linear progression only** — no branching. Step 1 → 2 → 3 → (OTP if enabled) → submit. Do NOT emit `next_step_when` rules or conditional skipping logic in v1.

3. **Step transitions**: emit a minimal inline `<script type="module">` block that toggles `hidden` on fieldsets when Next/Back buttons are clicked. No jQuery, no framework, no external state library. Plain DOM `addEventListener`. Validate the current fieldset (HTML5 `checkValidity()`) before advancing.

4. **OTP step**: if `otp_enabled: true` in the manifest, emit a `<section class="otp" hidden>` between the final fieldset and the final submit. The OTP section has a "Send code" button that POSTs the phone number to `/api/verificationsms`, then a 6-digit code input, then a "Verify" button. Do NOT emit the OTP section when `otp_enabled: false`. OTP is NEVER a step in the `steps` array — it is rendered conditionally from the top-level flag.

5. **Final submit**: the final step's submit button POSTs the assembled form (all step inputs) to `/api/handle_Client_Lead_Submission` (the Acquirely backend handles integration routing — Databowl / HubSpot / Slack / Webhook are CMS toggles, not page-level config).

6. **One <h1>**: in the hero. Step headings are `<h2>` (or `<legend>` — your choice; both are acceptable semantic markup).

7. **Images**: every `<img>` needs `src`, non-empty `alt`, `width`, `height`. The hero LCP image gets `fetchpriority="high" loading="eager"`. All other images get `loading="lazy"`. Placeholder URLs use `https://picsum.photos/seed/<slug>-<region>/<w>/<h>` (Skill B's Register Agent uploads real images later).

8. **Theming (Option Y+)**: emit a `<style>:root { --color-primary: ...; --color-accent: ...; --font-heading: ...; --font-body: ...; --spacing-section: 4rem; }</style>` bake-in block. Reference variables via Tailwind arbitrary values like `bg-[var(--color-primary)]`. Also `<link rel="stylesheet" href="/tokens.css">` so CMS tokens override at deploy time. Pick fonts ONLY from the contract's `theming.font_allowlist`.

9. **Required <head> contents**: charset, viewport, full SEO block (title / meta_description / canonical / og:* / twitter:* / JSON-LD WebPage), the bake-in `:root` <style>, the `/tokens.css` link, and the Tailwind v4 CDN script `https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4`. NO other CSS framework, NO jQuery, NO Bootstrap, NO dead CDNs.

10. **Footer**: copyright line, privacy + terms links. Same shape as landing-page family.

11. **Question types**:
    - `text`, `email`, `tel` render as `<input>` of that type.
    - `radio` renders as a set of `<label><input type="radio" name="..." value="...">…</label>` (one per option).
    - `select` renders as `<select>` with `<option>` per option.
    - `checkbox` renders as one or many `<input type="checkbox">` per option (multi-select).
    For radio/select/checkbox the `options` list is required (min 2). For text/email/tel no `options` allowed.

12. **Optional sections**: include `progress_indicator` (a "Step N of M" counter or a CSS bar above each fieldset), `trust_badges`, `testimonials`, or `sticky_cta_mobile` ONLY when the brief asks for them or the manifest's `optional_sections` lists them.

Below this you will see the full contract (forbidden patterns, image rules, etc.) and the JSON schema for the manifest. The HTML you emit must validate against the contract; the YAML you emit must validate against the schema.
"""


# ---------------------------------------------------------------------------
# Stub output — a deterministic 3-step funnel for unit tests / previews.
# No LLM involved; pure Python construction of a valid manifest + rendered HTML.
# ---------------------------------------------------------------------------

def _stub_output(slug: str, brief: str) -> tuple[str, SurveyFunnelManifest, str]:
    """Hand-crafted minimal valid 3-step funnel for pipeline testing."""
    manifest = SurveyFunnelManifest(
        slug=slug,
        intent=brief[:200] if len(brief) >= 10 else f"Survey funnel stub for {slug}",
        seo=SeoBlock(
            title=f"{slug.replace('-', ' ').title()} — stub funnel",
            meta_description=(
                f"Stub survey funnel for {slug}. Used by tests + previews. "
                f"Real designs are produced by the caller's chat session via make_design_brief."
            ),
        ),
        hero=HeroSection(
            headline=f"Compare {slug.replace('-', ' ').title()} quotes",
            subheading="Deterministic stub hero. Real designs come from the caller's chat session.",
            cta_label="Start",
            cta_url="#step-1",
            image_url=f"https://picsum.photos/seed/{slug}-hero/1600/900",
            image_alt=f"{slug} hero image placeholder",
        ),
        steps=[
            FormStep(
                id="step-1",
                heading="Tell us about your situation",
                questions=[
                    FormQuestion(
                        name="situation",
                        type="radio",
                        label="Which best describes you?",
                        options=["Option A", "Option B", "Option C"],
                        required=True,
                    ),
                ],
            ),
            FormStep(
                id="step-2",
                heading="When are you looking to act?",
                questions=[
                    FormQuestion(
                        name="timeframe",
                        type="radio",
                        label="Timeframe",
                        options=["Within 3 months", "3-6 months", "Just researching"],
                        required=True,
                    ),
                ],
            ),
            FormStep(
                id="step-3",
                heading="Your contact details",
                questions=[
                    FormQuestion(name="name",  type="text",  label="Full name", required=True),
                    FormQuestion(name="email", type="email", label="Email",     required=True),
                    FormQuestion(name="phone", type="tel",   label="Mobile",    required=True),
                ],
            ),
        ],
        otp_enabled=False,
        submit_label="Get My Quotes",
        optional_sections=["progress_indicator"],
        theme=ThemeTokens(),
    )
    html = _render_html(manifest)
    chat_summary = _build_chat_summary(brief, None, manifest, stub=True)
    return html, manifest, chat_summary


# ---------------------------------------------------------------------------
# HTML renderer (stub mode + fallback for invalid LLM HTML)
# ---------------------------------------------------------------------------

def _render_html(m: SurveyFunnelManifest) -> str:
    canonical = str(m.seo.canonical_url) if m.seo.canonical_url else f"https://example.com/{m.slug}"
    og_image = m.seo.og_image_url or m.hero.image_url
    total_steps = len(m.steps)

    fieldsets_html = "".join(_fieldset(s, i, total_steps, m) for i, s in enumerate(m.steps))
    otp_html = _otp_section(m) if m.otp_enabled else ""

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
    h1, h2, h3, legend {{ font-family: var(--font-heading); }}
    fieldset[hidden] {{ display: none; }}
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
        <a href="#step-1" class="mt-8 inline-block bg-[var(--color-accent)] hover:opacity-90 text-white px-6 py-3 rounded-md font-semibold">{_e(m.hero.cta_label)}</a>
      </div>
      <div>
        <img src="{_e(m.hero.image_url)}" alt="{_e(m.hero.image_alt)}" width="1200" height="800" fetchpriority="high" loading="eager" class="rounded-lg shadow-xl w-full h-auto">
      </div>
    </section>
  </header>

  <main>
    <section id="step-1" class="bg-gray-50 py-[var(--spacing-section)]">
      <div class="max-w-xl mx-auto px-6">
        <form id="survey-form" class="bg-white shadow-md rounded-lg p-8 space-y-6" action="/api/handle_Client_Lead_Submission" method="post" novalidate>
{fieldsets_html}{otp_html}
          <p class="text-xs text-gray-500 text-center">By submitting, you agree to our <a href="#privacy" class="underline">privacy policy</a>.</p>
        </form>
      </div>
    </section>
  </main>

  <footer class="border-t border-gray-200 py-8 text-center text-sm text-gray-500">
    <p>&copy; {date.today().year}. All rights reserved.</p>
  </footer>

  <script type="module">
    // Minimal linear step progression — no framework, no jQuery.
    const fieldsets = Array.from(document.querySelectorAll('#survey-form fieldset[data-step]'));
    const otp = document.querySelector('#survey-form section.otp');
    const otpEnabled = {str(m.otp_enabled).lower()};

    function show(idx) {{
      fieldsets.forEach((f, i) => f.hidden = (i !== idx));
      if (otp) otp.hidden = true;
    }}
    function showOtp() {{
      fieldsets.forEach(f => f.hidden = true);
      if (otp) otp.hidden = false;
    }}

    fieldsets.forEach((fs, idx) => {{
      const next = fs.querySelector('[data-action="next"]');
      const back = fs.querySelector('[data-action="back"]');
      if (next) next.addEventListener('click', (e) => {{
        e.preventDefault();
        const ok = Array.from(fs.querySelectorAll('input,select')).every(el => el.checkValidity());
        if (!ok) {{ fs.reportValidity?.(); return; }}
        if (idx < fieldsets.length - 1) show(idx + 1);
        else if (otpEnabled) showOtp();
        else document.getElementById('survey-form').submit();
      }});
      if (back) back.addEventListener('click', (e) => {{ e.preventDefault(); if (idx > 0) show(idx - 1); }});
    }});
  </script>

</body>
</html>
"""


def _fieldset(step: FormStep, idx: int, total: int, m: SurveyFunnelManifest) -> str:
    hidden_attr = "" if idx == 0 else " hidden"
    progress = (
        f'<p class="text-sm text-gray-500 mb-2">Step {idx + 1} of {total}</p>'
        if "progress_indicator" in m.optional_sections
        else ""
    )
    questions_html = "".join(_question(q) for q in step.questions)
    back_btn = (
        '<button type="button" data-action="back" class="px-4 py-2 border border-gray-300 rounded font-semibold">Back</button>'
        if idx > 0
        else ""
    )
    next_label = m.submit_label if (idx == total - 1 and not m.otp_enabled) else "Next"
    return f"""          <fieldset data-step="{idx}"{hidden_attr} class="space-y-4 border-0 p-0 m-0">
            {progress}
            <legend class="text-2xl font-bold mb-4">{_e(step.heading)}</legend>
{questions_html}
            <div class="flex justify-between gap-2 pt-2">
              {back_btn}
              <button type="button" data-action="next" class="ml-auto bg-[var(--color-primary)] hover:opacity-90 text-white px-6 py-3 rounded font-semibold">{_e(next_label)}</button>
            </div>
          </fieldset>
"""


def _question(q: FormQuestion) -> str:
    req = " required" if q.required else ""
    if q.type in {"text", "email", "tel"}:
        return f"""            <label class="block">
              <span class="block text-sm font-medium mb-1">{_e(q.label)}</span>
              <input type="{q.type}" name="{_e(q.name)}"{req} class="w-full px-4 py-2 border border-gray-300 rounded">
            </label>
"""
    if q.type == "select":
        opts = "".join(f'<option value="{_e(o)}">{_e(o)}</option>' for o in (q.options or []))
        return f"""            <label class="block">
              <span class="block text-sm font-medium mb-1">{_e(q.label)}</span>
              <select name="{_e(q.name)}"{req} class="w-full px-4 py-2 border border-gray-300 rounded">
                <option value="">Select…</option>
                {opts}
              </select>
            </label>
"""
    if q.type == "radio":
        inputs = "".join(
            f'<label class="flex items-center gap-2 py-1"><input type="radio" name="{_e(q.name)}" value="{_e(o)}"{req}>{_e(o)}</label>'
            for o in (q.options or [])
        )
        return f"""            <fieldset class="border-0 p-0 m-0">
              <legend class="block text-sm font-medium mb-1">{_e(q.label)}</legend>
              {inputs}
            </fieldset>
"""
    if q.type == "checkbox":
        inputs = "".join(
            f'<label class="flex items-center gap-2 py-1"><input type="checkbox" name="{_e(q.name)}" value="{_e(o)}">{_e(o)}</label>'
            for o in (q.options or [])
        )
        return f"""            <fieldset class="border-0 p-0 m-0">
              <legend class="block text-sm font-medium mb-1">{_e(q.label)}</legend>
              {inputs}
            </fieldset>
"""
    # unreachable — QuestionType enum is closed
    return ""


def _otp_section(m: SurveyFunnelManifest) -> str:
    """OTP block rendered when manifest.otp_enabled is true. Posts to /api/verificationsms."""
    return f"""          <section class="otp space-y-4" hidden>
            <h2 class="text-2xl font-bold">Verify your mobile</h2>
            <p class="text-sm text-gray-600">We sent a 6-digit code to the mobile number you entered.</p>
            <label class="block">
              <span class="block text-sm font-medium mb-1">Verification code</span>
              <input type="text" name="otp_code" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required class="w-full px-4 py-2 border border-gray-300 rounded">
            </label>
            <div class="flex justify-between gap-2">
              <button type="button" data-action="otp-resend" class="px-4 py-2 border border-gray-300 rounded font-semibold" formaction="/api/verificationsms">Resend code</button>
              <button type="submit" class="ml-auto bg-[var(--color-primary)] hover:opacity-90 text-white px-6 py-3 rounded font-semibold">{_e(m.submit_label)}</button>
            </div>
          </section>
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
# Chat summary + slug helpers
# ---------------------------------------------------------------------------

def _build_chat_summary(
    brief: str,
    references: Optional[list[str]],
    m: SurveyFunnelManifest,
    stub: bool = False,
) -> str:
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
        parts.append("> Stub output — generated deterministically by Python (no chat session).\n")
    parts.extend([
        "## Output",
        f"- family: {m.family}",
        f"- slug: {m.slug}",
        f"- intent: {m.intent}",
        f"- steps: {len(m.steps)} ({', '.join(s.id for s in m.steps)})",
        f"- otp_enabled: {m.otp_enabled}",
        f"- submit_label: {m.submit_label}",
        f"- optional sections: {', '.join(m.optional_sections) or '(none)'}",
        f"- theme: primary={m.theme.color_primary}, accent={m.theme.color_accent}",
    ])
    return "\n".join(parts) + "\n"


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:50] or "untitled-funnel")
