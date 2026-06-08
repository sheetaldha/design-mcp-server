"""Shared instructions scaffold for design family briefs.

The caller's chat session reads the `instructions` string returned by
`start_landing_page_intake` / `start_survey_funnel_intake` and follows it like a runbook.
Both families share a 7-step intake (acknowledge + clarify -> outline ->
generate -> mandatory preview -> checklist -> iterate -> submit). Only the
clarifying fields, the per-family defaults, and the family-specific contract
notes differ.

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
    # Speed-mode defaults to the icon-only path so we never silently fabricate
    # Pexels / Unsplash URLs. Operators who want photos explicitly answer the
    # images_choice clarifying question.
    "images_choice": "No — clean modern look with icons + gradients only",
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
# Strict-script rendering — used by the Landing Page family to stop the
# caller's Claude from inventing / rephrasing clarifying questions or
# fabricating option sets that aren't in `_CLARIFYING_FIELDS`. The brief
# becomes a FIXED SCRIPT, not a soft guideline.
# ---------------------------------------------------------------------------

_STRICT_SCRIPT_PREAMBLE = """STRICT QUESTION SCRIPT — READ FIRST AND OBEY.

The clarifying questions below are a FIXED SCRIPT, not suggestions. Treat them like a deposition transcript: use them word-for-word, in order, no improvisation.

1. **DO NOT INVENT QUESTIONS.** Ask ONLY the fields listed below. If a field you wish existed isn't here, it isn't part of intake — skip it.
2. **DO NOT REPHRASE.** Use each field's question text VERBATIM as the AskUserQuestion question (or as the plain-text prompt). No "improving" the wording, no shortening, no softening.
3. **DO NOT INVENT OR REORDER OPTIONS.** For fields with a curated option list, pass those options to AskUserQuestion (claude.ai's multi-choice card UI) exactly as written, in the exact order listed. Do not add options, do not drop options, do not change wording. "Other" is added by claude.ai's UI as a free-text escape — you do not need to add it to the options array yourself.
4. **ASK ONE AT A TIME, IN ORDER.** Walk the field list top to bottom, prefixing each `*Q<n> of <M>*` (excluding already-answered + checkpoint pseudo-fields from M).
5. **SKIP-ANSWERED IS THE ONLY EXCEPTION.** If the user's brief or a prior reply already answers a field, echo it back as ✅ and move on — never re-ASK a question whose answer is already on the table.
6. **CHECKPOINT FIELDS ARE NOT QUESTIONS.** Fields marked CHECKPOINT render a ✅/❓ summary of collected + remaining answers and wait for confirmation. Do NOT present them via AskUserQuestion.
7. **CLARIFY ONLY AFTER ANSWER.** If a user's response to a defined question is unclear, you may ask ONE follow-up in your own words — but only after the original question has been answered or explicitly skipped. Never pre-emptively split a defined question into multiple ones.

Why this matters: operators are non-technical and rely on a predictable, repeatable flow. Each invented question or reworded option erodes trust and makes batches inconsistent. The script below is the contract.

IMAGE & ICON RULES — NEVER FABRICATE.

The server now owns image + icon sourcing. The prod bug that triggered this rule: Claude was hallucinating Unsplash URLs that resolved to irrelevant photos (e.g. football stadiums for "lead generation"). New contract:

1. **NEVER write inline `<svg>` markup for icons.** For every icon in the design, call `fetch_icons` (during initial HTML generation) or `search_icons` (during iteration when the user wants alternatives). Icons come from the Iconify / Lucide library as real SVG markup the server hands back.
2. **NEVER fabricate Pexels or Unsplash photo URLs.** For every `<img>` tag with an external photo, the URL must come from one of:
   - `search_stock_images` — call this during HTML generation, SHOW the returned candidates as an inline numbered markdown-image gallery in chat FIRST (so the user actually SEES each photo), THEN present an AskUserQuestion to pick one, and embed the user's pick verbatim;
   - URLs the user pasted in chat — reuse them verbatim, no edits to the URL.
3. **Allowed exceptions:** data URIs for tiny inline graphics (favicons, decorative gradients), CSS `background: linear-gradient(...)` blocks, logo URLs the user explicitly provided.
4. **Required attribution for Pexels images:** render `Photo by <photographer>` (link to `photographer_url`) inside the image's `alt` text AND include a footer fine-print line: `Photos via <a href="https://pexels.com">Pexels</a>`. The `attribution_note` field returned by `search_stock_images` repeats this rule.

IMAGE FLOW — driven by the `images_choice` clarifying answer:

- `images_choice` = "Yes — I'll paste image URLs in chat now"
  After STEP 1 intake finishes, BEFORE generating HTML, prompt the user verbatim: "Paste your image URLs in chat now. Tell me which slot each goes to (hero, card 1, card 2, etc). I'll wait for you to paste them before generating." Wait for the paste. Embed each URL verbatim in the matching slot. Do NOT auto-substitute or alter the URLs.

- `images_choice` = "Yes — search free Pexels stock photos for me"
  Per photo slot (hero, cards, sections): call `search_stock_images(query=<slot keyword>)`. AskUserQuestion options are TEXT-ONLY, so the user sees NO photo unless you render it in chat first. So, in this exact order:
  1. POST AN INLINE NUMBERED MARKDOWN-IMAGE GALLERY (its own message) using each candidate's `url_medium` (~350px thumbnail) as the image src, one per line, so the user SEES every photo: `1. ![Photo by {photographer}]({url_medium})` … (numbered, one line per candidate). Do NOT skip it, do NOT describe photos in words instead, do NOT use `url_large` here (that's the embed, not the thumbnail).
  2. THEN AskUserQuestion to pick one; labels reference the number + photographer (e.g. "1 — by {photographer}").
  3. Embed the pick's `url_large` + alt + photographer attribution into the slot.

- `images_choice` = "No — clean modern look with icons + gradients only"
  Generate HTML with SVG icons (always via `fetch_icons`) plus CSS gradient placeholders for any photo-shaped slots. NO `<img>` tags except a logo URL the user explicitly provided in clarifying answers.

"""


def _render_field_line_strict(cf: ClarifyingField, index: int) -> str:
    """Strict per-field rendering — spells out the EXACT AskUserQuestion payload.

    Used by families that opt into `strict_script=True`. Each field block names
    the question text and (where applicable) the options as VERBATIM so the
    caller's Claude can copy them straight into AskUserQuestion without
    paraphrasing or reordering.
    """
    if cf.is_checkpoint:
        return (
            f"Field {index} — {cf.key} (CHECKPOINT — not a question, do NOT use AskUserQuestion)\n"
            f"  Action: render a ✅/❓ checklist summary of every answer collected "
            f"so far + any remaining questions. Wait for user confirmation.\n"
            f"  Accept: \"looks good\" / \"confirmed\" / \"continue\" → proceed to next field.\n"
            f"  Accept: \"change X to Y\" → update the named field, re-show the summary.\n"
            f"  Accept: \"go back to Z\" → re-ask the named field.\n"
            f"  Prompt text (use VERBATIM as the lead-in line): \"{cf.question}\""
        )
    if cf.suggested_options:
        opts_block = "\n".join(f'    - "{o}"' for o in cf.suggested_options)
        return (
            f"Field {index} — {cf.key}\n"
            f"  Question text (use VERBATIM): \"{cf.question}\"\n"
            f"  Options (use VERBATIM, in this exact order — do NOT add, drop, reword, or reorder):\n"
            f"{opts_block}\n"
            f"  Tool: AskUserQuestion with the question text + option list above. "
            f"Do NOT invent additional options. (claude.ai's UI surfaces \"Other\" as a free-text escape automatically.)"
        )
    return (
        f"Field {index} — {cf.key}\n"
        f"  Question text (use VERBATIM): \"{cf.question}\"\n"
        f"  Options: none — free-text answer. Tool: plain-text prompt (NOT AskUserQuestion)."
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
    strict_script: bool = False,
) -> str:
    """Render the checklist-first step-wise intake scaffold for a design family.

    When `enable_brief_first_branching=True` the rendered STEP 1 routes on the
    user's `page_intent` answer (New / Enhancement / Replica), and STEP 2 runs
    a brief-first + skip-answered intake (the user uploads a brief once, then
    the caller skips any clarifying field whose answer is already covered).
    Used by the Landing Page family; Survey Funnel leaves it off and gets the
    classic flat intake described inline below.

    When `strict_script=True` the clarifying-field block is prefaced with a
    STRICT QUESTION SCRIPT preamble and each field is rendered with VERBATIM
    question text + option list. This stops the caller's Claude from inventing
    or rephrasing questions — the rendered brief becomes a fixed script rather
    than a soft guideline. Used by the Landing Page family.
    """
    ref_block = ""
    if references:
        ref_block = "References:\n" + "\n".join(f"  - {r}" for r in references) + "\n\n"

    if strict_script:
        field_lines = "\n\n".join(
            _render_field_line_strict(cf, i + 1)
            for i, cf in enumerate(clarifying_fields)
        )
        fields_header = _STRICT_SCRIPT_PREAMBLE + "Clarifying fields — STRICT SCRIPT (use each VERBATIM):"
    else:
        field_lines = "\n".join(_render_field_line(cf) for cf in clarifying_fields)
        fields_header = "Clarifying fields (key — question if missing):"

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

{ref_block}Seven steps, in order. No HTML before the user has approved a written outline.

{intake_block}

{fields_header}
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

### STEP 4: PREVIEW THE GENERATED PAGE — MANDATORY, NO EXCEPTIONS

After generating the HTML and before asking the user what to do next, you
MUST do BOTH of the following in the same message:

1. **Render the page inline in chat.** Use claude.ai's HTML preview rendering
   (the same mechanism that shows artifacts inline) so the user can see the
   design without taking any action. Do not summarize the page verbally
   instead of rendering — the user wants to SEE it, not read about it.

2. **Surface the browser-openable preview URL.** Call `get_preview_url(design_id)`
   immediately and include the returned URL in the same message. This gives
   the user a way to open the design on any device (mobile, tablet, second
   monitor) — critical for landing page work where the visual is the product.

The next message you send to the user MUST contain BOTH (1) the inline render
AND (2) the get_preview_url result. Only after that, on the FOLLOWING turn or
in the same message AFTER the preview, ask "Submit / Iterate / Scrap?"

Do NOT:
- Skip the inline render because "it's a long page" — show it anyway.
- Skip get_preview_url because of approval friction — the user explicitly
  asked for the link.
- Ask "want to see the page?" — just show it. The user wants zero friction.
- Render only and forget the URL — the user wants BOTH ways to see it.

STEP 5 — Preview as checklist, not raw HTML.
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

STEP 6 — Iterate.
update_design(design_id=<id>, instructions=<feedback>). Regenerate, loop to STEP 4 (mandatory inline render + get_preview_url) then STEP 5 with new checklist + fresh get_preview_url link, then ALWAYS append: `Any further improvements, or this looks final?` Never auto-exit; even on "looks good", probe once before STEP 7. Scrap: cancel_design(design_id=<id>, reason=<reason>).

STEP 7 — Submit guard.
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
