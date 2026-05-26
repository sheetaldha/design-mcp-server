"""Shared instructions scaffold for design family briefs.

The caller's chat session reads the `instructions` string returned by
`design_landing_page` / `design_survey_funnel` and follows it like a runbook.
Both families share an adaptive, step-wise 6-step intake (acknowledge +
clarify -> outline -> generate -> preview -> iterate -> submit). Only the
clarifying fields, the per-family defaults, and the family-specific contract
notes differ.

Intake is ADAPTIVE and STEP-WISE: parse the brief, echo back what's filled in,
ask only missing fields ONE AT A TIME with a `*Question N of M*` prefix. A
speed-mode escape hatch ("just generate it") fills missing fields from the
family `DEFAULTS` dict and jumps to the outline.

Refer to the assistant in second person — the string is rendered into the
caller's session at run time.
"""

from __future__ import annotations

from typing import Optional


# Per-family defaults used by speed-mode and missing-answer fallback.
LANDING_PAGE_DEFAULTS: dict[str, str] = {
    "audience": "general consumers",
    "primary_cta": "Get started",
    "palette": "modern blue (#2563eb primary, slate text)",
    "benefits": "three differentiated value props",
    "tone": "friendly + professional",
    "references_to_avoid": "none stated",
}

SURVEY_FUNNEL_DEFAULTS: dict[str, str] = {
    "audience": "general consumers",
    "steps": "3 (situation, timeframe, contact details)",
    "otp": "skip OTP",
    "submit_label": "Get My Quotes",
    "post_submit": "thank-you on the same page",
    "palette": "modern blue (#2563eb primary, slate text)",
    "tone": "friendly + professional",
}


def render_brief(
    *,
    family_label: str,
    brief: str,
    slug_hint: str,
    references: Optional[list[str]],
    clarifying_fields: list[tuple[str, str]],
    family_contract_notes: str,
    defaults: dict[str, str],
    sanity_check_items: list[str],
) -> str:
    """Render the adaptive step-wise intake scaffold for a design family."""
    ref_block = ""
    if references:
        ref_block = "References:\n" + "\n".join(f"  - {r}" for r in references) + "\n\n"

    field_lines = "\n".join(f"  - {k}: {q}" for k, q in clarifying_fields)
    defaults_lines = "\n".join(f"  - {k}: {v}" for k, v in defaults.items())
    sanity_line = " · ".join(sanity_check_items)

    return f"""You are helping a teammate design a {family_label} microsite for Acquirely. Keep cognitive load LOW: parse what the user gave you, echo it back, and ask only what's missing — one question at a time, never a bundled list.

`design_id` (returned alongside) is the handle for every follow-up call (submit_design, update_design, cancel_design). Suggested slug: {slug_hint} (kebab-case; swap if the conversation suggests one).

The user's opening brief:
  "{brief}"

{ref_block}Six steps, in order. Do not skip ahead — and do not generate HTML before the user has approved a written outline.

STEP 1 — Acknowledge, parse, run an adaptive step-wise intake.

(a) Open with one or two warm sentences. No gushing.

(b) Parse the brief against the clarifying fields below. Echo back what IS filled in, in plain English. Example: "Got it — audience: Australian property sellers, CTA: book a free consultation, palette: navy, tone: premium + calm. Just need a few more answers."

(c) Offer the speed-mode escape hatch BEFORE the first question: "If you'd rather skip the questions and let me pick sensible defaults, say `just generate it` and I'll go straight to the outline."

(d) Ask only the MISSING fields, ONE AT A TIME. Never bundle them into a numbered list. Prefix every question with `*Question N of M*` (N = current number, M = total missing). Example: `*Question 2 of 4* — Brand colors: any in mind, or should I pick?` Wait for the reply before the next.

(e) If a reply answers more than one field, accept both, skip the answered one, lower M next time.

Clarifying fields (key — question if missing):
{field_lines}

Defaults (for speed-mode + any field left missing):
{defaults_lines}

Speed-mode triggers (any of): `just generate it`, `skip questions`, `use defaults`, `go ahead`, `you pick`, `surprise me`. On any of these: acknowledge with one line ("Skipping intake — using these defaults: …. Going to outline now."), fill missing fields from the defaults, and jump to STEP 2. The user still approves the outline before any HTML.

STEP 2 — Show a written outline. No HTML yet.
Cover: page <title> (70 char SEO cap), hero headline + subhead, section structure, CTA copy, palette + font (from font_allowlist). Ask: "Does this outline look right? Anything to change before I write the HTML?" Loop until sign-off. No HTML in this step.

STEP 3 — Generate.
Write the HTML and manifest against the `contract` and `manifest_schema`.
{family_contract_notes}
Keep <title> at or under 70 chars; mirror across <title>, og:title, twitter:title, JSON-LD name/headline. Walk the manifest through the schema; fix anything that would fail validation.

STEP 4 — Preview as a summary, not raw HTML.
6 to 10 bullets: title, hero headline, sections, palette, fonts, CTA / submit label, image strategy, lead-form / step fields. Append exactly one sanity-check line:
  *Sanity check: {sanity_line} ✓*
Offer: "say 'show me the html' if you want to see the full file before submit." Then ask: **Submit · Iterate · Scrap** — which one?

STEP 5 — Iterate.
On change requests: call update_design(design_id=<id>, instructions=<their feedback>). Regenerate HTML + manifest, loop back to STEP 4. "Looks good, maybe tighten the hero" is iteration, not approval. To scrap: call cancel_design(design_id=<id>, reason=<reason>) and stop.

STEP 6 — Submit only on an unambiguous yes.
Affirmatives that count: `yes`, `submit`, `ship it`, `go ahead`, `approved`, `looks good submit it`. A "looks good, maybe…" is still iteration. Call:
    submit_design(design_id=<id>, html=<full HTML>, manifest=<manifest dict>)
After submit, report: design_id, status, Bitbucket commit SHA, repo path, next step ("cd microsite-design-skills && git pull; UAT preview comes from the Orchestrator agent"). If ok=false, surface structured errors verbatim, fix, retry.

Tone: calm, plain English, second person. One question at a time, never a list."""
