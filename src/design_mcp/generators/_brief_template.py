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

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ClarifyingField:
    """A single clarifying question slot in a design-family intake.

    `suggested_options` carries a curated short-list of answers the caller
    should surface via claude.ai's built-in `AskUserQuestion` multi-choice
    card UI. Leave it as None for free-form text fields where the answer
    space is too varied to pre-enumerate.

    `is_checkpoint=True` flags a pseudo-field that does NOT collect data — it
    asks the user to confirm a summary of everything collected so far before
    the intake continues. The brief template renders these differently
    (summary + confirm/change/back-up prompt) and skips them entirely from
    the "missing field" counting used for `*Q N of M*` progress markers.
    """

    key: str
    question: str
    suggested_options: Optional[tuple[str, ...]] = None
    is_checkpoint: bool = False


def field(
    key: str,
    question: str,
    *options: str,
    is_checkpoint: bool = False,
) -> ClarifyingField:
    """Convenience constructor — `field("k", "q?", "A", "B")` vs the dataclass form.

    Pass `is_checkpoint=True` for a summary-and-confirm pseudo-field that
    doesn't take an answer but pauses the intake for user review.
    """
    return ClarifyingField(
        key=key,
        question=question,
        suggested_options=tuple(options) if options else None,
        is_checkpoint=is_checkpoint,
    )


# Per-family defaults used by speed-mode and missing-answer fallback.
LANDING_PAGE_DEFAULTS: dict[str, str] = {
    "page_intent": "New microsite landing page",
    "site_name": "Acquirely",
    "site_brief": "(none provided — full intake will run)",
    "primary_cta": "Get started",
    "palette": "modern blue (#2563eb primary, slate text)",
    "benefits": "three differentiated value props",
    "tone": "friendly + professional",
    "gtm_tag": "(none — skip GTM embed)",
    "references_to_avoid": "none stated",
    # Speed-mode skips optional sections rather than fabricating fake
    # testimonials / FAQs / trust badges. If the user wants them, they answer
    # the optional_sections_content clarifying question explicitly.
    "optional_sections_content": "no optional sections",
}

SURVEY_FUNNEL_DEFAULTS: dict[str, str] = {
    "vertical": "Other",
    "audience": "general consumers",
    "site_name": "Acquirely",
    "steps": "3 (situation, timeframe, contact details)",
    "otp": "skip OTP",
    "submit_label": "Get My Quotes",
    "post_submit": "thank-you on the same page",
    "palette": "modern blue (#2563eb primary, slate text)",
    "tone": "friendly + professional",
}


def _render_field_line(cf: ClarifyingField) -> str:
    """Format one clarifying-field bullet for STEP 1 of the brief.

    Fields with `suggested_options` direct the caller to use claude.ai's
    AskUserQuestion multi-choice card UI with the curated option set (plus
    an always-on "Other" free-text escape). Free-form fields render as plain
    text questions. Checkpoint pseudo-fields render the summary-and-confirm
    rubric instead.
    """
    if cf.is_checkpoint:
        return (
            f"  - {cf.key} (CHECKPOINT — pseudo-field, do NOT ask for data): {cf.question}\n"
            f"      Render the summary-and-confirm rubric from STEP 1 (c) point 4."
        )
    if cf.suggested_options:
        opts = ", ".join(f'"{o}"' for o in cf.suggested_options)
        return (
            f"  - {cf.key}: {cf.question}\n"
            f"      When asking {cf.key}, prefer AskUserQuestion (multi-choice card UI) "
            f"with these options: [{opts}]. Always offer \"Other\" as a free-text escape."
        )
    return (
        f"  - {cf.key}: {cf.question}\n"
        f"      Ask {cf.key} as plain text — too varied for multi-choice."
    )


# ---------------------------------------------------------------------------
# Intake STEP-1 blocks — two variants. Both are inlined into the rendered
# brief by render_brief(); only the wording inside STEP 1 differs.
# ---------------------------------------------------------------------------

_CLASSIC_INTAKE_BLOCK = """STEP 1 — Acknowledge, parse, run the intake.
For fields with suggested_options, prefer the AskUserQuestion tool (claude.ai's native multi-choice card UI). For free-form fields, use plain text questions. Don't bundle multiple fields into one question — one at a time.
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
(d) If a reply answers more than one field, accept all, drop them, lower M."""


_BRIEF_FIRST_INTAKE_BLOCK = """STEP 1 — Acknowledge, scope FIRST (page_intent routes the rest).
AskUserQuestion for suggested_options fields, plain text otherwise. One at a time.
(a) One warm sentence. No gushing.
(b) Ask `page_intent` IMMEDIATELY. Branch on the answer:
  - "New microsite landing page" → brief-first skip-answered intake in (c).
  - "Enhancement to an existing landing page" → ask page URL, call `fetch_url_screenshots(url)` (3 images @ mobile/iPad/desktop) for visual context, skip brand/tone/palette/benefits, ask ONLY: what changes + new offer/CTA, jump to STEP 3.
  - "Replica of an existing landing page" → ask URL or pasted HTML; if URL, call `fetch_url_screenshots(url)`. Clone structure + copy, ask for minor edits, jump to STEP 3.

(c) BRIEF-FIRST + SKIP-ANSWERED (page_intent = "New microsite landing page"):
  1. Ask `site_brief` SECOND. Tell user: paste images/copy/wireframes/reference URLs — more shared = fewer questions.
  2. Parse brief against every remaining field. Already answered → skip; quote as ✅ (e.g. `✅ Palette: teal (from brief)`). Still missing → ask one at a time, prefix each Question N of M — short form `*Q<n> of <M>*` (M = remaining-unanswered, excluding already-answered + checkpoint pseudo-fields).
  3. Echo back format:
```
From your brief:
✅ <field>: <value already answered>
❓ <field still missing>

Asking the remaining ones one at a time. Or "just generate it" to skip.
```
  4. CHECKPOINT (`is_checkpoint=True`): render ✅/❓ summary of collected + remaining. Wait for confirmation. "looks good"/"confirmed"/"continue" → proceed. "change X to Y" → update + re-show. "go back to Z" → re-ask.
(d) If one reply answers several fields, accept all, lower M."""


def render_brief(
    *,
    family_label: str,
    brief: str,
    slug_hint: str,
    references: Optional[list[str]],
    clarifying_fields: list[ClarifyingField],
    family_contract_notes: str,
    defaults: dict[str, str],
    sanity_check_items: list[str],
    enable_brief_first_branching: bool = False,
) -> str:
    """Render the checklist-first step-wise intake scaffold for a design family.

    When `enable_brief_first_branching=True` the rendered STEP 1 routes on the
    user's `page_intent` answer (New / Enhancement / Replica), and STEP 2 runs
    a brief-first + skip-answered intake (the user uploads a brief once, then
    the caller skips any clarifying field whose answer is already covered).
    Used by the Landing Page family; Survey Funnel leaves it off and gets the
    classic flat intake described inline below.
    """
    ref_block = ""
    if references:
        ref_block = "References:\n" + "\n".join(f"  - {r}" for r in references) + "\n\n"

    field_lines = "\n".join(_render_field_line(cf) for cf in clarifying_fields)
    defaults_lines = "\n".join(f"  - {k}: {v}" for k, v in defaults.items())
    sanity_line = " · ".join(sanity_check_items)

    intake_block = (
        _BRIEF_FIRST_INTAKE_BLOCK
        if enable_brief_first_branching
        else _CLASSIC_INTAKE_BLOCK
    )

    return f"""You are helping design a {family_label} microsite for Acquirely. Render every status / outline / preview / error moment as a tight ✅/❌/❓ checklist. Prose only inside STEP 1 questions, asked ONE AT A TIME.

`design_id` (returned alongside) is the handle for submit_design, update_design, get_design_status, cancel_design, get_preview_url. Suggested slug: {slug_hint} (kebab-case).

User's opening brief:
  "{brief}"

{ref_block}Six steps, in order. No HTML before the user has approved a written outline.

{intake_block}

Clarifying fields (key — question if missing):
{field_lines}

Defaults (speed-mode + any field left missing):
{defaults_lines}

Speed-mode triggers: `just generate it`, `skip questions`, `use defaults`, `go ahead`, `you pick`, `surprise me`. On any: one line ("Skipping intake — using defaults."), fill from defaults, jump to STEP 2 (Outline). Outline still gets user approval before HTML.

STEP 2 — Outline. No HTML yet.
```
Outline (review before HTML):
✅ Title: "..." (<X>/60 chars, bare — site_name appended at render)
✅ Site name: "..." (3-50 chars)
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
Keep `seo.title` bare and ≤60 chars on the manifest. Rendered `<title>` MUST be `{{title}} | {{site_name}}` (suffix only here — rendered title lands ≤75 chars). og:title, twitter:title, JSON-LD `name`/`headline` stay BARE (no suffix). Also emit `<meta property="og:url" content="{{canonical_url}}">` and include `"url": "{{canonical_url}}"` in the JSON-LD WebPage object alongside `name` and `description`. Lead form posts to `/api/add-lead`. Validate the manifest against the schema; fix anything that would fail.

STEP 4 — Preview as checklist, not raw HTML.
```
Generated:
✅ Title (<X>/60 bare): "..." | site_name="..."
✅ Hero H1 + LCP image
✅ <key sections / cards / steps>
✅ Lead form / submit: <fields or label>
✅ <palette> + <font> applied
✅ Sanity check: {sanity_line} ✓

📄 **Preview in your browser:** Call get_preview_url(design_id), open the link. Works on mobile.

Next: **Submit** · **Iterate** · **Scrap**. Or say "show me the html" to paste inline.
```
Preview needs html persisted: run submit_design(..., publish=False) first, then get_preview_url.

STEP 5 — Iterate.
update_design(design_id=<id>, instructions=<feedback>). Regenerate, loop to STEP 4 with new checklist + fresh get_preview_url link, then ALWAYS append: `Any further improvements, or this looks final?` Never auto-exit; even on "looks good", probe once before STEP 6. Scrap: cancel_design(design_id=<id>, reason=<reason>).

STEP 6 — Submit guard.
Affirmatives: `yes`, `submit`, `ship it`, `go ahead`, `approved`. Before submit_design(publish=True): if the user has NEITHER seen a get_preview_url result earlier NOR said they viewed it elsewhere, respond verbatim:
```
⚠️ Hold on — you haven't viewed the actual HTML yet.

Options:
- **Open in browser** — I'll call get_preview_url and paste the link (recommended)
- **Show inline** — I'll paste the full HTML in chat
- **Submit anyway** — only if you've reviewed it elsewhere

Which?
```
Only proceed after EXPLICIT viewed-confirmation OR "submit anyway". Then call:
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
The preview link stays live for the next hour if you want to share it.
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
