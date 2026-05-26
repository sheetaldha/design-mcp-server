"""Day 3 refactor — return-prompts pattern + iteration tools, PG-backed drafts,
auth-derived user_email.

Covers:
- design_landing_page returns the expected structured brief shape
- drafts.create / get / update / set_status / cleanup_expired work
- Cross-user isolation: user A's design_id is invisible to user B
- create() persists user_email
- get() returns None when user_email doesn't match
- record_submission + mark_published helpers behave
- submit_design derives user_email from auth context (mocks two users)
- submit_design validates manifest + HTML and round-trips through a
  bare git repo (publish=True) without touching the real microsite-design-skills
- submit_design with publish=False stops at "submitted" status
- submit_design surfaces structured errors when the manifest is invalid
- update_design returns iteration instructions and reopens the draft
- get_design_status returns the record + summary
- cancel_design soft-deletes without removing the record
- cleanup_expired removes only expired rows
- Restart resilience: re-instantiating the backend with the same store
  surfaces previously-persisted drafts
- AuthContextError raised when no auth context is in scope
"""

from __future__ import annotations

import os
import subprocess
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
from design_mcp.manifest import LandingPageManifest  # noqa: E402
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
        record = drafts.get(design_id, DEFAULT_USER)
        assert record.status == "published"
        assert record.commit_sha == result["commit_sha"]

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
        submit_design(
            design_id=design_id,
            html=_valid_html(),
            manifest=_valid_manifest(),
            publish=True,
        )
        assert captured["user_email"] == DEFAULT_USER

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
        assert drafts.get(design_id, DEFAULT_USER).status == "submitted"

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
        assert drafts.get(design_id, DEFAULT_USER).status == "drafted"

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
        assert any("not owned by this user" in e for e in result["errors"])

    def test_cross_user_submit_blocked(self, temp_design_repo):
        """Design created by user A is invisible to user B at submit_design."""
        # Alice creates a draft.
        with _set_user("alice@x.com"):
            brief = design_landing_page(brief="Alice's brief")
            design_id = brief["design_id"]

        # Bob attempts to submit_design against Alice's design_id.
        with _set_user("bob@x.com"):
            result = submit_design(
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
        submit_design(
            design_id=design_id,
            html=_valid_html(),
            manifest=_valid_manifest(),
            publish=True,
        )
        result = cancel_design(design_id)
        assert result["ok"] is False
        assert result["status"] == "published"
