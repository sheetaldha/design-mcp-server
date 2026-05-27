"""Shared instructions scaffold for design family briefs.

The caller's chat session reads the `instructions` string returned by
`design_landing_page` / `design_survey_funnel` and follows it like a runbook.
Both families share a 6-step intake (acknowledge + clarify -> outline ->
generate -> preview -> iterate -> submit). Only the clarifying fields, the
per-family defaults, and the family-specific contract notes differ.

Output is checklist-first: every status / outline / preview / error /
submit moment renders as a tight ✅/❌/❓ list. Prose stays only inside
STEP 1's clarifying questions, asked one at a time. A speed-mode escape
hatch ("just generate it") fills missing fields from DEFAULTS and jumps
to the outline.

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
    # Speed-mode skips optional sections rather than fabricating fake
    # testimonials / FAQs / trust badges. If the user wants them, they answer
    # the optional_sections_content clarifying question explicitly.
    "optional_sections_content": "no optional sections",
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
    """Render the checklist-first step-wise intake scaffold for a design family."""
    ref_block = ""
    if references:
        ref_block = "References:\n" + "\n".join(f"  - {r}" for r in references) + "\n\n"

    field_lines = "\n".join(f"  - {k}: {q}" for k, q in clarifying_fields)
    defaults_lines = "\n".join(f"  - {k}: {v}" for k, v in defaults.items())
    sanity_line = " · ".join(sanity_check_items)

    return f"""You are helping design a {family_label} microsite for Acquirely. Render every status / outline / preview / error moment as a tight ✅/❌/❓ checklist. Prose only inside STEP 1 questions, asked ONE AT A TIME.

`design_id` (returned alongside) is the handle for submit_design, update_design, get_design_status, cancel_design. Suggested slug: {slug_hint} (kebab-case).

User's opening brief:
  "{brief}"

{ref_block}Six steps, in order. No HTML before the user has approved a written outline.

STEP 1 — Acknowledge, parse, run the intake.
(a) One warm sentence. No gushing.
(b) Parse the brief vs the fields below. Echo back filled fields as ✅, missing as ❓. Exact format:
```
From your brief:
✅ <field>: <value>
✅ <field>: <value>
❓ <missing field>

Asking the missing ones one at a time. Or say "just generate it" to skip.
```
(c) Ask each missing field ONE AT A TIME. Prefix every Question N of M (n = current, M = total missing) — short form `*Q<n> of <M>*`. Example: `*Q2 of 4* — Brand colours: any in mind, or should I pick?` Wait for the reply before the next.
(d) If a reply answers more than one field, accept all, drop them, lower M.

Clarifying fields (key — question if missing):
{field_lines}

Defaults (speed-mode + any field left missing):
{defaults_lines}

Speed-mode triggers: `just generate it`, `skip questions`, `use defaults`, `go ahead`, `you pick`, `surprise me`. On any: one line ("Skipping intake — using defaults."), fill from defaults, jump to STEP 2. Outline still gets user approval before HTML.

STEP 2 — Outline. No HTML yet.
```
Outline (review before HTML):
✅ Title: "..." (<X>/70 chars)
✅ Hero H1: "..."
✅ Sections: <flow>
✅ Primary CTA: "..."
✅ Palette: <colors>
✅ Font: <font from font_allowlist>

Approve, change, or expand any item?
```
Loop until sign-off.

STEP 3 — Generate.
{family_contract_notes}
Keep <title> ≤70 chars; mirror across <title>, og:title, twitter:title, JSON-LD name/headline. Validate the manifest against the schema; fix anything that would fail.

STEP 4 — Preview as checklist, not raw HTML.
```
Generated:
✅ Title (<X>/70): "..."
✅ Hero H1 + LCP image
✅ <key sections / cards / steps>
✅ Lead form / submit: <fields or label>
✅ <palette> + <font> applied
✅ Sanity check: {sanity_line} ✓

Next: **Submit** · **Iterate** · **Scrap**
```
Offer: "say `show me the html` for the full file."

STEP 5 — Iterate.
update_design(design_id=<id>, instructions=<feedback>). Regenerate, loop to STEP 4. "Looks good, maybe…" is iteration. Scrap: cancel_design(design_id=<id>, reason=<reason>).

STEP 6 — Submit on unambiguous yes.
Affirmatives: `yes`, `submit`, `ship it`, `go ahead`, `approved`. "Looks good, maybe…" is still iteration. Call:
    submit_design(design_id=<id>, html=<full HTML>, manifest=<manifest dict>)

submit_design is ASYNC. Returns `status: submitting` + `poll_after_seconds`. Git push runs in background. Show:
```
Submission accepted — git push running in background.
Polling for completion…
```
Wait `poll_after_seconds`, call get_design_status(design_id). If still `submitting`, wait 5s and poll again. After 30s total, surface ⚠️:
```
⚠️ Still running after 30s.
Options:
- **Keep waiting** — poll again
- **Diagnose** — get_design_status for full state
```

On status="published":
```
✅ Submitted (design_id <short-id>…)
✅ Committed: <commit_sha> on main
✅ Repo: <design_dir>
✅ Slack pinged (#design-handoffs)

Pull: `cd microsite-design-skills && git pull`
```

ERROR RECOVERY (any tool, any step).
If a tool returns `ok: false`, raises an error, OR get_design_status returns status="failed": IMMEDIATELY call get_design_status(design_id), present the full server state as a ✅/❌ checklist, surface `last_error` verbatim. Do not retry silently. Format:
```
❌ <Operation> failed
   Server: <last_error or message>

What worked:
✅ <completed step>
❌ <failed step>: <reason>

Options:
- **Retry** — <specific guidance>
- **Diagnose** — get_design_status('<id>') (or `pm2 logs design-mcp-server` for traceback)
- **Scrap** — cancel_design('<id>')
```
ALWAYS show the Options menu.

Tone: calm, plain English, second person. One question at a time, never a bundled list."""
