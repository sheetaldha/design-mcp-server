"""Day 3 refactor — return-prompts pattern + iteration tools, PG-backed drafts,
auth-derived user_email.

Covers:
- design_landing_page returns the expected structured brief shape
- drafts.create / get / update / set_status / set_last_error / cleanup_expired work
- Cross-user isolation: user A's design_id is invisible to user B
- create() persists user_email
- get() returns None when user_email doesn't match
- record_submission + mark_published helpers behave
- submit_design derives user_email from auth context (mocks two users)
- submit_design (publish=True) returns immediately with status="submitting"
  and the git push runs in a background task that flips to "published" or
  "failed" + last_error
- submit_design with publish=False stops at "submitted" status, no background work
- submit_design surfaces structured errors when the manifest is invalid
- update_design returns iteration instructions and reopens the draft
- get_design_status returns full lifecycle state (including last_error)
- cancel_design soft-deletes without removing the record
- cleanup_expired removes only expired rows
- Restart resilience: re-instantiating the backend with the same store
  surfaces previously-persisted drafts
- AuthContextError raised when no auth context is in scope
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

# Make sure the test never needs ANTHROPIC_API_KEY or TOKEN_DB_PASSWORD before
# importing modules that read env.
os.environ.setdefault("TOKEN_DB_PASSWORD", "test-only-not-used")

from mcp.server.auth.middleware.auth_context import auth_context_var  # noqa: E402
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402

from design_mcp import drafts  # noqa: E402
from design_mcp.generators import landing_page as landing_gen  # noqa: E402
from design_mcp.manifest import (  # noqa: E402
    FaqItem,
    LandingPageManifest,
    Testimonial,
    TrustBadge,
)

# Tell pytest's auto-collector that this Pydantic model is NOT a test class
# (its name starts with "Test" so pytest tries to collect it otherwise).
Testimonial.__test__ = False  # type: ignore[attr-defined]
from pydantic import ValidationError  # noqa: E402
from design_mcp import server as server_mod  # noqa: E402
from design_mcp.server import (  # noqa: E402
    AuthContextError,
    cancel_design,
    design_landing_page,
    get_design_status,
    submit_design,
    update_design,
)


# Default test user — covers the common case. Cross-user tests override.
DEFAULT_USER = "sheetal@acquirely.com.au"
OTHER_USER = "evil@example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@contextmanager
def _set_user(email: str) -> Iterator[None]:
    """Push an AuthenticatedUser onto the contextvar so resolve_user_email() works.

    We also stub out the DB lookups that resolve_user_email uses (oauth and
    invite tables), so tests don't need a live PG instance.
    """
    fake_token = AccessToken(
        token="t" * 64,
        client_id=f"test-client:{email}",
        scopes=["design:write"],
        expires_at=None,
    )
    # Pre-seed the cache attribute so resolve_user_email never hits DB.
    try:
        object.__setattr__(fake_token, "__user_email", email)
    except Exception:
        pass
    fake_user = AuthenticatedUser(fake_token)
    token_handle = auth_context_var.set(fake_user)
    try:
        yield
    finally:
        auth_context_var.reset(token_handle)


@pytest.fixture(autouse=True)
def _reset_drafts():
    drafts._reset_for_tests()
    yield
    drafts._reset_for_tests()


@pytest.fixture(autouse=True)
def _default_user_context():
    """Default — every test runs inside the DEFAULT_USER auth context.

    Tests that want a different user explicitly use _set_user(...) inside.
    """
    with _set_user(DEFAULT_USER):
        yield


@pytest.fixture
def temp_design_repo(tmp_path, monkeypatch) -> Path:
    """Initialise a working git repo and route publish_design at it.

    publish_design's network calls (clone / pull / push) are stubbed out;
    the local commit is real so we can inspect the artefacts.
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
            "site_name": "Acquirely Test",
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
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="something", slug_hint="something",
        )
        assert record.family == "landing-page"
        assert record.brief == "something"
        assert record.status == "drafted"
        assert record.user_email == DEFAULT_USER
        assert len(record.design_id) == 36  # uuid4 hex+dashes
        assert record.expires_at > record.created_at

    def test_create_persists_user_email(self):
        record = drafts.create(
            user_email="me@x.com", family="landing-page",
            brief="x", slug_hint="x",
        )
        again = drafts.get(record.design_id, "me@x.com")
        assert again is not None
        assert again.user_email == "me@x.com"

    def test_get_unknown_returns_none(self):
        assert drafts.get("00000000-0000-0000-0000-000000000000", DEFAULT_USER) is None

    def test_get_cross_user_returns_none(self):
        """User A creates a draft; user B's get() returns None."""
        record = drafts.create(
            user_email="alice@x.com", family="landing-page",
            brief="alice's draft", slug_hint="alice",
        )
        # Alice can read it
        assert drafts.get(record.design_id, "alice@x.com") is not None
        # Bob cannot
        assert drafts.get(record.design_id, "bob@x.com") is None

    def test_update_round_trip(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="x", slug_hint="x",
        )
        drafts.update(record.design_id, DEFAULT_USER, slug="hello", status="submitted")
        again = drafts.get(record.design_id, DEFAULT_USER)
        assert again is not None
        assert again.slug == "hello"
        assert again.status == "submitted"
        assert any(h["event"] == "updated" for h in again.iteration_log)

    def test_update_cross_user_raises_keyerror(self):
        record = drafts.create(
            user_email="alice@x.com", family="landing-page",
            brief="x", slug_hint="x",
        )
        with pytest.raises(KeyError):
            drafts.update(record.design_id, "bob@x.com", slug="pwn")

    def test_update_rejects_invalid_status(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="x", slug_hint="x",
        )
        with pytest.raises(ValueError):
            drafts.update(record.design_id, DEFAULT_USER, status="nonsense")

    def test_set_status_round_trip(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="x", slug_hint="x",
        )
        drafts.set_status(record.design_id, DEFAULT_USER, "cancelled")
        assert drafts.get(record.design_id, DEFAULT_USER).status == "cancelled"

    def test_record_submission_and_mark_published(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="x", slug_hint="x",
        )
        drafts.record_submission(
            record.design_id, DEFAULT_USER,
            html="<html></html>", manifest={"k": "v"},
            chat_summary="summary", slug="my-slug",
        )
        s = drafts.get(record.design_id, DEFAULT_USER)
        assert s.status == "submitted"
        assert s.html == "<html></html>"
        assert s.manifest == {"k": "v"}
        assert s.slug == "my-slug"

        drafts.mark_published(record.design_id, DEFAULT_USER, repo_sha="abc123", design_dir="/tmp/x")
        p = drafts.get(record.design_id, DEFAULT_USER)
        assert p.status == "published"
        assert p.published_repo_sha == "abc123"
        assert p.commit_sha == "abc123"

    def test_cleanup_expired_flips_only_expired_active_drafts(self):
        # Expired drafted draft — should flip
        r1 = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="expired", slug_hint="e",
        )
        drafts.update(
            r1.design_id, DEFAULT_USER,
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        # Active (future expiry) — should NOT flip
        r2 = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="future", slug_hint="f",
        )
        # Already-cancelled, expired — should NOT flip
        r3 = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="cancelled-expired", slug_hint="c",
        )
        drafts.update(r3.design_id, DEFAULT_USER, status="cancelled",
                      expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))

        flipped = drafts.cleanup_expired()
        assert flipped == 1
        assert drafts.get(r1.design_id, DEFAULT_USER).status == "expired"
        assert drafts.get(r2.design_id, DEFAULT_USER).status == "drafted"
        assert drafts.get(r3.design_id, DEFAULT_USER).status == "cancelled"

    def test_set_last_error_round_trip(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="err", slug_hint="err",
        )
        drafts.set_last_error(record.design_id, DEFAULT_USER, "boom")
        assert drafts.get(record.design_id, DEFAULT_USER).last_error == "boom"

    def test_set_last_error_truncates_to_2000_chars(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="trunc", slug_hint="trunc",
        )
        big = "x" * 5000
        drafts.set_last_error(record.design_id, DEFAULT_USER, big)
        stored = drafts.get(record.design_id, DEFAULT_USER).last_error
        assert stored is not None
        assert len(stored) == 2000

    def test_set_last_error_none_clears(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="clear", slug_hint="clear",
        )
        drafts.set_last_error(record.design_id, DEFAULT_USER, "oops")
        drafts.set_last_error(record.design_id, DEFAULT_USER, None)
        assert drafts.get(record.design_id, DEFAULT_USER).last_error is None

    def test_set_last_error_is_idempotent(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="idem", slug_hint="idem",
        )
        drafts.set_last_error(record.design_id, DEFAULT_USER, "same")
        log_len_before = len(drafts.get(record.design_id, DEFAULT_USER).iteration_log)
        drafts.set_last_error(record.design_id, DEFAULT_USER, "same")
        log_len_after = len(drafts.get(record.design_id, DEFAULT_USER).iteration_log)
        assert log_len_before == log_len_after  # second call must not append

    def test_set_last_error_cross_user_blocked(self):
        record = drafts.create(
            user_email="alice@x.com", family="landing-page",
            brief="x", slug_hint="x",
        )
        with pytest.raises(KeyError):
            drafts.set_last_error(record.design_id, "bob@x.com", "pwn")

    def test_submitting_and_failed_are_valid_statuses(self):
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="x", slug_hint="x",
        )
        drafts.set_status(record.design_id, DEFAULT_USER, "submitting")
        assert drafts.get(record.design_id, DEFAULT_USER).status == "submitting"
        drafts.set_status(record.design_id, DEFAULT_USER, "failed")
        assert drafts.get(record.design_id, DEFAULT_USER).status == "failed"

    def test_restart_resilience_via_shared_backend(self):
        """Simulate a PM2 restart: same underlying storage, fresh module-level
        backend instance, previously-created drafts still readable."""
        backend = drafts._InMemoryBackend()
        drafts.set_backend(backend)
        record = drafts.create(
            user_email=DEFAULT_USER, family="landing-page",
            brief="survives", slug_hint="s",
        )
        # Reinstall a fresh facade pointing at the same storage dict.
        new_backend = drafts._InMemoryBackend()
        new_backend._rows = backend._rows  # shared underlying state (= PG table)
        drafts.set_backend(new_backend)
        # Same design_id is still gettable.
        found = drafts.get(record.design_id, DEFAULT_USER)
        assert found is not None
        assert found.brief == "survives"


# ---------------------------------------------------------------------------
# resolve_user_email + AuthContextError
# ---------------------------------------------------------------------------

class TestAuthContext:
    def test_resolve_user_email_returns_default_user_inside_context(self):
        # The autouse fixture already installs DEFAULT_USER.
        assert server_mod.resolve_user_email() == DEFAULT_USER

    def test_resolve_user_email_raises_without_context(self):
        # Tear down the context entirely.
        from mcp.server.auth.middleware.auth_context import auth_context_var
        token = auth_context_var.set(None)
        try:
            with pytest.raises(AuthContextError):
                server_mod.resolve_user_email()
        finally:
            auth_context_var.reset(token)


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
        # The draft must exist in the store, scoped to the auth user.
        record = drafts.get(result["design_id"], DEFAULT_USER)
        assert record is not None
        assert record.brief.startswith("Healthboost")
        assert record.user_email == DEFAULT_USER

    def test_slug_hint_used_when_provided(self):
        result = design_landing_page(brief="anything", slug="my-custom-slug")
        assert result["slug_hint"] == "my-custom-slug"


# ---------------------------------------------------------------------------
# Instructions UX — both families should drive an iterative, question-driven
# intake rather than producing HTML straight from a one-line brief.
# ---------------------------------------------------------------------------

# Phrases every family's instructions must surface so the caller's chat
# walks the user through ask -> outline -> generate -> preview -> iterate -> submit.
# Updated for the checklist-default UX with auto-error-recovery.
_SHARED_REQUIRED_PHRASES = [
    "outline",
    "submit_design",
    "update_design",
    "cancel_design",
    "<title>",
    "60 char",
    "Next: **Submit** · **Iterate** · **Scrap**",
    "show me the html",
    "Acknowledge",
    "one at a time",
    "Question",
    "of M",
    "just generate it",
    "Sanity check",
    "Echo back",
    "From your brief:",
    "✅",
    "❓",
    "❌",
    "*Q",
    "Generated:",
    "Outline (review before HTML)",
    "get_design_status",
    "poll_after_seconds",
    "Options:",
    "Retry",
    "Diagnose",
    "Scrap",
    "⚠️",
    "get_preview_url",
    "Preview in your browser",
    "haven't viewed the actual HTML yet",
    "Submit anyway",
    "Any further improvements",
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
        assert "/api/add-lead" in text
        # The legacy endpoint that doesn't exist as a real route MUST NOT appear.
        assert "/api/handle_Client_Lead_Submission" not in text

    def test_survey_funnel_instructions_carry_family_specific_rules(self):
        text = _instructions_for("survey-funnel", "anything")
        assert "Survey Funnel" in text
        assert "OTP" in text
        assert "1 to 5" in text or "1..5" in text
        assert "/api/verificationsms" in text
        assert "/api/add-lead" in text
        assert "/api/handle_Client_Lead_Submission" not in text

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_mention_site_name_and_title_suffix(self, family):
        text = _instructions_for(family, "anything")
        # Manifest field + render pattern + sanity line all need to mention site_name.
        assert "site_name" in text, f"[{family}] instructions should mention the site_name manifest field"
        assert "{title} | {site_name}" in text, (
            f"[{family}] instructions should describe the rendered <title>={{title}} | {{site_name}} pattern"
        )

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_mention_og_url_and_jsonld_url(self, family):
        text = _instructions_for(family, "anything")
        assert "og:url" in text, f"[{family}] instructions should require <meta property='og:url'>"
        # Either 'JSON-LD' or 'json-ld' phrasing — accept both.
        assert "JSON-LD" in text or "json-ld" in text.lower(), (
            f"[{family}] instructions should mention JSON-LD"
        )

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_mention_add_lead_endpoint(self, family):
        text = _instructions_for(family, "anything")
        assert "/api/add-lead" in text
        assert "/api/handle_Client_Lead_Submission" not in text

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
        # Post-merge of the 3 May-27 parallel branches (UX guard + SEO fields +
        # optional-section data). Landing currently renders ~1218 words,
        # survey ~1169. Ceiling 1250 keeps a small headroom; trim instructions
        # in a follow-up if real-world usage shows the brief feels too long.
        word_count = len(text.split())
        assert word_count <= 1250, f"[{family}] instructions are {word_count} words; ceiling 1250"

    # ----- Adaptive step-wise intake (Day-3 UX refresh) -----

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_mention_one_question_at_a_time(self, family):
        text = _instructions_for(family, "anything")
        assert "one at a time" in text.lower() or "ONE AT A TIME" in text, (
            f"[{family}] instructions should require asking one question at a time"
        )

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_include_progress_indicator_pattern(self, family):
        text = _instructions_for(family, "anything")
        # Looks for the `*Question N of M*` progress prefix pattern.
        assert "Question" in text and "of M" in text, (
            f"[{family}] instructions should include the *Question N of M* progress prefix"
        )

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_include_speed_mode_escape_hatch(self, family):
        text = _instructions_for(family, "anything")
        assert "just generate it" in text, (
            f"[{family}] instructions should include the speed-mode escape hatch"
        )

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_include_sanity_check_line(self, family):
        text = _instructions_for(family, "anything")
        assert "Sanity check" in text, (
            f"[{family}] instructions should include the contract sanity-check line"
        )

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_include_tightened_submit_iterate_scrap(self, family):
        text = _instructions_for(family, "anything")
        assert "Next: **Submit** · **Iterate** · **Scrap**" in text, (
            f"[{family}] instructions should include the bold Submit · Iterate · Scrap prompt"
        )

    @pytest.mark.parametrize("family", ["landing-page", "survey-funnel"])
    def test_instructions_echo_back_filled_fields(self, family):
        text = _instructions_for(family, "anything")
        # The caller should be told to echo back what's filled in from the brief.
        assert "Echo back" in text or "echo back" in text, (
            f"[{family}] instructions should tell the caller to echo back filled fields"
        )

    def test_defaults_dict_exposed_for_each_family(self):
        from design_mcp.generators._brief_template import (
            LANDING_PAGE_DEFAULTS,
            SURVEY_FUNNEL_DEFAULTS,
        )
        # Sanity: defaults dicts are non-empty and cover the per-family fields.
        assert isinstance(LANDING_PAGE_DEFAULTS, dict)
        assert isinstance(SURVEY_FUNNEL_DEFAULTS, dict)
        for k in ("audience", "primary_cta", "palette", "benefits", "tone"):
            assert k in LANDING_PAGE_DEFAULTS, f"Landing Page defaults missing {k!r}"
        for k in ("audience", "steps", "otp", "submit_label", "post_submit", "palette"):
            assert k in SURVEY_FUNNEL_DEFAULTS, f"Survey Funnel defaults missing {k!r}"


# ---------------------------------------------------------------------------
# submit_design
# ---------------------------------------------------------------------------

def _submit(**kwargs):
    """Helper — submit_design is now async; sync-wrap for the legacy tests."""
    return asyncio.run(submit_design(**kwargs))


def _await_terminal(design_id: str, user_email: str, *, timeout: float = 5.0) -> Any:
    """Poll the in-memory draft store until status is no longer 'submitting' (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = drafts.get(design_id, user_email)
        if rec is not None and rec.status != "submitting":
            return rec
        time.sleep(0.02)
    return drafts.get(design_id, user_email)


class TestSubmitDesign:
    def test_publish_true_returns_submitting_then_publishes(self, temp_design_repo):
        brief_resp = design_landing_page(brief="Test brief for submit")
        design_id = brief_resp["design_id"]
        manifest = _valid_manifest()
        html = _valid_html()

        async def go():
            result = await submit_design(
                design_id=design_id, html=html, manifest=manifest, publish=True,
            )
            # Drain the background task created inside submit_design.
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return result

        result = asyncio.run(go())
        assert result["ok"] is True, result
        assert result["manifest_valid"] is True
        assert result["status"] == "submitting"
        assert result["poll_after_seconds"] == 3
        assert result["slug"] == "test-landing"
        # After awaiting the background task: the draft is published.
        record = drafts.get(design_id, DEFAULT_USER)
        assert record.status == "published"
        assert record.commit_sha
        assert Path(record.design_dir).exists()
        assert (Path(record.design_dir) / "test-landing.html").exists()
        assert (Path(record.design_dir) / "page-meta.yaml").exists()
        assert record.last_error is None

    def test_publish_true_returns_quickly_even_when_publish_blocks(self, temp_design_repo, monkeypatch):
        """submit_design must return in <200ms even if publish_design takes 5s."""

        def slow_publish(**kw):  # noqa: ARG001
            time.sleep(5.0)
            raise RuntimeError("test-injected: should not block the tool response")

        monkeypatch.setattr("design_mcp.server.publish_design", slow_publish)

        brief_resp = design_landing_page(brief="async timing check")
        design_id = brief_resp["design_id"]

        async def go():
            t0 = time.monotonic()
            result = await submit_design(
                design_id=design_id,
                html=_valid_html(),
                manifest=_valid_manifest(),
                publish=True,
            )
            elapsed = time.monotonic() - t0
            return result, elapsed

        result, elapsed = asyncio.run(go())
        assert result["status"] == "submitting"
        assert elapsed < 0.2, f"submit_design blocked the event loop for {elapsed:.3f}s"

    def test_background_task_marks_failed_on_publish_exception(self, temp_design_repo, monkeypatch):
        """If publish_design raises, the background task sets status=failed + last_error."""

        def boom(**kw):  # noqa: ARG001
            raise RuntimeError("simulated git push failure")

        monkeypatch.setattr("design_mcp.server.publish_design", boom)

        brief_resp = design_landing_page(brief="failure-path coverage")
        design_id = brief_resp["design_id"]

        async def go():
            result = await submit_design(
                design_id=design_id,
                html=_valid_html(),
                manifest=_valid_manifest(),
                publish=True,
            )
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return result

        result = asyncio.run(go())
        assert result["status"] == "submitting"  # initial accept
        rec = drafts.get(design_id, DEFAULT_USER)
        assert rec is not None
        assert rec.status == "failed"
        assert rec.last_error and "simulated git push failure" in rec.last_error

    def test_background_task_truncates_huge_last_error(self, temp_design_repo, monkeypatch):
        huge = "x" * 5000

        def boom(**kw):  # noqa: ARG001
            raise RuntimeError(huge)

        monkeypatch.setattr("design_mcp.server.publish_design", boom)

        brief_resp = design_landing_page(brief="truncate check")
        design_id = brief_resp["design_id"]

        async def go():
            await submit_design(
                design_id=design_id,
                html=_valid_html(),
                manifest=_valid_manifest(),
                publish=True,
            )
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(go())
        rec = drafts.get(design_id, DEFAULT_USER)
        assert rec.status == "failed"
        assert len(rec.last_error) <= 2000

    def test_publish_uses_authenticated_user_email_for_commit(self, temp_design_repo, monkeypatch):
        """Verify the git commit author is the authenticated user, not a caller-supplied value."""
        captured: dict[str, Any] = {}
        from design_mcp import repo as repo_mod
        real_publish = repo_mod.publish_design

        def spy_publish(**kw):
            captured.update(kw)
            return real_publish(**kw)

        monkeypatch.setattr("design_mcp.server.publish_design", spy_publish)

        brief_resp = design_landing_page(brief="Author check")
        design_id = brief_resp["design_id"]

        async def go():
            await submit_design(
                design_id=design_id,
                html=_valid_html(),
                manifest=_valid_manifest(),
                publish=True,
            )
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(go())
        assert captured["user_email"] == DEFAULT_USER

    def test_publish_false_marks_submitted_only_and_skips_background(self, temp_design_repo, monkeypatch):
        brief_resp = design_landing_page(brief="Preview only brief")
        design_id = brief_resp["design_id"]

        called = {"count": 0}

        def spy(**kw):  # noqa: ARG001
            called["count"] += 1
            return (Path("/tmp/never"), "deadbeef")

        monkeypatch.setattr("design_mcp.server.publish_design", spy)

        result = _submit(
            design_id=design_id,
            html=_valid_html(),
            manifest=_valid_manifest(),
            publish=False,
        )
        assert result["ok"] is True
        assert result["status"] == "submitted"
        assert result["manifest_valid"] is True
        assert result["poll_after_seconds"] == 0
        assert called["count"] == 0  # no background publish at all
        assert drafts.get(design_id, DEFAULT_USER).status == "submitted"

    def test_invalid_manifest_returns_structured_errors(self):
        brief_resp = design_landing_page(brief="Will fail validation")
        design_id = brief_resp["design_id"]
        bad = _valid_manifest()
        # Only 2 features — contract requires exactly 3.
        bad["features"] = bad["features"][:2]
        result = _submit(
            design_id=design_id, html=_valid_html(), manifest=bad, publish=False,
        )
        assert result["ok"] is False
        assert result["manifest_valid"] is False
        assert any("manifest validation failed" in e for e in result["errors"])
        # Draft is still in drafted state — caller can retry.
        assert drafts.get(design_id, DEFAULT_USER).status == "drafted"

    def test_html_missing_h1_returns_error(self):
        brief_resp = design_landing_page(brief="Missing h1 brief")
        design_id = brief_resp["design_id"]
        result = _submit(
            design_id=design_id,
            html="<!doctype html><html><head><title>x</title></head><body>no h1</body></html>",
            manifest=_valid_manifest(),
            publish=False,
        )
        assert result["ok"] is False
        assert any("h1" in e for e in result["errors"])

    def test_unknown_design_id_returns_not_found(self):
        result = _submit(
            design_id="00000000-0000-0000-0000-000000000000",
            html=_valid_html(),
            manifest=_valid_manifest(),
            publish=False,
        )
        assert result["ok"] is False
        assert result["status"] == "not-found"
        assert any("not owned by this user" in e for e in result["errors"])

    def test_cross_user_submit_blocked(self, temp_design_repo):
        """Design created by user A is invisible to user B at submit_design."""
        # Alice creates a draft.
        with _set_user("alice@x.com"):
            brief = design_landing_page(brief="Alice's brief")
            design_id = brief["design_id"]

        # Bob attempts to submit_design against Alice's design_id.
        with _set_user("bob@x.com"):
            result = _submit(
                design_id=design_id,
                html=_valid_html(),
                manifest=_valid_manifest(),
                publish=False,
            )
        assert result["ok"] is False
        assert result["status"] == "not-found"

        # The draft is still drafted for Alice — Bob did not affect it.
        record = drafts.get(design_id, "alice@x.com")
        assert record.status == "drafted"


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

    def test_get_design_status_returns_full_lifecycle_shape(self):
        """Async-submit polling expects status, last_error, commit_sha, design_dir,
        published_repo_sha, manifest_valid, iteration_count, ISO timestamps."""
        brief_resp = design_landing_page(brief="Lifecycle shape check")
        design_id = brief_resp["design_id"]
        result = get_design_status(design_id)
        assert result["ok"] is True
        for key in (
            "design_id", "status", "family", "slug", "user_email",
            "iteration_count", "manifest_valid",
            "commit_sha", "design_dir", "published_repo_sha", "last_error",
            "created_at", "updated_at", "expires_at",
        ):
            assert key in result, f"get_design_status missing key {key!r}"
        # Sensible defaults for a fresh draft.
        assert result["status"] == "drafted"
        assert result["family"] == "landing-page"
        assert result["last_error"] is None
        assert result["commit_sha"] is None
        assert result["design_dir"] is None
        assert result["published_repo_sha"] is None
        assert result["manifest_valid"] is None  # no manifest persisted yet
        assert result["iteration_count"] >= 1   # creation event recorded
        assert result["user_email"] == DEFAULT_USER

    def test_get_design_status_surfaces_last_error_after_failed_publish(self, temp_design_repo, monkeypatch):
        def boom(**kw):  # noqa: ARG001
            raise RuntimeError("repo unreachable")
        monkeypatch.setattr("design_mcp.server.publish_design", boom)

        brief_resp = design_landing_page(brief="surface last_error")
        design_id = brief_resp["design_id"]

        async def go():
            await submit_design(
                design_id=design_id,
                html=_valid_html(),
                manifest=_valid_manifest(),
                publish=True,
            )
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(go())
        result = get_design_status(design_id)
        assert result["ok"] is True
        assert result["status"] == "failed"
        assert result["last_error"] and "repo unreachable" in result["last_error"]

    def test_get_design_status_cross_user_returns_not_found(self):
        with _set_user("alice@x.com"):
            brief = design_landing_page(brief="Alice's status check")
            design_id = brief["design_id"]
        with _set_user("bob@x.com"):
            result = get_design_status(design_id)
        assert result["ok"] is False
        assert any("not owned by this user" in e for e in result["errors"])

    def test_cancel_design_marks_cancelled_and_retains_record(self):
        brief_resp = design_landing_page(brief="To be cancelled")
        design_id = brief_resp["design_id"]
        result = cancel_design(design_id, reason="user changed their mind")
        assert result["ok"] is True
        assert result["status"] == "cancelled"
        # Record must still exist.
        record = drafts.get(design_id, DEFAULT_USER)
        assert record.status == "cancelled"
        assert record.last_error == "user changed their mind"

    def test_cancel_design_cross_user_blocked(self):
        with _set_user("alice@x.com"):
            brief = design_landing_page(brief="Cant be cancelled by Bob")
            design_id = brief["design_id"]
        with _set_user("bob@x.com"):
            result = cancel_design(design_id, reason="malicious")
        assert result["ok"] is False
        # Alice's draft remains drafted.
        record = drafts.get(design_id, "alice@x.com")
        assert record.status == "drafted"

    def test_update_design_cross_user_blocked(self):
        with _set_user("alice@x.com"):
            brief = design_landing_page(brief="Alice iteration")
            design_id = brief["design_id"]
        with _set_user("bob@x.com"):
            result = update_design(design_id, instructions="hijack")
        assert result["ok"] is False

    def test_cancel_after_publish_is_rejected(self, temp_design_repo):
        brief_resp = design_landing_page(brief="Cant cancel after publish")
        design_id = brief_resp["design_id"]

        async def go():
            await submit_design(
                design_id=design_id,
                html=_valid_html(),
                manifest=_valid_manifest(),
                publish=True,
            )
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(go())
        # Background task should have flipped status to published.
        assert drafts.get(design_id, DEFAULT_USER).status == "published"
        result = cancel_design(design_id)
        assert result["ok"] is False
        assert result["status"] == "published"


# ---------------------------------------------------------------------------
# SeoBlock — new site_name field + tightened title length + render checks
# ---------------------------------------------------------------------------

class TestSeoBlock:
    def test_valid_site_name_and_title_accepted(self):
        from design_mcp.manifest import SeoBlock
        seo = SeoBlock(
            title="HealthBoost cover for over-50s",
            site_name="HealthBoost",
            meta_description="Health insurance comparison for Australians over 50 — quotes in 90 seconds.",
        )
        assert seo.title == "HealthBoost cover for over-50s"
        assert seo.site_name == "HealthBoost"

    def test_site_name_required(self):
        from pydantic import ValidationError
        from design_mcp.manifest import SeoBlock
        with pytest.raises(ValidationError):
            SeoBlock(
                title="Some bare title",
                meta_description="A long enough meta description to pass the 20-char minimum check.",
            )

    @pytest.mark.parametrize("site_name", ["", "ab", "x" * 51])
    def test_site_name_length_rejected(self, site_name):
        from pydantic import ValidationError
        from design_mcp.manifest import SeoBlock
        with pytest.raises(ValidationError):
            SeoBlock(
                title="Valid bare title",
                site_name=site_name,
                meta_description="A long enough meta description to pass the 20-char minimum check.",
            )

    @pytest.mark.parametrize("site_name", ["abc", "HealthBoost", "x" * 50])
    def test_site_name_length_accepted(self, site_name):
        from design_mcp.manifest import SeoBlock
        seo = SeoBlock(
            title="Valid bare title",
            site_name=site_name,
            meta_description="A long enough meta description to pass the 20-char minimum check.",
        )
        assert seo.site_name == site_name

    def test_title_max_length_tightened_to_60(self):
        from pydantic import ValidationError
        from design_mcp.manifest import SeoBlock
        # 61 chars should fail under the new ≤60 cap.
        with pytest.raises(ValidationError):
            SeoBlock(
                title="x" * 61,
                site_name="Acquirely",
                meta_description="A long enough meta description to pass the 20-char minimum check.",
            )

    def test_title_at_60_chars_accepted(self):
        from design_mcp.manifest import SeoBlock
        seo = SeoBlock(
            title="x" * 60,
            site_name="Acquirely",
            meta_description="A long enough meta description to pass the 20-char minimum check.",
        )
        assert len(seo.title) == 60

    def test_render_html_emits_title_suffix_and_og_url(self):
        """The fallback renderer must apply the {title} | {site_name} pattern
        in <title> only, keep og:title bare, and emit og:url + JSON-LD url."""
        from design_mcp.generators import landing_page as landing_gen
        m = LandingPageManifest(**_valid_manifest())
        html = landing_gen._render_html(m)
        # <title> carries the suffix.
        assert "<title>Test Landing — Day 3 Refactor | Acquirely Test</title>" in html
        # og:title stays BARE (no " | site_name" suffix).
        assert 'property="og:title" content="Test Landing — Day 3 Refactor"' in html
        # og:url present and equal to canonical (defaulted from slug).
        assert 'property="og:url"' in html
        # JSON-LD WebPage object includes a url key.
        assert '"url":' in html
        # Form posts to the corrected endpoint.
        assert 'action="/api/add-lead"' in html
        assert "/api/handle_Client_Lead_Submission" not in html
# Optional-section structured data: Testimonial / FaqItem / TrustBadge models
# plus the two-way contract on LandingPageManifest between
# `optional_sections` flags and their sibling payload fields.
# ---------------------------------------------------------------------------


def _testimonial(**overrides) -> dict[str, Any]:
    base = {
        "quote": "Sold the place in nine days, $82k above the reserve, calmest auction we have ever sat through.",
        "author": "Priya Singh",
        "location": "Parramatta NSW",
        "outcome": "Sold $82k above reserve",
    }
    base.update(overrides)
    return base


def _faq(**overrides) -> dict[str, Any]:
    base = {
        "question": "How long does the appraisal take?",
        "answer": "About 30 minutes on site plus a follow-up call within 48 hours with the written report.",
    }
    base.update(overrides)
    return base


def _badge(**overrides) -> dict[str, Any]:
    base = {
        "label": "REIA member 2024",
        "detail": "4.8★ on Google · 1,200 reviews",
    }
    base.update(overrides)
    return base


class TestOptionalSectionModels:
    def test_testimonial_round_trip(self):
        t = Testimonial(**_testimonial())
        assert t.quote.startswith("Sold the place")
        assert t.author == "Priya Singh"
        assert t.location == "Parramatta NSW"
        assert t.outcome == "Sold $82k above reserve"

    def test_testimonial_quote_too_short_rejected(self):
        with pytest.raises(ValidationError):
            Testimonial(**_testimonial(quote="too short"))  # < 20 chars

    def test_testimonial_quote_too_long_rejected(self):
        with pytest.raises(ValidationError):
            Testimonial(**_testimonial(quote="x" * 401))  # > 400 chars

    def test_testimonial_author_too_short_rejected(self):
        with pytest.raises(ValidationError):
            Testimonial(**_testimonial(author="X"))  # < 2 chars

    def test_testimonial_location_and_outcome_optional(self):
        t = Testimonial(quote="A" * 25, author="Jane Doe")
        assert t.location is None
        assert t.outcome is None

    def test_faq_round_trip(self):
        f = FaqItem(**_faq())
        assert f.question.endswith("?")
        assert len(f.answer) >= 20

    def test_faq_question_too_short_rejected(self):
        with pytest.raises(ValidationError):
            FaqItem(question="short?", answer="x" * 25)

    def test_faq_answer_too_short_rejected(self):
        with pytest.raises(ValidationError):
            FaqItem(question="A reasonable question?", answer="short")

    def test_trust_badge_round_trip(self):
        b = TrustBadge(**_badge())
        assert b.label == "REIA member 2024"
        assert b.icon_url is None
        assert b.detail == "4.8★ on Google · 1,200 reviews"

    def test_trust_badge_label_too_short_rejected(self):
        with pytest.raises(ValidationError):
            TrustBadge(label="X")

    def test_trust_badge_icon_and_detail_optional(self):
        b = TrustBadge(label="Trusted Partner")
        assert b.icon_url is None
        assert b.detail is None


class TestLandingPageOptionalSectionsValidator:
    def _base(self) -> dict[str, Any]:
        return _valid_manifest()

    def test_no_optional_sections_no_data_is_valid(self):
        LandingPageManifest(**self._base())  # smoke: backwards-compat

    def test_enabling_testimonials_without_data_fails(self):
        bad = self._base()
        bad["optional_sections"] = ["testimonials"]
        with pytest.raises(ValidationError) as ei:
            LandingPageManifest(**bad)
        assert "testimonials" in str(ei.value)

    def test_enabling_testimonials_with_one_item_fails_min(self):
        bad = self._base()
        bad["optional_sections"] = ["testimonials"]
        bad["testimonials"] = [_testimonial()]  # only 1 (min 2)
        with pytest.raises(ValidationError) as ei:
            LandingPageManifest(**bad)
        assert "2-6" in str(ei.value)

    def test_enabling_testimonials_with_seven_items_fails_max(self):
        bad = self._base()
        bad["optional_sections"] = ["testimonials"]
        bad["testimonials"] = [_testimonial() for _ in range(7)]
        with pytest.raises(ValidationError) as ei:
            LandingPageManifest(**bad)
        assert "2-6" in str(ei.value)

    def test_orphan_testimonials_without_flag_fails(self):
        bad = self._base()
        bad["optional_sections"] = []  # flag NOT set
        bad["testimonials"] = [_testimonial(), _testimonial()]
        with pytest.raises(ValidationError) as ei:
            LandingPageManifest(**bad)
        assert "orphan" in str(ei.value) or "without" in str(ei.value)

    def test_enabling_faq_without_data_fails(self):
        bad = self._base()
        bad["optional_sections"] = ["faq"]
        with pytest.raises(ValidationError):
            LandingPageManifest(**bad)

    def test_enabling_faq_with_two_items_fails_min(self):
        bad = self._base()
        bad["optional_sections"] = ["faq"]
        bad["faq"] = [_faq(), _faq(question="Another good question, is it?")]
        with pytest.raises(ValidationError) as ei:
            LandingPageManifest(**bad)
        assert "3-10" in str(ei.value)

    def test_orphan_faq_without_flag_fails(self):
        bad = self._base()
        bad["faq"] = [_faq(), _faq(), _faq()]
        with pytest.raises(ValidationError):
            LandingPageManifest(**bad)

    def test_enabling_trust_badges_without_data_fails(self):
        bad = self._base()
        bad["optional_sections"] = ["trust_badges"]
        with pytest.raises(ValidationError):
            LandingPageManifest(**bad)

    def test_enabling_trust_badges_with_two_items_fails_min(self):
        bad = self._base()
        bad["optional_sections"] = ["trust_badges"]
        bad["trust_badges"] = [_badge(), _badge(label="Award Winner")]
        with pytest.raises(ValidationError) as ei:
            LandingPageManifest(**bad)
        assert "3-8" in str(ei.value)

    def test_orphan_trust_badges_without_flag_fails(self):
        bad = self._base()
        bad["trust_badges"] = [_badge(), _badge(), _badge()]
        with pytest.raises(ValidationError):
            LandingPageManifest(**bad)

    def test_all_three_sections_enabled_and_populated_is_valid(self):
        good = self._base()
        good["optional_sections"] = ["testimonials", "faq", "trust_badges"]
        good["testimonials"] = [_testimonial() for _ in range(2)]
        good["faq"] = [_faq() for _ in range(3)]
        good["trust_badges"] = [_badge() for _ in range(3)]
        m = LandingPageManifest(**good)
        assert len(m.testimonials) == 2
        assert len(m.faq) == 3
        assert len(m.trust_badges) == 3

    def test_max_bounds_inclusive_are_valid(self):
        good = self._base()
        good["optional_sections"] = ["testimonials", "faq", "trust_badges"]
        good["testimonials"] = [_testimonial() for _ in range(6)]
        good["faq"] = [_faq() for _ in range(10)]
        good["trust_badges"] = [_badge() for _ in range(8)]
        LandingPageManifest(**good)  # should not raise

    def test_sticky_cta_mobile_flag_carries_no_payload(self):
        good = self._base()
        good["optional_sections"] = ["sticky_cta_mobile"]
        # No sibling payload required for this flag — must remain valid.
        LandingPageManifest(**good)


class TestSanityCheckHelper:
    def test_static_items_only_when_no_optional_sections(self):
        m = LandingPageManifest(**_valid_manifest())
        items = landing_gen.sanity_check_items_for_manifest(m)
        joined = " | ".join(items)
        assert "testimonials data populated" not in joined
        assert "faq data populated" not in joined
        assert "trust_badges data populated" not in joined

    def test_appends_per_section_items_when_flags_set(self):
        data = _valid_manifest()
        data["optional_sections"] = ["testimonials", "faq", "trust_badges"]
        data["testimonials"] = [_testimonial() for _ in range(3)]
        data["faq"] = [_faq() for _ in range(4)]
        data["trust_badges"] = [_badge() for _ in range(5)]
        m = LandingPageManifest(**data)
        items = landing_gen.sanity_check_items_for_manifest(m)
        joined = " | ".join(items)
        assert "testimonials data populated ✓ (3 items)" in joined
        assert "faq data populated ✓ (4 items)" in joined
        assert "trust_badges data populated ✓ (5 items)" in joined


class TestInstructionsCarryOptionalSectionsGuidance:
    def test_landing_brief_mentions_optional_sections_content_field(self):
        text = _instructions_for("landing-page", "anything")
        assert "optional_sections_content" in text

    def test_landing_brief_mentions_section_counts(self):
        text = _instructions_for("landing-page", "anything")
        assert "2-6" in text
        assert "3-10" in text
        assert "3-8" in text

    def test_landing_brief_mentions_testimonials_faq_trust_badges(self):
        text = _instructions_for("landing-page", "anything")
        lower = text.lower()
        assert "testimonials" in lower
        assert "faq" in lower
        assert "trust badges" in lower or "trust_badges" in lower

    def test_landing_brief_mentions_populated(self):
        text = _instructions_for("landing-page", "anything")
        assert "populated" in text.lower()

    def test_landing_brief_default_skips_optional_sections(self):
        from design_mcp.generators._brief_template import LANDING_PAGE_DEFAULTS
        assert (
            LANDING_PAGE_DEFAULTS.get("optional_sections_content")
            == "no optional sections"
        )
