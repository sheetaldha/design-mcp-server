"""Tests for the server-driven clarifying-question state machine.

Covers:
- Linear traversal of the full landing-page field list (page_intent ->
  site_name -> ... -> optional_sections_content) with the checkpoint in
  position 5.
- Empty / skip-style answers move a field into `state['skipped']` and
  advance the index without recording it under `collected`.
- Already-collected fields are short-circuited on re-entry.
- Already-skipped fields are short-circuited on re-entry.
- Checkpoint advance ("continue" / "looks good" / "confirmed").
- Checkpoint "change <field> to <value>" updates a prior answer and
  re-shows the same checkpoint.
- Checkpoint "go back to <field>" rewinds the index and reopens the field.
- Submitting with the wrong field_key raises ValueError (so the server
  can surface a structured 'resync' response).
- next_question() returns None when intake is complete.
- The NextQuestion payload is JSON-serialisable and carries the verbatim
  question text + option list for landing-page page_intent.
"""

from __future__ import annotations

import pytest

from design_mcp import intake_state_machine as sm
from design_mcp.generators._brief_template import field, ClarifyingField
from design_mcp.generators.landing_page import _CLARIFYING_FIELDS


# Canonical landing-page walk for the production 16-field intake, in field
# order. Required fields carry real answers; conditional fields say "Not
# required"; optional fields carry a value or "skip"; the terminal checkpoint
# advances on "looks good". Shared by the integration tests below.
LANDING_WALK: list[tuple[str, str]] = [
    ("page_intent", "New microsite landing page"),
    ("site_brief", "brief paste"),
    ("reference_layout", "Not required"),
    ("page_path", "/health"),
    ("site_name", "HealthBoost"),
    ("content_copy", "you write it"),
    ("primary_cta", "Request a quote"),
    ("images_choice", "No — clean modern look with icons + gradients only"),
    ("palette", "modern blue"),
    ("integrations", "Not required"),
    ("tracking", "Not required"),
    ("benefits", "fast"),
    ("tone", "Friendly + casual"),
    # A real value (not the skip token "none") so this optional field lands
    # in `collected`, not `skipped`.
    ("references_to_avoid", "no competitor styles"),
    ("optional_sections_content", "no optional sections"),
    ("review_checkpoint", "looks good"),
]


# ---------------------------------------------------------------------------
# Helpers — short field lists for the unit tests so we don't keep poking at
# the production landing-page list (which has its own integration coverage).
# ---------------------------------------------------------------------------

def _toy_fields() -> list[ClarifyingField]:
    """Three regular fields, one checkpoint, two more regular fields."""
    return [
        field("color", "What colour?", "Red", "Blue", "Green"),
        field("size", "What size?"),
        field("review", "Confirm so far?", is_checkpoint=True),
        field("shipping", "Shipping?", "Standard", "Express"),
        field("gift_wrap", "Gift wrap?"),
    ]


# ---------------------------------------------------------------------------
# next_question — basic shape + traversal
# ---------------------------------------------------------------------------

class TestNextQuestion:
    def test_initial_state_returns_first_field(self):
        nq = sm.next_question(_toy_fields(), {})
        assert nq is not None
        assert nq.field_key == "color"
        assert nq.question_text == "What colour?"
        assert nq.options == ("Red", "Blue", "Green")
        assert nq.is_checkpoint is False
        assert nq.checkpoint_payload is None
        assert nq.position == 1

    def test_initial_state_total_remaining_excludes_nothing_yet(self):
        nq = sm.next_question(_toy_fields(), {})
        # 4 regular fields + 1 checkpoint = 5 total nodes to walk.
        assert nq.total_remaining == 5

    def test_none_state_is_normalised_to_fresh(self):
        nq = sm.next_question(_toy_fields(), None)
        assert nq is not None
        assert nq.field_key == "color"

    def test_returns_none_when_index_past_end(self):
        state = {"current_field_index": 99, "collected": {}, "skipped": []}
        assert sm.next_question(_toy_fields(), state) is None

    def test_returns_none_when_all_fields_handled(self):
        state = {
            "current_field_index": 0,
            "collected": {"color": "Red", "size": "M", "shipping": "Standard", "gift_wrap": "no"},
            "skipped": [],
            "checkpoint_state": "confirmed",
        }
        # Advance past the checkpoint too — it's at index 2.
        state["current_field_index"] = len(_toy_fields())
        assert sm.next_question(_toy_fields(), state) is None

    def test_free_form_field_renders_with_no_options(self):
        state = {"current_field_index": 1, "collected": {"color": "Red"}, "skipped": []}
        nq = sm.next_question(_toy_fields(), state)
        assert nq.field_key == "size"
        assert nq.options is None
        assert "plain text" in nq.instruction_for_claude.lower() or "NOT AskUserQuestion" in nq.instruction_for_claude

    def test_already_collected_field_is_skipped(self):
        """If current_field_index points at a field already in `collected`,
        traversal walks past it to the next unhandled field."""
        state = {"current_field_index": 0, "collected": {"color": "Red"}, "skipped": []}
        nq = sm.next_question(_toy_fields(), state)
        assert nq.field_key == "size"

    def test_already_skipped_field_is_skipped(self):
        state = {"current_field_index": 0, "collected": {}, "skipped": ["color"]}
        nq = sm.next_question(_toy_fields(), state)
        assert nq.field_key == "size"

    def test_checkpoint_field_carries_payload(self):
        state = {
            "current_field_index": 2,
            "collected": {"color": "Red", "size": "M"},
            "skipped": [],
        }
        nq = sm.next_question(_toy_fields(), state)
        assert nq.field_key == "review"
        assert nq.is_checkpoint is True
        assert nq.options is None
        assert nq.checkpoint_payload is not None
        assert nq.checkpoint_payload["collected"] == {"color": "Red", "size": "M"}
        assert "shipping" in nq.checkpoint_payload["remaining_fields"]
        assert "gift_wrap" in nq.checkpoint_payload["remaining_fields"]

    def test_to_dict_is_json_serialisable(self):
        import json
        nq = sm.next_question(_toy_fields(), {})
        d = nq.to_dict()
        # tuple -> list so json.dumps doesn't choke.
        assert isinstance(d["options"], list)
        json.dumps(d)  # smoke — must not raise


# ---------------------------------------------------------------------------
# submit_answer — regular fields
# ---------------------------------------------------------------------------

class TestSubmitAnswerRegularFields:
    def test_records_answer_and_advances(self):
        new_state = sm.submit_answer(_toy_fields(), {}, "color", "Red")
        assert new_state["collected"] == {"color": "Red"}
        assert new_state["current_field_index"] == 1
        # Next question is now "size".
        nq = sm.next_question(_toy_fields(), new_state)
        assert nq.field_key == "size"

    def test_empty_answer_skips(self):
        new_state = sm.submit_answer(_toy_fields(), {}, "color", "")
        assert "color" in new_state["skipped"]
        assert "color" not in new_state["collected"]
        assert new_state["current_field_index"] == 1

    def test_whitespace_answer_skips(self):
        new_state = sm.submit_answer(_toy_fields(), {}, "color", "   ")
        assert "color" in new_state["skipped"]

    @pytest.mark.parametrize("token", ["skip", "Skip", "(skip)", "none", "N/A", "pass"])
    def test_skip_synonyms_skip(self, token):
        new_state = sm.submit_answer(_toy_fields(), {}, "color", token)
        assert "color" in new_state["skipped"]
        assert "color" not in new_state["collected"]

    def test_other_freeform_answer_records_as_collected(self):
        # "Other"-style replies (user typed something not in the option list)
        # are still recorded verbatim — the state machine doesn't try to
        # validate against the option list (the contract is "verbatim").
        new_state = sm.submit_answer(_toy_fields(), {}, "color", "Magenta")
        assert new_state["collected"] == {"color": "Magenta"}

    def test_wrong_field_key_raises(self):
        with pytest.raises(ValueError, match=r"Expected field_key 'color'"):
            sm.submit_answer(_toy_fields(), {}, "size", "M")

    def test_answer_after_intake_complete_raises(self):
        state = {
            "current_field_index": len(_toy_fields()),
            "collected": {"color": "R", "size": "M", "shipping": "S", "gift_wrap": "n"},
            "skipped": [],
            "checkpoint_state": "confirmed",
        }
        with pytest.raises(ValueError, match=r"intake is already complete"):
            sm.submit_answer(_toy_fields(), state, "gift_wrap", "no")

    def test_answering_a_field_after_rewind_un_skips_it(self):
        """End-to-end: skip a field, then rewind to it via a checkpoint
        'go back to', then answer it — the field moves from skipped to
        collected."""
        fields = _toy_fields()
        state: dict = {}
        # Skip color.
        state = sm.submit_answer(fields, state, "color", "")
        assert "color" in state["skipped"]
        # Answer size so we can hit the checkpoint.
        state = sm.submit_answer(fields, state, "size", "M")
        # At the checkpoint now.
        assert sm.next_question(fields, state).field_key == "review"
        # Rewind back to color — the rewind path drops it from skipped.
        state = sm.submit_answer(fields, state, "review", "go back to color")
        assert "color" not in state["skipped"]
        # Now answer color for real.
        state = sm.submit_answer(fields, state, "color", "Red")
        assert state["collected"]["color"] == "Red"
        assert "color" not in state["skipped"]


# ---------------------------------------------------------------------------
# submit_answer — checkpoint advance / change / rewind / note
# ---------------------------------------------------------------------------

def _state_at_checkpoint() -> dict:
    """Walk the toy field list to the checkpoint with two regular answers."""
    fields = _toy_fields()
    state: dict = {}
    state = sm.submit_answer(fields, state, "color", "Red")
    state = sm.submit_answer(fields, state, "size", "M")
    # Cursor now sits on the checkpoint (index 2).
    assert state["current_field_index"] == 2
    return state


class TestSubmitAnswerCheckpoint:
    @pytest.mark.parametrize(
        "answer",
        ["continue", "looks good", "confirmed", "Looks Great", "yes — ship it"],
    )
    def test_advance_phrases_move_past_checkpoint(self, answer):
        state = _state_at_checkpoint()
        new = sm.submit_answer(_toy_fields(), state, "review", answer)
        assert new["checkpoint_state"] == "confirmed"
        assert new["current_field_index"] == 3
        nq = sm.next_question(_toy_fields(), new)
        assert nq.field_key == "shipping"

    def test_change_updates_field_and_stays_on_checkpoint(self):
        state = _state_at_checkpoint()
        new = sm.submit_answer(
            _toy_fields(), state, "review", "change color to Blue",
        )
        assert new["collected"]["color"] == "Blue"
        # Still on the checkpoint — index unchanged.
        assert new["current_field_index"] == 2
        assert new["checkpoint_state"] == "pending"
        # And next_question returns the SAME checkpoint again.
        nq = sm.next_question(_toy_fields(), new)
        assert nq.field_key == "review"
        # The summary payload should reflect the new colour.
        assert nq.checkpoint_payload["collected"]["color"] == "Blue"

    def test_change_unknown_field_raises(self):
        state = _state_at_checkpoint()
        with pytest.raises(ValueError, match=r"not a known clarifying field"):
            sm.submit_answer(
                _toy_fields(), state, "review", "change nonsense to whatever",
            )

    def test_go_back_rewinds_and_reopens_field(self):
        state = _state_at_checkpoint()
        new = sm.submit_answer(_toy_fields(), state, "review", "go back to size")
        # Cursor jumps to size (index 1) and `size` is removed from collected.
        assert new["current_field_index"] == 1
        assert "size" not in new["collected"]
        nq = sm.next_question(_toy_fields(), new)
        assert nq.field_key == "size"
        # The earlier `color` answer is preserved.
        assert new["collected"].get("color") == "Red"

    def test_go_back_unknown_field_raises(self):
        state = _state_at_checkpoint()
        with pytest.raises(ValueError, match=r"not a known clarifying field"):
            sm.submit_answer(_toy_fields(), state, "review", "go back to nonsense")

    def test_freeform_note_stays_on_checkpoint(self):
        state = _state_at_checkpoint()
        new = sm.submit_answer(
            _toy_fields(), state, "review", "actually, let me think about it",
        )
        # Index unchanged — we're still on the checkpoint.
        assert new["current_field_index"] == 2
        assert new["checkpoint_state"] == "pending"
        # The note is stashed (visible to operators / for debugging).
        assert new["collected"].get("__checkpoint_note__") == (
            "actually, let me think about it"
        )

    def test_go_back_takes_precedence_over_advance(self):
        """'go back to color' contains no advance-phrase substring; but if
        the user wrote 'continue but first go back to color', the rewind
        regex doesn't match (it's anchored) and we fall back to advance.
        This test pins the priority: an unambiguous 'go back to X' wins."""
        state = _state_at_checkpoint()
        new = sm.submit_answer(_toy_fields(), state, "review", "go back to color")
        # Rewind path: index drops, color removed from collected.
        assert new["current_field_index"] == 0
        assert "color" not in new["collected"]

    def test_change_then_advance_resumes_at_next_field(self):
        state = _state_at_checkpoint()
        state = sm.submit_answer(
            _toy_fields(), state, "review", "change color to Blue",
        )
        state = sm.submit_answer(_toy_fields(), state, "review", "continue")
        nq = sm.next_question(_toy_fields(), state)
        assert nq.field_key == "shipping"
        assert state["collected"]["color"] == "Blue"


# ---------------------------------------------------------------------------
# Integration with the production landing-page field list
# ---------------------------------------------------------------------------

class TestLandingPageFieldListIntegration:
    def test_first_question_is_page_intent_with_three_options(self):
        nq = sm.next_question(_CLARIFYING_FIELDS, {})
        assert nq.field_key == "page_intent"
        assert nq.question_text == "What kind of work is this?"
        assert nq.options == (
            "New microsite landing page",
            "Enhancement to an existing landing page",
            "Replica of an existing landing page",
        )

    def test_full_walk_records_every_field_and_ends_with_none(self):
        """Walk the full 16-field landing intake in order: required fields get
        real answers, conditional ones get 'Not required', optional ones get a
        value-or-skip, then the terminal review_checkpoint advances via 'looks
        good'. Confirm next_question returns None and that required/optional
        real answers landed in collected while conditionals went to
        not_required."""
        state: dict = {}
        # (field_key, answer) — the canonical landing walk, in field order.
        for key, ans in LANDING_WALK:
            nq = sm.next_question(_CLARIFYING_FIELDS, state)
            assert nq is not None, f"next_question returned None before {key}"
            assert nq.field_key == key, (
                f"expected {key!r} next, got {nq.field_key!r}"
            )
            state = sm.submit_answer(_CLARIFYING_FIELDS, state, key, ans)
        # All done.
        assert sm.next_question(_CLARIFYING_FIELDS, state) is None
        # Required + optional real answers landed in collected.
        for key in (
            "page_intent", "site_brief", "page_path", "site_name",
            "content_copy", "primary_cta", "images_choice", "palette",
            "benefits", "tone", "references_to_avoid",
            "optional_sections_content",
        ):
            assert key in state["collected"], f"{key} missing from collected"
        # Conditional fields answered "Not required" went to not_required.
        for key in ("reference_layout", "integrations", "tracking"):
            assert key in state["not_required"], (
                f"{key} expected in not_required"
            )
            assert key not in state["collected"]

    def test_review_checkpoint_is_terminal(self):
        """review_checkpoint is the LAST field (index 15) — once every prior
        field is handled, it's the next_question the state machine surfaces."""
        # It's the terminal field in the list.
        assert _CLARIFYING_FIELDS[-1].key == "review_checkpoint"
        assert _CLARIFYING_FIELDS[-1].is_checkpoint is True

        # Handle all 15 prior fields (everything except the checkpoint), then
        # the checkpoint should come up next.
        state: dict = {}
        for key, ans in LANDING_WALK[:-1]:  # all but review_checkpoint
            state = sm.submit_answer(_CLARIFYING_FIELDS, state, key, ans)
        nq = sm.next_question(_CLARIFYING_FIELDS, state)
        assert nq.field_key == "review_checkpoint"
        assert nq.is_checkpoint is True
        # It's the 16th node displayed (1-indexed terminal position).
        assert nq.position == 16


# ---------------------------------------------------------------------------
# Requirement levels — required / conditional / optional + Not-required NA
# ---------------------------------------------------------------------------

def _req_fields() -> list[ClarifyingField]:
    """One field per requirement level for completeness-gate tests."""
    return [
        field("must_have", "Required value?", requirement="required"),
        field("maybe", "Conditional value?", requirement="conditional"),
        field("extra", "Optional value?", requirement="optional"),
    ]


class TestRequirementLevels:
    def test_next_question_exposes_requirement(self):
        nq = sm.next_question(_req_fields(), {})
        assert nq.field_key == "must_have"
        assert nq.requirement == "required"
        # Surfaced in the serialised payload too.
        assert nq.to_dict()["requirement"] == "required"

    def test_required_instruction_says_required(self):
        nq = sm.next_question(_req_fields(), {})
        assert "REQUIRED" in nq.instruction_for_claude

    def test_conditional_instruction_says_not_required_confirm(self):
        state = sm.submit_answer(_req_fields(), {}, "must_have", "yes")
        nq = sm.next_question(_req_fields(), state)
        assert nq.field_key == "maybe"
        assert nq.requirement == "conditional"
        assert "CONDITIONAL" in nq.instruction_for_claude

    @pytest.mark.parametrize("reply", ["", "skip", "none", "N/A", "not required", "no integration needed"])
    def test_required_field_rejects_skip_like_answers(self, reply):
        with pytest.raises(ValueError, match=r"REQUIRED and cannot be skipped"):
            sm.submit_answer(_req_fields(), {}, "must_have", reply)

    def test_required_field_accepts_real_answer(self):
        new = sm.submit_answer(_req_fields(), {}, "must_have", "a real value")
        assert new["collected"]["must_have"] == "a real value"

    def test_conditional_not_required_records_explicit_na(self):
        state = sm.submit_answer(_req_fields(), {}, "must_have", "x")
        state = sm.submit_answer(_req_fields(), state, "maybe", "Not required")
        assert "maybe" in state["not_required"]
        assert "maybe" not in state["collected"]
        assert "maybe" not in state["skipped"]
        # And it's treated as handled — traversal moves to the optional field.
        nq = sm.next_question(_req_fields(), state)
        assert nq.field_key == "extra"

    def test_conditional_silent_skip_also_records_not_required(self):
        # A bare "skip" on a conditional field still records an explicit NA
        # (conditional fields are never silently skipped).
        state = sm.submit_answer(_req_fields(), {}, "must_have", "x")
        state = sm.submit_answer(_req_fields(), state, "maybe", "skip")
        assert "maybe" in state["not_required"]
        assert "maybe" not in state["skipped"]

    def test_conditional_real_answer_records_collected(self):
        state = sm.submit_answer(_req_fields(), {}, "must_have", "x")
        state = sm.submit_answer(_req_fields(), state, "maybe", "Databowl via API")
        assert state["collected"]["maybe"] == "Databowl via API"
        assert "maybe" not in state["not_required"]

    def test_optional_field_silent_skip(self):
        state = sm.submit_answer(_req_fields(), {}, "must_have", "x")
        state = sm.submit_answer(_req_fields(), state, "maybe", "Not required")
        state = sm.submit_answer(_req_fields(), state, "extra", "skip")
        assert "extra" in state["skipped"]
        assert "extra" not in state["not_required"]
        # Intake complete after all three handled.
        assert sm.next_question(_req_fields(), state) is None

    def test_answering_clears_prior_not_required(self):
        # Mark conditional NA, then a real answer flips it out of not_required.
        state = sm.submit_answer(_req_fields(), {}, "must_have", "x")
        state = sm.submit_answer(_req_fields(), state, "maybe", "Not required")
        assert "maybe" in state["not_required"]
        # Rewind would re-ask; simulate a re-answer by going back via index.
        state["current_field_index"] = 1
        state["not_required"] = [k for k in state["not_required"] if k != "maybe"]
        state = sm.submit_answer(_req_fields(), state, "maybe", "actual integration")
        assert state["collected"]["maybe"] == "actual integration"
        assert "maybe" not in state["not_required"]

    def test_default_requirement_is_optional(self):
        # Fields built without an explicit requirement stay optional so legacy
        # behaviour (silent skip) is preserved.
        f = field("legacy", "Legacy?")
        assert f.requirement == "optional"

    # ----- NA matching must be anchored, not substring (regression) -----

    def test_conditional_real_answer_with_buried_na_phrase_is_collected(self):
        """A genuine conditional answer that merely CONTAINS an NA phrase mid-
        string must be stored verbatim, NOT routed to not_required. This is the
        load-bearing case — a dropped integration detail breaks lead delivery."""
        fields = _req_fields()
        state = sm.submit_answer(fields, {}, "must_have", "x")
        answer = "Use the API for new leads; SFTP not needed for the secondary feed"
        state = sm.submit_answer(fields, state, "maybe", answer)
        assert state["collected"]["maybe"] == answer
        assert "maybe" not in state["not_required"]

    def test_required_real_answer_with_buried_na_phrase_is_accepted(self):
        """A required answer containing an NA phrase mid-string must NOT be
        rejected — only a leading/standalone NA reply is a skip."""
        fields = _req_fields()
        answer = "No fluff, not needed paperwork — just fast quotes."
        state = sm.submit_answer(fields, {}, "must_have", answer)
        assert state["collected"]["must_have"] == answer

    @pytest.mark.parametrize(
        "reply",
        ["Not required", "not required — design fresh", "no integration needed", "No tracking"],
    )
    def test_leading_na_phrase_still_counts_as_na(self, reply):
        """A standalone / leading NA confirmation on a conditional field still
        records an explicit Not-required."""
        fields = _req_fields()
        state = sm.submit_answer(fields, {}, "must_have", "x")
        state = sm.submit_answer(fields, state, "maybe", reply)
        assert "maybe" in state["not_required"]
        assert "maybe" not in state["collected"]
