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
        """Walk every non-checkpoint field with a stub answer, advance the
        checkpoint via 'continue', and confirm next_question returns None."""
        state: dict = {}
        # Walk in order. Position 6 (index 5) is the checkpoint after
        # images_choice was inserted at position 5.
        answers = {
            "page_intent": "New microsite landing page",
            "site_name": "HealthBoost",
            "site_brief": "uploaded brief paste",
            "primary_cta": "Get started",
            "images_choice": "No — clean modern look with icons + gradients only",
            "palette": "modern blue",
            "benefits": "fast, accurate, cheap",
            "tone": "Friendly + casual",
            "gtm_tag": "GTM-XXXXXXX",
            "references_to_avoid": "no competitor styles",
            "optional_sections_content": "no optional sections",
        }
        ordered_keys = [
            "page_intent", "site_name", "site_brief", "primary_cta",
            "images_choice",
            "review_checkpoint",  # checkpoint
            "palette", "benefits", "tone", "gtm_tag",
            "references_to_avoid", "optional_sections_content",
        ]
        for key in ordered_keys:
            nq = sm.next_question(_CLARIFYING_FIELDS, state)
            assert nq is not None, f"next_question returned None before {key}"
            assert nq.field_key == key, (
                f"expected {key!r} next, got {nq.field_key!r}"
            )
            if key == "review_checkpoint":
                state = sm.submit_answer(
                    _CLARIFYING_FIELDS, state, key, "looks good",
                )
            else:
                state = sm.submit_answer(
                    _CLARIFYING_FIELDS, state, key, answers[key],
                )
        # All done.
        assert sm.next_question(_CLARIFYING_FIELDS, state) is None
        # Every non-checkpoint answer landed.
        for key, value in answers.items():
            assert state["collected"][key] == value

    def test_checkpoint_position_is_six(self):
        """The checkpoint sits at position 6 in the displayed flow (1-indexed).

        It moved from 5→6 when images_choice was inserted at position 5.
        """
        # Walk past the five pre-checkpoint fields with real answers so the
        # checkpoint comes up next.
        state: dict = {}
        for key, ans in [
            ("page_intent", "New microsite landing page"),
            ("site_name", "HealthBoost"),
            ("site_brief", "x"),
            ("primary_cta", "Get started"),
            ("images_choice", "No — clean modern look with icons + gradients only"),
        ]:
            state = sm.submit_answer(_CLARIFYING_FIELDS, state, key, ans)
        nq = sm.next_question(_CLARIFYING_FIELDS, state)
        assert nq.field_key == "review_checkpoint"
        assert nq.is_checkpoint is True
        # Position counts the checkpoint itself (6 nodes walked so far).
        assert nq.position == 6
