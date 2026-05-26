"""Day 3 refactor — return-prompts pattern + iteration tools.

Covers:
- design_landing_page returns the expected structured brief shape
- drafts.create / get / update / set_status / cleanup_expired work
- submit_design validates manifest + HTML and round-trips through a
  bare git repo (publish=True) without touching the real microsite-design-skills
- submit_design with publish=False stops at "submitted" status
- submit_design surfaces structured errors when the manifest is invalid
- update_design returns iteration instructions and reopens the draft
- get_design_status returns the record + summary
- cancel_design soft-deletes without removing the record
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

# Make sure the test never needs ANTHROPIC_API_KEY or TOKEN_DB_PASSWORD before
# importing modules that read env. We only need TOKEN_DB_PASSWORD for paths
# that call DesignConfig.from_env (submit_design with publish=True).
os.environ.setdefault("TOKEN_DB_PASSWORD", "test-only-not-used")

from design_mcp import drafts  # noqa: E402
from design_mcp.generators import landing_page as landing_gen  # noqa: E402
from design_mcp.manifest import LandingPageManifest  # noqa: E402
from design_mcp.server import (  # noqa: E402
    cancel_design,
    design_landing_page,
    get_design_status,
    submit_design,
    update_design,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_drafts():
    drafts._reset_for_tests()
    yield
    drafts._reset_for_tests()


@pytest.fixture
def temp_design_repo(tmp_path, monkeypatch) -> Path:
    """Initialise a bare-ish git repo in tmp_path and point the design repo there.

    We use a single working clone (not a true bare repo) so the publish_design
    helper can commit straight in. To skip the `git push origin <branch>` step
    we monkeypatch publish_design to drop it — easier than spinning up a
    second bare remote.
    """
    repo = tmp_path / "microsite-design-skills"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)

    monkeypatch.setenv("DESIGN_REPO_LOCAL_CLONE", str(repo))
    monkeypatch.setenv("DESIGN_REPO_SSH", f"file://{repo}")
    monkeypatch.setenv("DESIGN_REPO_BRANCH", "main")
    monkeypatch.setenv("TOKEN_DB_PASSWORD", "test-only-not-used")

    # Patch out the network steps in repo.ensure_repo + the final push.
    from design_mcp import repo as repo_mod

    def fake_ensure_repo(cfg):  # type: ignore[no-untyped-def]
        return repo

    real_run = repo_mod._run

    def filtered_run(cmd, cwd=None):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["git", "push"]:
            return ""
        return real_run(cmd, cwd=cwd)

    monkeypatch.setattr(repo_mod, "ensure_repo", fake_ensure_repo)
    monkeypatch.setattr(repo_mod, "_run", filtered_run)

    return repo


# ---------------------------------------------------------------------------
# Helpers — build a valid HTML + manifest pair for landing-page family
# ---------------------------------------------------------------------------

def _valid_manifest(slug: str = "test-landing") -> dict[str, Any]:
    return {
        "family": "landing-page",
        "version": 1,
        "slug": slug,
        "intent": "Test landing page used by the day-3 refactor pytest suite.",
        "seo": {
            "title": "Test Landing — Day 3 Refactor",
            "meta_description": "Pytest-fixture landing page exercising the return-prompts submit_design flow.",
        },
        "hero": {
            "headline": "Welcome to the test landing",
            "subheading": "Built by pytest to verify submit_design round-trips through a temp git repo.",
            "cta_label": "Get Started",
            "image_url": "https://picsum.photos/seed/test-hero/1600/900",
            "image_alt": "Test hero illustration",
        },
        "features": [
            {
                "heading": f"Feature {i}",
                "paragraph": f"Stub paragraph for feature {i}.",
                "image_url": f"https://picsum.photos/seed/test-f{i}/400/400",
                "image_alt": f"Feature {i} icon",
            }
            for i in range(1, 4)
        ],
        "form": {"submit_label": "Get Started"},
        "optional_sections": [],
        "theme": {
            "color_primary": "#1F4E79",
            "color_accent": "#2E75B6",
            "color_text_body": "#1F2937",
            "color_bg_body": "#FFFFFF",
            "font_heading": "Montserrat",
            "font_body": "Montserrat",
        },
    }


def _valid_html() -> str:
    manifest = LandingPageManifest(**_valid_manifest())
    return landing_gen._render_html(manifest)


# ---------------------------------------------------------------------------
# drafts.py
# ---------------------------------------------------------------------------

class TestDrafts:
    def test_create_returns_record_with_uuid_and_drafted_status(self):
        record = drafts.create(family="landing-page", brief="something")
        assert record.family == "landing-page"
        assert record.brief == "something"
        assert record.status == "drafted"
        assert len(record.design_id) == 36  # uuid4 hex+dashes
        assert record.expires_at > record.created_at

    def test_get_unknown_raises_keyerror(self):
        with pytest.raises(KeyError):
            drafts.get("nope")

    def test_update_round_trip(self):
        record = drafts.create(family="landing-page", brief="x")
        drafts.update(record.design_id, slug="hello", status="submitted")
        again = drafts.get(record.design_id)
        assert again.slug == "hello"
        assert again.status == "submitted"
        assert any(h["event"] == "updated" for h in again.history)

    def test_update_rejects_invalid_status(self):
        record = drafts.create(family="landing-page", brief="x")
        with pytest.raises(ValueError):
            drafts.update(record.design_id, status="nonsense")

    def test_cleanup_expired_flips_old_drafts(self):
        record = drafts.create(family="landing-page", brief="x")
        # Force expiry into the past.
        drafts.update(record.design_id, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        flipped = drafts.cleanup_expired()
        assert flipped == 1
        assert drafts.get(record.design_id).status == "expired"


# ---------------------------------------------------------------------------
# design_landing_page (return-prompts entry tool)
# ---------------------------------------------------------------------------

class TestDesignLandingPage:
    def test_returns_expected_keys(self):
        result = design_landing_page(brief="Healthboost UAT, fresh greens, premium tone")
        expected = {
            "design_id",
            "family",
            "status",
            "instructions",
            "contract",
            "manifest_schema",
            "slug_hint",
            "expires_at",
            "next_action",
        }
        assert expected.issubset(result.keys())
        assert result["family"] == "landing-page"
        assert result["status"] == "drafted"
        # Contract + schema should be non-trivial dicts.
        assert isinstance(result["contract"], dict) and result["contract"]
        assert "properties" in result["manifest_schema"]
        # The draft must exist in the store.
        record = drafts.get(result["design_id"])
        assert record.brief.startswith("Healthboost")

    def test_slug_hint_used_when_provided(self):
        result = design_landing_page(brief="anything", slug="my-custom-slug")
        assert result["slug_hint"] == "my-custom-slug"


# ---------------------------------------------------------------------------
# Instructions UX — both families should drive an iterative, question-driven
# intake rather than producing HTML straight from a one-line brief.
# ---------------------------------------------------------------------------

# Phrases every family's instructions must surface so the caller's chat
# walks the user through ask -> outline -> generate -> preview -> iterate -> submit.
_SHARED_REQUIRED_PHRASES = [
    "ask the user",
    "outline",
    "wait for",
    "submit_design",
    "update_design",
    "cancel_design",
    "<title>",
    "70 char",
    "Ready to submit, iterate, or scrap",
    "show me the html",
    "Acknowledge",
]


def _instructions_for(family: str, brief: str) -> str:
    if family == "landing-page":
        from design_mcp.server import design_landing_page as _entry
    elif family == "survey-funnel":
        from design_mcp.server import design_survey_funnel as _entry
    else:  # pragma: no cover - sanity guard
        raise ValueError(family)
    return _entry(brief=brief)["instructions"]


class TestInstructionsUX:
    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_mention_all_flow_anchors(self, family):
        text = _instructions_for(family, "HealthBoost — over-50s health insurance, blue palette")
        lower = text.lower()
        for phrase in _SHARED_REQUIRED_PHRASES:
            assert phrase.lower() in lower, (
                f"[{family}] instructions missing required phrase: {phrase!r}"
            )

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_echo_the_user_brief_verbatim(self, family):
        brief = "HealthBoost — over-50s health insurance, blue palette"
        text = _instructions_for(family, brief)
        assert brief in text, f"[{family}] instructions should echo the user's brief"

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_block_html_before_outline_approval(self, family):
        text = _instructions_for(family, "anything")
        # Must explicitly defer HTML until the outline is approved.
        assert "No HTML yet" in text or "no HTML" in text.lower()
        # The "do not skip ahead" / outline-first language must be present.
        assert "before any HTML" in text or "approved a written outline" in text

    def test_landing_page_instructions_carry_family_specific_rules(self):
        text = _instructions_for("landing-page", "anything")
        assert "Landing Page" in text
        assert "feature cards" in text  # exactly 3 feature cards
        assert "/api/handle_Client_Lead_Submission" in text

    def test_survey_funnel_instructions_carry_family_specific_rules(self):
        text = _instructions_for("survey-funnel", "anything")
        assert "Survey Funnel" in text
        assert "OTP" in text
        assert "1 to 5" in text or "1..5" in text
        assert "/api/verificationsms" in text
        assert "/api/handle_Client_Lead_Submission" in text

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_contain_actual_clarifying_questions(self, family):
        # Not just "ask clarifying questions" — the actual phrasing.
        text = _instructions_for(family, "anything")
        assert "?" in text
        # At least 4 question marks in the clarifying-question block.
        assert text.count("?") >= 4

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_under_word_budget(self, family):
        text = _instructions_for(family, "anything")
        # Soft budget per the spec — ~600 words per family. Allow a little slack.
        word_count = len(text.split())
        assert word_count <= 750, f"[{family}] instructions are {word_count} words; budget ~600"


# ---------------------------------------------------------------------------
# submit_design
# ---------------------------------------------------------------------------

class TestSubmitDesign:
    def test_publish_true_commits_to_temp_repo(self, temp_design_repo):
        brief_resp = design_landing_page(brief="Test brief for submit")
        design_id = brief_resp["design_id"]
        manifest = _valid_manifest()
        html = _valid_html()

        result = submit_design(
            design_id=design_id,
            html=html,
            manifest=manifest,
            user_email="test@example.com",
            publish=True,
        )
        assert result["ok"] is True, result
        assert result["committed"] is True
        assert result["status"] == "published"
        assert result["slug"] == "test-landing"
        assert Path(result["design_dir"]).exists()
        assert (Path(result["design_dir"]) / "test-landing.html").exists()
        assert (Path(result["design_dir"]) / "page-meta.yaml").exists()

        # The draft state reflects the publish.
        record = drafts.get(design_id)
        assert record.status == "published"
        assert record.commit_sha == result["commit_sha"]

    def test_publish_false_marks_submitted_only(self, temp_design_repo):
        brief_resp = design_landing_page(brief="Preview only brief")
        design_id = brief_resp["design_id"]

        result = submit_design(
            design_id=design_id,
            html=_valid_html(),
            manifest=_valid_manifest(),
            publish=False,
        )
        assert result["ok"] is True
        assert result["committed"] is False
        assert result["status"] == "submitted"
        assert drafts.get(design_id).status == "submitted"

    def test_invalid_manifest_returns_structured_errors(self):
        brief_resp = design_landing_page(brief="Will fail validation")
        design_id = brief_resp["design_id"]
        bad = _valid_manifest()
        # Only 2 features — contract requires exactly 3.
        bad["features"] = bad["features"][:2]
        result = submit_design(
            design_id=design_id,
            html=_valid_html(),
            manifest=bad,
            publish=False,
        )
        assert result["ok"] is False
        assert any("manifest validation failed" in e for e in result["errors"])
        # Draft is still in drafted state — caller can retry.
        assert drafts.get(design_id).status == "drafted"

    def test_html_missing_h1_returns_error(self):
        brief_resp = design_landing_page(brief="Missing h1 brief")
        design_id = brief_resp["design_id"]
        result = submit_design(
            design_id=design_id,
            html="<!doctype html><html><head><title>x</title></head><body>no h1</body></html>",
            manifest=_valid_manifest(),
            publish=False,
        )
        assert result["ok"] is False
        assert any("h1" in e for e in result["errors"])

    def test_unknown_design_id_returns_not_found(self):
        result = submit_design(
            design_id="00000000-0000-0000-0000-000000000000",
            html=_valid_html(),
            manifest=_valid_manifest(),
            publish=False,
        )
        assert result["ok"] is False
        assert result["status"] == "not-found"


# ---------------------------------------------------------------------------
# update_design + get_design_status + cancel_design
# ---------------------------------------------------------------------------

class TestIterationTools:
    def test_update_design_returns_iteration_instructions(self):
        brief_resp = design_landing_page(brief="Iteration baseline")
        design_id = brief_resp["design_id"]
        result = update_design(design_id, instructions="Use a warmer palette and drop the trust badges")
        assert result["ok"] is True
        assert result["current_status"] == "drafted"
        assert "iteration_instructions" in result
        assert "Use a warmer palette" in result["iteration_instructions"]
        assert "manifest_schema" in result and result["manifest_schema"]

    def test_get_design_status_returns_record_and_summary(self):
        brief_resp = design_landing_page(brief="Status check")
        design_id = brief_resp["design_id"]
        result = get_design_status(design_id)
        assert result["ok"] is True
        assert result["record"]["status"] == "drafted"
        assert "summary" in result and design_id in result["summary"]

    def test_cancel_design_marks_cancelled_and_retains_record(self):
        brief_resp = design_landing_page(brief="To be cancelled")
        design_id = brief_resp["design_id"]
        result = cancel_design(design_id, reason="user changed their mind")
        assert result["ok"] is True
        assert result["status"] == "cancelled"
        # Record must still exist.
        record = drafts.get(design_id)
        assert record.status == "cancelled"
        assert record.last_error == "user changed their mind"

    def test_cancel_after_publish_is_rejected(self, temp_design_repo):
        brief_resp = design_landing_page(brief="Cant cancel after publish")
        design_id = brief_resp["design_id"]
        submit_design(
            design_id=design_id,
            html=_valid_html(),
            manifest=_valid_manifest(),
            publish=True,
        )
        result = cancel_design(design_id)
        assert result["ok"] is False
        assert result["status"] == "published"
