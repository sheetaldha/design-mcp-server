"""Server-driven state machine for clarifying-question intake.

Pure-Python module — no IO, no DB calls, no Claude. The state lives in a
plain ``dict`` (the ``clarifying_state`` jsonb column on
``design_mcp_drafts``) and the field list is injected by the caller. The
server (``server.py``) owns DB IO and wires this module to the live draft.

Why this exists
---------------
Previously the server returned a long prose ``instructions`` blob that
embedded the clarifying questions. The caller's Claude was supposed to
ask them VERBATIM — but in practice it improvised: different wording,
reordered options, sometimes silently dropping fields entirely. Strict
preambles helped, but the instructions are guidance, not a contract.

This module flips the model: the server decides which question is next,
and the caller is reduced to a question-asker. Claude calls
``submit_clarifying_answer`` after each user reply; the server records the
answer and returns the next question (or ``None`` when intake is
complete). The question text + options are passed VERBATIM through a
structured ``NextQuestion`` dataclass, leaving no room for paraphrase.

State payload contract
----------------------
::

    {
      "current_field_index": int,   # 0-based pointer into the field list
      "collected": {                # field_key -> recorded answer
        "page_intent": "...",
        ...
      },
      "skipped": [str, ...],        # OPTIONAL fields the user silently skipped
      "not_required": [str, ...],   # CONDITIONAL fields explicitly confirmed N/A
      "checkpoint_state": str | None,   # "pending" | "confirmed"; only meaningful
                                        # while the current field is a checkpoint
    }

``skipped`` and ``not_required`` both mark a field "handled" so traversal
walks past it, but they mean different things: ``skipped`` is a silent skip
of an *optional* field, while ``not_required`` is an *explicit* "Not
required" confirmation of a *conditional* field (the strict gate the brief
demands for integrations / tracking / DNQ). A *required* field can be
neither — answering it with a skip/"Not required" reply raises ValueError.

A missing key is treated as the empty/initial value (``0`` / ``{}`` /
``[]`` / ``None``) so a freshly-created draft can start with
``clarifying_state = {}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Optional

from .generators._brief_template import ClarifyingField


# Phrases that advance a checkpoint. Case-insensitive substring match.
_CHECKPOINT_ADVANCE_PHRASES = (
    "looks good",
    "looks ok",
    "looks great",
    "confirmed",
    "confirm",
    "continue",
    "proceed",
    "all good",
    "ship it",
    "yes",
)

# Regex for "change <field_key> to <new value>" — captures the field_key
# and the value. Case-insensitive on the verb only.
_CHECKPOINT_CHANGE_RE = re.compile(
    r"^\s*change\s+([a-z_][a-z0-9_]*)\s+to\s+(.+?)\s*$",
    re.IGNORECASE,
)

# Regex for "go back to <field_key>".
_CHECKPOINT_REWIND_RE = re.compile(
    r"^\s*go\s+back\s+to\s+([a-z_][a-z0-9_]*)\s*$",
    re.IGNORECASE,
)

# Standard instruction string for AskUserQuestion fields — passed back to
# Claude as `instruction_for_claude` so there's no temptation to paraphrase.
_ASK_USER_QUESTION_INSTRUCTION = (
    "Use claude.ai's AskUserQuestion tool. question_text and options must "
    "be passed VERBATIM, in this exact order. Do not invent, rephrase, or "
    "reorder."
)

_PLAIN_TEXT_INSTRUCTION = (
    "Ask the user this question as plain text (NOT AskUserQuestion — the "
    "answer space is too varied). Use question_text VERBATIM."
)

_CHECKPOINT_INSTRUCTION = (
    "This is a CHECKPOINT — do NOT call AskUserQuestion. Render the "
    "checkpoint_payload as a ✅/❓ summary in chat. Wait for the user to "
    "reply with 'continue' / 'looks good' / 'confirmed' (advance), "
    "'change <field_key> to <new value>' (update + re-show), or "
    "'go back to <field_key>' (rewind)."
)

# Appended to a field's instruction based on its requirement level so the
# caller enforces the completeness gate in chat (the server enforces it too).
_REQUIRED_SUFFIX = (
    " THIS FIELD IS REQUIRED — the user MUST provide a real value. A 'skip' / "
    "'none' / 'Not required' reply is NOT accepted; if they try, explain it's "
    "required and re-ask. Do not call submit_clarifying_answer with an empty "
    "or skip-style answer for this field."
)
_CONDITIONAL_SUFFIX = (
    " THIS FIELD IS CONDITIONAL — if it genuinely does not apply, the user must "
    "EXPLICITLY confirm 'Not required' (a silent skip is not enough). Submit "
    "their reply verbatim: 'Not required' / 'N/A' records an explicit no-op, a "
    "real value records the detail. Never silently skip it."
)


@dataclass(frozen=True)
class NextQuestion:
    """The next clarifying question the server wants Claude to ask.

    Treat every field as load-bearing — Claude must surface ``question_text``
    and ``options`` VERBATIM. For checkpoints, render ``checkpoint_payload``
    as a summary instead.
    """

    field_key: str
    position: int                      # 1-indexed for display ("Q1 of N")
    total_remaining: int               # how many fields left including this one
    question_text: str                 # use VERBATIM in AskUserQuestion / chat
    options: Optional[tuple[str, ...]] # use VERBATIM, or None for free-text
    is_checkpoint: bool                # if True, render summary NOT AskUserQuestion
    checkpoint_payload: Optional[dict] # set when is_checkpoint=True; else None
    instruction_for_claude: str        # short directive — copy this to chat as-is
    agent_hint: Optional[str]          # agent-only directive; ACT on it, NEVER render to user
    requirement: str                   # "required" | "conditional" | "optional"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view (tuple -> list)."""
        d = asdict(self)
        if self.options is not None:
            d["options"] = list(self.options)
        return d


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _normalise_state(state: Optional[dict]) -> dict:
    """Return a fresh dict with all required keys populated.

    Treats ``None`` and ``{}`` as a brand-new intake — keeps the column
    default ``'{}'::jsonb`` compatible with the state machine.
    """
    state = dict(state or {})
    state.setdefault("current_field_index", 0)
    state.setdefault("collected", {})
    state.setdefault("skipped", [])
    state.setdefault("not_required", [])
    state.setdefault("checkpoint_state", None)
    # Defensive: never let the index regress below zero.
    if state["current_field_index"] < 0:
        state["current_field_index"] = 0
    return state


def _is_already_handled(state: dict, field: ClarifyingField) -> bool:
    """A regular (non-checkpoint) field is 'handled' if it's been collected,
    silently skipped (optional), or explicitly confirmed Not-required
    (conditional). Checkpoints are tracked via ``checkpoint_state`` not
    ``collected``, so we never short-circuit them this way.
    """
    if field.is_checkpoint:
        return False
    return (
        field.key in state["collected"]
        or field.key in state["skipped"]
        or field.key in state.get("not_required", [])
    )


def _total_remaining_from(field_list: list[ClarifyingField], state: dict) -> int:
    """How many non-handled fields are at-or-after ``current_field_index``."""
    idx = state["current_field_index"]
    return sum(
        1 for cf in field_list[idx:]
        if not _is_already_handled(state, cf)
    )


def _position_for(field_list: list[ClarifyingField], state: dict) -> int:
    """1-indexed display position of the current field in the full intake.

    Counts every field from the start of the list up to and including the
    current one (regardless of whether earlier fields were answered or
    skipped). This is the "Q5 of N" number a user sees in chat — it tracks
    overall progress through the script, not just the remaining ones.
    """
    return state["current_field_index"] + 1


def _build_checkpoint_payload(
    field_list: list[ClarifyingField],
    state: dict,
) -> dict:
    """Snapshot of collected + remaining-non-checkpoint fields for rendering."""
    remaining = [
        cf.key for cf in field_list[state["current_field_index"] + 1 :]
        if not cf.is_checkpoint and not _is_already_handled(state, cf)
    ]
    return {
        "collected": dict(state["collected"]),
        "remaining_fields": remaining,
        "skipped": list(state["skipped"]),
        "not_required": list(state.get("not_required", [])),
    }


# ---------------------------------------------------------------------------
# Public API — next_question / submit_answer
# ---------------------------------------------------------------------------

def next_question(
    field_list: list[ClarifyingField],
    state: Optional[dict],
) -> Optional[NextQuestion]:
    """Return the next question to ask, or ``None`` when intake is complete.

    Walks the field list from ``current_field_index``, skipping any field
    already in ``collected`` or ``skipped``. For checkpoint fields, builds
    a summary payload + checkpoint-specific instruction so Claude renders
    a ✅/❓ summary rather than calling AskUserQuestion.
    """
    norm = _normalise_state(state)
    # Find the first non-handled field at-or-after current_field_index.
    idx = norm["current_field_index"]
    while idx < len(field_list) and _is_already_handled(norm, field_list[idx]):
        idx += 1
    if idx >= len(field_list):
        return None

    # Advance the pointer past any already-handled fields so callers see
    # consistent position numbers. (Mutates the local copy; the persisted
    # state is updated by submit_answer.)
    norm["current_field_index"] = idx
    cf = field_list[idx]

    total_remaining = _total_remaining_from(field_list, norm)
    position = _position_for(field_list, norm)

    if cf.is_checkpoint:
        payload = _build_checkpoint_payload(field_list, norm)
        return NextQuestion(
            field_key=cf.key,
            position=position,
            total_remaining=total_remaining,
            question_text=cf.question,
            options=None,
            is_checkpoint=True,
            checkpoint_payload=payload,
            instruction_for_claude=_CHECKPOINT_INSTRUCTION,
            agent_hint=cf.agent_hint,
            requirement=cf.requirement,
        )

    instruction = (
        _ASK_USER_QUESTION_INSTRUCTION
        if cf.suggested_options
        else _PLAIN_TEXT_INSTRUCTION
    )
    if cf.agent_hint:
        instruction = (
            f"{instruction}\nAGENT HINT (act on this BEFORE asking; NEVER show it to "
            f"the user): {cf.agent_hint} — if you can resolve the answer from the brief "
            f"or prior context, call submit_clarifying_answer with that value and skip "
            f"the question; only ask the user when it's genuinely unresolved."
        )
    if cf.requirement == "required":
        instruction = instruction + _REQUIRED_SUFFIX
    elif cf.requirement == "conditional":
        instruction = instruction + _CONDITIONAL_SUFFIX
    return NextQuestion(
        field_key=cf.key,
        position=position,
        total_remaining=total_remaining,
        question_text=cf.question,
        options=cf.suggested_options,
        is_checkpoint=False,
        checkpoint_payload=None,
        instruction_for_claude=instruction,
        agent_hint=cf.agent_hint,
        requirement=cf.requirement,
    )


def _is_skip_answer(answer: str) -> bool:
    """True when the user's reply means 'skip this field'."""
    norm = (answer or "").strip().lower()
    if not norm:
        return True
    return norm in {"skip", "(skip)", "none", "n/a", "na", "no answer", "pass"}


# Explicit "this doesn't apply" phrases for the conditional / required gate.
# ANCHORED at the start of the answer (with a trailing word boundary) so only a
# standalone NA confirmation counts — e.g. "Not required", "Not required —
# design fresh", "no integration needed". A phrase buried inside a real answer
# ("...SFTP not needed for the secondary feed", "no fluff, not needed paperwork")
# must NOT be misread as NA, or a load-bearing integration detail gets silently
# dropped (conditional) or a valid answer wrongly rejected (required).
_NA_START_RE = re.compile(
    r"^\s*[\"']?\s*(?:"
    r"not\s+required|not\s+applicable|not\s+needed|not\s+req|"
    r"no\s+integrations?|no\s+tracking|no\s+dnq|none\s+needed"
    r")\b",
    re.IGNORECASE,
)


def _is_na_answer(answer: str) -> bool:
    """True when the reply means 'this field does not apply' (a skip token OR an
    explicit, leading 'Not required'-style confirmation). The requirement level
    decides how that is recorded — skipped (optional), not_required
    (conditional), or rejected (required).

    Matching is ANCHORED (skip-token exact-match via `_is_skip_answer`, NA
    phrases only at the START of the answer) so a phrase appearing inside a real
    free-text answer never misclassifies it as a no-op.
    """
    if _is_skip_answer(answer):
        return True
    return bool(_NA_START_RE.match(answer or ""))


def _field_index_by_key(
    field_list: list[ClarifyingField], key: str,
) -> Optional[int]:
    for i, cf in enumerate(field_list):
        if cf.key == key:
            return i
    return None


def submit_answer(
    field_list: list[ClarifyingField],
    state: Optional[dict],
    field_key: str,
    answer: str,
) -> dict:
    """Record the user's answer and return the NEW state dict.

    Validation contract
    -------------------
    The caller (``server.submit_clarifying_answer``) is responsible for
    confirming ``field_key`` matches the *expected* next question — this
    function trusts it. We re-check the match here as a defensive belt-and-
    braces guard and raise ``ValueError`` if it diverges, so a stale
    front-end can't silently corrupt the state.

    Regular fields
    --------------
    - Empty / "skip" / "none" answers are added to ``state['skipped']``
      and the index advances.
    - Any other answer is stored under ``state['collected'][field_key]``
      and the index advances.

    Checkpoint fields
    -----------------
    - "looks good" / "continue" / "confirmed" (etc.) -> ``checkpoint_state``
      flips to "confirmed" and the index advances past the checkpoint.
    - "change <field_key> to <new value>" -> ``state['collected'][<key>]``
      is updated; ``checkpoint_state`` resets to "pending"; the index
      DOES NOT advance (the checkpoint re-asks itself).
    - "go back to <field_key>" -> the index rewinds to that field's
      position; any later answers stay in ``collected`` so they're
      treated as already-handled when traversal resumes. The named field
      is removed from ``collected`` / ``skipped`` so it re-asks.
    - Anything else is treated as a free-form note and stored under
      ``collected['__checkpoint_note__']`` while waiting for confirmation
      (we deliberately do NOT advance — operators should explicitly say
      'continue').
    """
    norm = _normalise_state(state)

    expected = None
    # Skip past any handled fields to find the truly-expected current field.
    idx = norm["current_field_index"]
    while idx < len(field_list) and _is_already_handled(norm, field_list[idx]):
        idx += 1
    if idx < len(field_list):
        expected = field_list[idx]

    if expected is None:
        raise ValueError(
            f"intake is already complete; cannot record answer for "
            f"field_key={field_key!r}"
        )
    if expected.key != field_key:
        raise ValueError(
            f"Expected field_key {expected.key!r}, got {field_key!r}. "
            f"Call get_next_question to resync."
        )

    # Make sure the index points at the expected field before mutating.
    norm["current_field_index"] = idx

    if expected.is_checkpoint:
        return _apply_checkpoint_answer(field_list, norm, expected, answer)

    # Regular field. Route a no-op reply ("skip" / empty / "Not required" /
    # "N/A" / "no integration") on the field's requirement level:
    #   - required    -> REJECT (a required field can't be skipped)
    #   - conditional -> record an EXPLICIT Not-required confirmation
    #   - optional    -> silent skip (legacy behaviour)
    if _is_na_answer(answer):
        if expected.requirement == "required":
            raise ValueError(
                f"field_key {expected.key!r} is REQUIRED and cannot be skipped "
                f"or marked 'Not required'; provide a real value."
            )
        # Clear any stale collected value so re-traversal treats it as handled
        # via skipped / not_required, not answered.
        norm["collected"] = {
            k: v for k, v in norm["collected"].items() if k != expected.key
        }
        if expected.requirement == "conditional":
            if expected.key not in norm["not_required"]:
                norm["not_required"] = list(norm["not_required"]) + [expected.key]
            norm["skipped"] = [k for k in norm["skipped"] if k != expected.key]
        else:  # optional
            if expected.key not in norm["skipped"]:
                norm["skipped"] = list(norm["skipped"]) + [expected.key]
            norm["not_required"] = [k for k in norm["not_required"] if k != expected.key]
    else:
        norm["collected"] = {**norm["collected"], expected.key: answer}
        # A real answer clears any prior skip / not-required marker.
        if expected.key in norm["skipped"]:
            norm["skipped"] = [k for k in norm["skipped"] if k != expected.key]
        if expected.key in norm["not_required"]:
            norm["not_required"] = [k for k in norm["not_required"] if k != expected.key]

    norm["current_field_index"] = idx + 1
    # Leaving a regular field always clears any lingering checkpoint state.
    norm["checkpoint_state"] = None
    return norm


def _apply_checkpoint_answer(
    field_list: list[ClarifyingField],
    state: dict,
    field: ClarifyingField,
    answer: str,
) -> dict:
    """Handle the four checkpoint outcomes — advance / change / rewind / note."""
    raw = (answer or "").strip()
    lower = raw.lower()

    # Rewind first — "go back to <key>" — so it isn't shadowed by "continue".
    m_rewind = _CHECKPOINT_REWIND_RE.match(raw)
    if m_rewind:
        target_key = m_rewind.group(1)
        target_idx = _field_index_by_key(field_list, target_key)
        if target_idx is None:
            raise ValueError(
                f"go back: field_key {target_key!r} is not a known clarifying field"
            )
        # Reopen the target: drop it from collected + skipped + not_required
        # so it re-asks.
        state["collected"] = {
            k: v for k, v in state["collected"].items() if k != target_key
        }
        state["skipped"] = [k for k in state["skipped"] if k != target_key]
        state["not_required"] = [
            k for k in state.get("not_required", []) if k != target_key
        ]
        state["current_field_index"] = target_idx
        state["checkpoint_state"] = None
        return state

    # Change — "change <key> to <new value>"
    m_change = _CHECKPOINT_CHANGE_RE.match(raw)
    if m_change:
        target_key = m_change.group(1)
        new_value = m_change.group(2).strip()
        if _field_index_by_key(field_list, target_key) is None:
            raise ValueError(
                f"change: field_key {target_key!r} is not a known clarifying field"
            )
        state["collected"] = {**state["collected"], target_key: new_value}
        # If the target was skipped / marked Not-required, it's now answered —
        # drop it from both lists.
        state["skipped"] = [k for k in state["skipped"] if k != target_key]
        state["not_required"] = [
            k for k in state.get("not_required", []) if k != target_key
        ]
        # Stay on the checkpoint, re-show the summary.
        state["checkpoint_state"] = "pending"
        return state

    # Advance — substring match against any known confirm phrase.
    if any(phrase in lower for phrase in _CHECKPOINT_ADVANCE_PHRASES):
        state["checkpoint_state"] = "confirmed"
        # Move past the checkpoint pseudo-field.
        state["current_field_index"] = state["current_field_index"] + 1
        return state

    # Free-form note while waiting for confirmation — stash it but stay put.
    notes = dict(state["collected"])
    notes["__checkpoint_note__"] = raw
    state["collected"] = notes
    state["checkpoint_state"] = "pending"
    return state


__all__ = [
    "NextQuestion",
    "next_question",
    "submit_answer",
]
