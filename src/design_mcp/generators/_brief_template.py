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
from typing import Literal, Optional

# Requirement level for a clarifying field — drives the completeness gate.
#   "required"    — must be provided; a skip / "Not required" reply is REJECTED.
#   "conditional" — must be provided OR explicitly confirmed "Not required"
#                   (a silent skip is not enough — the user has to say so).
#   "optional"    — silent skip is fine.
Requirement = Literal["required", "conditional", "optional"]


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

    `agent_hint` carries a directive to the CALLER'S Claude — NOT user-facing
    copy. It tells Claude how to resolve the field before asking (e.g. "derive
    from the brief if obvious; otherwise ask"). The caller must act on it and
    must NEVER render it into the user-facing question. Keeping it out of
    `question` stops the instruction leaking into AskUserQuestion / chat.

    `requirement` sets the completeness level (see ``Requirement``):
    "required" fields block intake until provided (a skip is rejected),
    "conditional" fields must be provided or explicitly confirmed "Not
    required", and "optional" fields may be skipped silently. Checkpoints
    are always treated as optional regardless of this field.
    """

    key: str
    question: str
    suggested_options: Optional[tuple[str, ...]] = None
    is_checkpoint: bool = False
    agent_hint: Optional[str] = None
    requirement: Requirement = "optional"


def field(
    key: str,
    question: str,
    *options: str,
    is_checkpoint: bool = False,
    agent_hint: Optional[str] = None,
    requirement: Requirement = "optional",
) -> ClarifyingField:
    """Convenience constructor — `field("k", "q?", "A", "B")` vs the dataclass form.

    Pass `is_checkpoint=True` for a summary-and-confirm pseudo-field that
    doesn't take an answer but pauses the intake for user review.

    Pass `agent_hint="..."` for an agent-only directive (how to resolve the
    field before asking) that must NOT be shown to the user — see
    `ClarifyingField`.

    Pass `requirement="required" | "conditional"` to raise the field above the
    default "optional" — see `ClarifyingField` / `Requirement`.
    """
    return ClarifyingField(
        key=key,
        question=question,
        suggested_options=tuple(options) if options else None,
        is_checkpoint=is_checkpoint,
        agent_hint=agent_hint,
        requirement=requirement,
    )


# Per-family defaults used by speed-mode and missing-answer fallback. Note:
# speed-mode fills these as real VALUES (not skips), so even a "required" field
# is satisfied; conditional fields get an explicit "no …" confirmation. The
# manifest-level gate still enforces the production-critical blocks at submit.
LANDING_PAGE_DEFAULTS: dict[str, str] = {
    "page_intent": "New microsite landing page",
    "site_brief": "(none provided — full intake will run)",
    "reference_layout": "not required — design fresh",
    "page_path": "/landing",
    "site_name": "Acquirely",
    "content_copy": "you write it",
    "primary_cta": "Get started",
    # Speed-mode defaults to the icon-only path so we never silently fabricate
    # Pexels / Unsplash URLs. Operators who want photos explicitly answer the
    # images_choice clarifying question.
    "images_choice": "No — clean modern look with icons + gradients only",
    "palette": "modern blue (#2563eb primary, slate text)",
    "integrations": "no integration — generic store only",
    "tracking": "no tracking",
    "benefits": "three differentiated value props",
    "tone": "friendly + professional",
    "references_to_avoid": "none stated",
    # Speed-mode skips optional sections rather than fabricating fake
    # testimonials / FAQs / trust badges. If the user wants them, they answer
    # the optional_sections_content clarifying question explicitly.
    "optional_sections_content": "no optional sections",
}

SURVEY_FUNNEL_DEFAULTS: dict[str, str] = {
    "vertical": "Other",
    "site_brief": "(none provided — full intake will run)",
    "reference_layout": "not required — design fresh",
    "page_path": "/funnel",
    "audience": "general consumers",
    "site_name": "Acquirely",
    "steps": "3 (situation, timeframe, contact details)",
    "question_dependencies": "no dependencies",
    "dnq_rules": "no DNQ rules",
    "otp": "skip OTP",
    "submit_label": "Get My Quotes",
    "post_submit": "thank-you on the same page",
    # Mirrors LANDING_PAGE_DEFAULTS: speed-mode defaults to the icon/gradient
    # path so we never silently fabricate Pexels / Unsplash URLs. Operators who
    # want photos answer the images_choice clarifying question explicitly.
    "images_choice": "No — clean modern look with icons + gradients only",
    "palette": "modern blue (#2563eb primary, slate text)",
    "tone": "friendly + professional",
    "integrations": "no integration — generic store only",
    "tracking": "no tracking",
}


# Shared short directive for the server-driven intake (both families). Tells
# the caller's Claude HOW to drive the state machine: review the brief first,
# auto-fill what it can, ask only gaps, honour requirement levels + agent_hint.
# Single source of truth — both landing_page and survey_funnel re-export it.
INSTRUCTIONS_SHORT = (
    "INTAKE FLOW: This server controls the clarifying-question flow. It is "
    "REVIEW-FIRST — parse the user's brief, auto-fill every field you can from "
    "it (call submit_clarifying_answer with the derived value), echo back what "
    "you filled (✅) vs what's still missing (❓), and only ASK for the gaps. "
    "Do NOT ask the user to resend things already in the brief. For each "
    "`next_question` returned:\n"
    "- If `is_checkpoint=false`: call AskUserQuestion with `question_text` and "
    "`options` EXACTLY as given (verbatim, in order, no rephrasing). If "
    "`options` is null, ask as plain text.\n"
    "- If `is_checkpoint=true`: render the `checkpoint_payload` as a ✅/❓ "
    "summary message in chat (NOT AskUserQuestion). Wait for user reply.\n"
    "- `requirement` sets strictness:\n"
    "    • \"required\" — the user MUST give a real value; never submit an "
    "empty / 'skip' / 'Not required' answer (the server rejects it). Where the "
    "question allows it, \"you write it\" delegation counts as a real answer.\n"
    "    • \"conditional\" — if it doesn't apply, the user must EXPLICITLY "
    "confirm 'Not required' (submit that verbatim); never silently skip. These "
    "are the load-bearing integration / tracking / DNQ / dependency gates — be "
    "strict, a missing one can break the system.\n"
    "    • \"optional\" — a silent skip is fine.\n"
    "- If `agent_hint` is non-null: it is an AGENT-ONLY directive — act on it "
    "BEFORE asking and NEVER render it to the user. Resolve from the brief / "
    "prior context first; if you can, submit that value and skip the question.\n"
    "\n"
    "After the user answers, call "
    "`submit_clarifying_answer(design_id, field_key, answer)`. The response "
    "includes the NEXT `next_question`, or `null` when intake is complete. If "
    "it returns ok:false with a 'REQUIRED' error, that field can't be skipped — "
    "ask for a real value and resubmit.\n"
    "\n"
    "When `next_question` is `null` the brief is confirmed — summarise the "
    "confirmed brief, then proceed to STEP 2 (outline) per the existing "
    "contract (see `instructions_legacy` for the full runbook). Do NOT generate "
    "the HTML/MDF until intake is complete."
)


def _render_field_line(cf: ClarifyingField) -> str:
    """Format one clarifying-field bullet for STEP 1 of the brief.

    Fields with `suggested_options` direct the caller to use claude.ai's
    AskUserQuestion multi-choice card UI with the curated option set (plus
    an always-on "Other" free-text escape). Free-form fields render as plain
    text questions. Checkpoint pseudo-fields render the summary-and-confirm
    rubric instead.
    """
    hint_line = (
        f"\n      AGENT HINT (do NOT show the user — act on it first): {cf.agent_hint}"
        if cf.agent_hint
        else ""
    )
    if cf.is_checkpoint:
        return (
            f"  - {cf.key} (CHECKPOINT — pseudo-field, do NOT ask for data): {cf.question}\n"
            f"      Render the summary-and-confirm rubric from STEP 1 (c) point 4."
            f"{hint_line}"
        )
    if cf.suggested_options:
        opts = ", ".join(f'"{o}"' for o in cf.suggested_options)
        return (
            f"  - {cf.key}: {cf.question}\n"
            f"      When asking {cf.key}, prefer AskUserQuestion (multi-choice card UI) "
            f"with these options: [{opts}]. Always offer \"Other\" as a free-text escape."
            f"{hint_line}"
        )
    return (
        f"  - {cf.key}: {cf.question}\n"
        f"      Ask {cf.key} as plain text — too varied for multi-choice."
        f"{hint_line}"
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

"""


# ---------------------------------------------------------------------------
# REQUIRED-INPUTS COMPLETENESS GATE — the operator's review-first rules,
# rendered into the brief so the caller reviews the brief, asks ONLY for gaps,
# and never generates until every required/applicable input is confirmed. The
# server enforces the same gate at submit_design; this is the in-chat half.
# ---------------------------------------------------------------------------

_COMPLETENESS_GATE_BLOCK = """REQUIRED-INPUTS COMPLETENESS GATE — review first, then generate.

Before generating ANY HTML / MDF file, REVIEW the uploaded brief and confirm whether all required inputs are present. Required inputs:
- Sample template or reference layout
- URL or landing page path
- Content / copy for the page
- Images or image instructions
- Survey form questions (survey funnel)
- Question dependencies, if any (survey funnel)
- Integration details, if required: client name · API documentation · Google Sheet details · SFTP details · any other delivery/integration method
- Tracking pixels or tracking scripts
- Colour palette / brand colours
- DNQ points in the survey, if any (survey funnel)

Rules:
- Do NOT ask the user to resend everything.
- Only ask for the SPECIFIC missing items.
- If an item is not applicable, ask the user to confirm "Not required".
- Do NOT generate the final HTML/MDF file until all required or applicable items are confirmed.
- Once all required inputs are available, SUMMARISE the confirmed brief and then generate the page.
- BE STRICT: a missing integration or DNQ detail can break the system — check thoroughly.

The server enforces this too: submit_design is REJECTED until the clarifying intake is complete (every required field answered, every conditional provided or confirmed "Not required").

"""


# ---------------------------------------------------------------------------
# IMAGE & ICON RULES + IMAGE FLOW — SINGLE SOURCE OF TRUTH for the
# server-driven stock-image / icon flow. Family-agnostic: slot examples name
# "hero, cards/steps, sections" so it reads correctly for both Landing Page
# (hero + 3 cards) and Survey Funnel (hero + steps + optional results/CTA).
# Rendered into a family's brief whenever `enable_image_flow=True` (which
# `strict_script=True` implies). Do NOT duplicate this prose anywhere else —
# both families pull from this one constant so attribution / anti-fabrication
# rules stay identical.
# ---------------------------------------------------------------------------

_IMAGE_ICON_RULES_BLOCK = """IMAGE & ICON RULES — NEVER FABRICATE.

The server now owns image + icon sourcing. The prod bug that triggered this rule: Claude was hallucinating Unsplash URLs that resolved to irrelevant photos (e.g. football stadiums for "lead generation"). New contract:

1. **NEVER write inline `<svg>` markup for icons.** For every icon in the design, call `fetch_icons` (during initial HTML generation) or `search_icons` (during iteration when the user wants alternatives). Icons come from the Iconify / Lucide library as real SVG markup the server hands back.
2. **NEVER fabricate Pexels or Unsplash photo URLs.** For every `<img>` tag with an external photo, the URL must come from one of:
   - `search_stock_images` — call this during HTML generation, SHOW the returned candidates as an inline numbered markdown-image gallery in chat FIRST (so the user actually SEES each photo), THEN present an AskUserQuestion to pick one, and embed the user's pick verbatim;
   - URLs the user pasted in chat — reuse them verbatim, no edits to the URL.
3. **Allowed exceptions:** data URIs for tiny inline graphics (favicons, decorative gradients), CSS `background: linear-gradient(...)` blocks, logo URLs the user explicitly provided.
4. **Required attribution (per provider — every photo has a `provider` tag):** Pexels → `Photo by <photographer>` (linked to `photographer_url`) + footer line `Photos via <a href="https://pexels.com">Pexels</a>`. Unsplash → `Photo by <a href="{photographer_url}">{photographer}</a> on <a href="{source}">Unsplash</a>` (photographer_url + source already carry the UTM params Unsplash requires). The per-source `attribution_note` from `search_stock_images` spells out the exact rule.

IMAGE FLOW — driven by the `images_choice` clarifying answer:

- `images_choice` = "Yes — I'll paste image URLs in chat now"
  After STEP 1 intake finishes, BEFORE generating HTML, prompt the user verbatim: "Paste your image URLs in chat now. Tell me which slot each goes to (hero, card 1 / step 1, results, etc). I'll wait for you to paste them before generating." Wait for the paste. Embed each URL verbatim in the matching slot. Do NOT auto-substitute or alter the URLs.

- `images_choice` = "Yes — search free stock photos (Pexels + Unsplash) for me"
  Per photo slot (hero, cards/steps, sections, results/CTA): call `search_stock_images(query=<slot keyword>, source="both")` to pull from BOTH Pexels + Unsplash. AskUserQuestion options are TEXT-ONLY, so the user sees NO photo unless you render it in chat first. So, in this exact order:
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
    hint_line = (
        f"\n  AGENT HINT (NOT user-facing — resolve from context first, ask only if unresolved; "
        f"never render this line): {cf.agent_hint}"
        if cf.agent_hint
        else ""
    )
    req_line = ""
    if cf.requirement == "required":
        req_line = "\n  [REQUIRED — a skip / \"Not required\" reply is rejected; need a real value]"
    elif cf.requirement == "conditional":
        req_line = "\n  [CONDITIONAL — provide a value OR have the user confirm \"Not required\"; never silently skip]"
    if cf.is_checkpoint:
        return (
            f"Field {index} — {cf.key} (CHECKPOINT — not a question, do NOT use AskUserQuestion)\n"
            f"  Action: render a ✅/❓ checklist summary of every answer collected "
            f"so far + any remaining questions. Wait for user confirmation.\n"
            f"  Accept: \"looks good\" / \"confirmed\" / \"continue\" → proceed to next field.\n"
            f"  Accept: \"change X to Y\" → update the named field, re-show the summary.\n"
            f"  Accept: \"go back to Z\" → re-ask the named field.\n"
            f"  Prompt text (use VERBATIM as the lead-in line): \"{cf.question}\"{hint_line}"
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
            f"{req_line}{hint_line}"
        )
    return (
        f"Field {index} — {cf.key}\n"
        f"  Question text (use VERBATIM): \"{cf.question}\"\n"
        f"  Options: none — free-text answer. Tool: plain-text prompt (NOT AskUserQuestion)."
        f"{req_line}{hint_line}"
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
    enable_image_flow: bool = False,
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

    When `enable_image_flow=True` (which `strict_script=True` implies) the
    shared IMAGE & ICON RULES + IMAGE FLOW block is rendered into the brief so
    the family's `images_choice` clarifying answer drives the server-owned
    stock-image / icon sourcing flow (anti-fabrication + inline gallery +
    per-provider attribution). Both Landing Page and Survey Funnel pull this
    from the single `_IMAGE_ICON_RULES_BLOCK` constant — never duplicated.
    """
    ref_block = ""
    if references:
        ref_block = "References:\n" + "\n".join(f"  - {r}" for r in references) + "\n\n"

    # strict_script implies the image flow; either flag turns the shared block on.
    image_block = _IMAGE_ICON_RULES_BLOCK if (enable_image_flow or strict_script) else ""

    # The review-first completeness gate (operator's Rules) rides with the
    # strict script — both families use it.
    gate_block = _COMPLETENESS_GATE_BLOCK if strict_script else ""

    if strict_script:
        field_lines = "\n\n".join(
            _render_field_line_strict(cf, i + 1)
            for i, cf in enumerate(clarifying_fields)
        )
        fields_header = (
            _STRICT_SCRIPT_PREAMBLE + image_block
            + "Clarifying fields — STRICT SCRIPT (use each VERBATIM):"
        )
    else:
        field_lines = "\n".join(_render_field_line(cf) for cf in clarifying_fields)
        fields_header = image_block + "Clarifying fields (key — question if missing):"

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

{gate_block}{intake_block}

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
