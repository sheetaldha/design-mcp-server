"""FastMCP server — return-prompts pattern (no LLM on the server).

The server hands the caller a structured brief (instructions + contract +
manifest JSON schema) and a `design_id`. The caller's Claude generates the
HTML + manifest using its own subscription, then submits both back via
`submit_design`. The server validates, optionally commits to the
microsite-design-skills repo, and tracks lifecycle in a draft store.

Tools exposed:
    design_ping            — health check
    design_landing_page    — kick off a landing-page draft (returns brief)
    submit_design          — validate + commit a completed design
    update_design          — issue iteration instructions for a draft
    get_design_status      — inspect a draft
    cancel_design          — void a draft (records audit trail)

Day 5 will switch from stdio to HTTP/SSE for claude.ai web access.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

import yaml
from mcp.server.fastmcp import FastMCP

from . import drafts
from .config import DesignConfig
from .generators import landing_page as landing_gen
from .generators import survey_funnel as survey_gen
from .manifest import LandingPageManifest
from .repo import publish_design

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

# Honor HOST/PORT from env for production HTTP transport.
# FastMCP's internal Uvicorn server reads these from constructor kwargs.
import os as _os
mcp = FastMCP(
    "design-mcp-server",
    host=_os.getenv("HOST", "127.0.0.1"),
    port=int(_os.getenv("PORT", "8050")),
)


# ---------------------------------------------------------------------------
# Family registry — maps family slug → manifest class.
# Survey-funnel is added lazily so this server still imports cleanly while
# Agent 2 is mid-flight on contracts/survey_funnel.yaml + SurveyFunnelManifest.
# ---------------------------------------------------------------------------

def _manifest_class_for(family: str):
    if family == "landing-page":
        return LandingPageManifest
    if family == "survey-funnel":
        try:
            from .manifest import SurveyFunnelManifest  # type: ignore[attr-defined]
        except ImportError as exc:
            raise ValueError(
                "survey-funnel family is not available yet "
                "(SurveyFunnelManifest not in design_mcp.manifest)"
            ) from exc
        return SurveyFunnelManifest
    raise ValueError(f"unknown family {family!r}")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@mcp.tool()
def design_ping() -> dict:
    """Health check — confirms the MCP server is up and config loads.

    Returns:
        {"mcp": "ok", "version": "...", "public_url": "...", ...}
    """
    from . import __version__

    try:
        cfg = DesignConfig.from_env()
        return {
            "mcp": "ok",
            "version": __version__,
            "public_url": cfg.public_url,
            "mode": "return-prompts (no server-side LLM)",
            "design_repo": cfg.design_repo_ssh,
        }
    except RuntimeError as e:
        return {"mcp": "ok", "config_error": str(e)}


# ---------------------------------------------------------------------------
# design_landing_page — return-prompts entrypoint
# ---------------------------------------------------------------------------

@mcp.tool()
def design_landing_page(
    brief: str,
    references: Optional[list[str]] = None,
    slug: Optional[str] = None,
) -> dict:
    """Start a Landing Page design. Returns a brief the caller's Claude will act on.

    The caller (claude.ai web/mobile or Claude Code) reads the returned
    `instructions`, `contract` and `manifest_schema`, generates the HTML +
    manifest, then calls `submit_design(design_id, html, manifest)`.

    Args:
        brief: what the site sells, target audience, tone, color preferences
        references: optional list of URLs / image refs / inspiration notes
        slug: optional slug override; default is auto-slugified from the brief

    Returns:
        {
          "design_id": <uuid>,
          "family": "landing-page",
          "status": "drafted",
          "instructions": <plain-text instructions for the caller's Claude>,
          "contract": <parsed landing_page.yaml>,
          "manifest_schema": <Pydantic JSON schema>,
          "slug_hint": <kebab-case suggestion>,
          "expires_at": <ISO timestamp, 24h from now>,
          "next_action": "Call submit_design(design_id, html, manifest) when ready.",
        }
    """
    brief_payload = landing_gen.make_design_brief(
        design_id="pending",  # real id assigned by drafts.create below
        brief=brief,
        references=references,
        requested_slug=slug,
    )
    record = drafts.create(
        family="landing-page",
        brief=brief,
        slug_hint=brief_payload["slug_hint"],
    )
    return _entry_response(record, brief_payload)


@mcp.tool()
def design_survey_funnel(
    brief: str,
    references: Optional[list[str]] = None,
    slug: Optional[str] = None,
) -> dict:
    """Start a Survey Funnel design. Returns a brief the caller's Claude will act on.

    Same flow as design_landing_page but for the Survey Funnel family — multi-step
    form (1..5 steps), optional OTP gate, no branching DSL in v1. The caller's
    Claude generates the HTML + manifest, then calls submit_design(...).

    Args:
        brief: what the funnel qualifies for, target audience, tone, color, OTP yes/no
        references: optional URLs / inspiration / competitor sites
        slug: optional slug override; default auto-slugified from brief

    Returns:
        Same shape as design_landing_page, but family="survey-funnel" and
        manifest_schema is the SurveyFunnelManifest JSON schema.
    """
    brief_payload = survey_gen.make_design_brief(
        brief=brief,
        references=references,
        requested_slug=slug,
    )
    record = drafts.create(
        family="survey-funnel",
        brief=brief,
        slug_hint=brief_payload["slug_hint"],
    )
    return _entry_response(record, brief_payload)


def _entry_response(record, brief_payload: dict) -> dict:
    """Shared response shape for both design_* entry tools."""
    return {
        "design_id": record.design_id,
        "family": record.family,
        "status": record.status,
        "instructions": brief_payload["instructions"],
        "contract": brief_payload["contract"],
        "manifest_schema": brief_payload["manifest_schema"],
        "slug_hint": brief_payload["slug_hint"],
        "expires_at": record.expires_at.isoformat(),
        "next_action": (
            "Generate the HTML + manifest per the instructions, then call "
            f"submit_design(design_id='{record.design_id}', html=..., manifest=...)."
        ),
    }


# ---------------------------------------------------------------------------
# submit_design — caller posts the completed HTML + manifest for validation
# ---------------------------------------------------------------------------

def _html_sanity_check(html: str) -> list[str]:
    """Return a list of structural problems with the HTML. Empty = ok."""
    issues: list[str] = []
    lower = html.lower()
    if "<html" not in lower:
        issues.append("missing <html> root element")
    if "<title" not in lower:
        issues.append("missing <title> in <head>")
    h1_count = lower.count("<h1")
    if h1_count != 1:
        issues.append(f"expected exactly one <h1>, found {h1_count}")
    if "<body" not in lower:
        issues.append("missing <body> element")
    return issues


@mcp.tool()
def submit_design(
    design_id: str,
    html: str,
    manifest: dict,
    user_email: str = "sheetal@acquirely.com.au",
    publish: bool = True,
) -> dict:
    """Validate and (optionally) commit a generated design.

    Args:
        design_id: the id returned by design_landing_page / design_survey_funnel
        html: the full HTML5 document produced by the caller's Claude
        manifest: the parsed manifest dict (matches manifest_schema for the family)
        user_email: git attribution for the commit
        publish: when True (default) write + commit + push to microsite-design-skills

    Returns:
        On success: {ok: True, design_id, slug, family, status, html_size,
                     committed: bool, design_dir?, commit_sha?, manifest}
        On failure: {ok: False, design_id, errors: [...], status}
    """
    try:
        record = drafts.get(design_id)
    except KeyError as exc:
        return {"ok": False, "design_id": design_id, "errors": [str(exc)], "status": "not-found"}

    if record.status in {"published", "cancelled", "expired"}:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [f"draft already in terminal status {record.status!r}; start a new draft"],
            "status": record.status,
        }

    errors: list[str] = []

    # 1. Manifest validation via the family's Pydantic model.
    try:
        manifest_cls = _manifest_class_for(record.family)
    except ValueError as exc:
        return {"ok": False, "design_id": design_id, "errors": [str(exc)], "status": record.status}

    try:
        manifest_obj = manifest_cls(**manifest)
    except Exception as exc:  # Pydantic ValidationError + anything else
        errors.append(f"manifest validation failed: {exc}")

    # 2. HTML structural sanity.
    errors.extend(_html_sanity_check(html))

    if errors:
        drafts.update(design_id, last_error="; ".join(errors))
        return {
            "ok": False,
            "design_id": design_id,
            "errors": errors,
            "status": record.status,
            "hint": "Fix the issues above and call submit_design again with the corrected html + manifest.",
        }

    manifest_dict = manifest_obj.model_dump(mode="json")
    slug = manifest_dict.get("slug") or record.slug_hint or "untitled"
    chat_summary = _build_chat_summary(record.brief, slug, record.family, manifest_dict)

    result: dict[str, Any] = {
        "ok": True,
        "design_id": design_id,
        "slug": slug,
        "family": record.family,
        "html_size": len(html),
        "manifest": manifest_dict,
        "committed": False,
    }

    if publish:
        cfg = DesignConfig.from_env()
        manifest_yaml = yaml.dump(manifest_dict, sort_keys=False, default_flow_style=False)
        design_dir, sha = publish_design(
            cfg=cfg,
            slug=slug,
            html=html,
            manifest_yaml=manifest_yaml,
            chat_summary=chat_summary,
            user_email=user_email,
        )
        drafts.update(
            design_id,
            status="published",
            slug=slug,
            html=html,
            manifest=manifest_dict,
            chat_summary=chat_summary,
            commit_sha=sha,
            design_dir=str(design_dir),
            last_error=None,
        )
        result.update(
            committed=True,
            design_dir=str(design_dir),
            commit_sha=sha,
            status="published",
        )
    else:
        drafts.update(
            design_id,
            status="submitted",
            slug=slug,
            html=html,
            manifest=manifest_dict,
            chat_summary=chat_summary,
            last_error=None,
        )
        result["status"] = "submitted"

    return result


# ---------------------------------------------------------------------------
# update_design — return iteration instructions for the caller
# ---------------------------------------------------------------------------

@mcp.tool()
def update_design(design_id: str, instructions: str) -> dict:
    """Issue iteration instructions for an existing draft.

    The server does NOT regenerate anything. It returns guidance the
    caller's Claude uses to produce a revised HTML + manifest, which is
    then re-submitted via submit_design.

    Args:
        design_id: existing draft id
        instructions: natural-language refinements (e.g. "use darker greens,
                      tighten the headline, drop the trust badges section")
    """
    try:
        record = drafts.get(design_id)
    except KeyError as exc:
        return {"ok": False, "design_id": design_id, "errors": [str(exc)]}

    if record.status in {"published", "cancelled", "expired"}:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [f"cannot iterate on a draft in status {record.status!r}"],
        }

    # Reload contract for the family so the caller has it in hand again.
    if record.family == "landing-page":
        from .generators.landing_page import _load_contract  # noqa: WPS437 — internal helper
        contract = _load_contract()
        manifest_schema = LandingPageManifest.model_json_schema()
    else:
        contract = {}
        manifest_schema = {}

    drafts.update(
        design_id,
        status="drafted",  # iteration reopens the draft
        last_error=None,
    )

    iteration_prompt = (
        f"Apply these refinements to design {design_id}:\n\n"
        f"{instructions}\n\n"
        "Regenerate the HTML + manifest satisfying the SAME contract (re-attached "
        "below for reference). Then call:\n"
        f"    submit_design(design_id='{design_id}', html=..., manifest=...)\n"
        "with the new artefacts."
    )

    return {
        "ok": True,
        "design_id": design_id,
        "family": record.family,
        "current_status": "drafted",
        "iteration_instructions": iteration_prompt,
        "contract": contract,
        "manifest_schema": manifest_schema,
        "previous_manifest": record.manifest,
        "previous_slug": record.slug,
    }


# ---------------------------------------------------------------------------
# get_design_status — read-only inspection
# ---------------------------------------------------------------------------

@mcp.tool()
def get_design_status(design_id: str) -> dict:
    """Return the full DraftRecord for a design plus a human-readable summary."""
    try:
        record = drafts.get(design_id)
    except KeyError as exc:
        return {"ok": False, "design_id": design_id, "errors": [str(exc)]}

    data = record.to_dict()
    summary_lines = [
        f"design {record.design_id}",
        f"  family : {record.family}",
        f"  status : {record.status}",
        f"  created: {record.created_at.isoformat()}",
        f"  expires: {record.expires_at.isoformat()}",
        f"  slug   : {record.slug or record.slug_hint or '(none)'}",
    ]
    if record.commit_sha:
        summary_lines.append(f"  commit : {record.commit_sha}")
    if record.last_error:
        summary_lines.append(f"  error  : {record.last_error}")

    return {
        "ok": True,
        "design_id": record.design_id,
        "record": data,
        "summary": "\n".join(summary_lines),
    }


# ---------------------------------------------------------------------------
# cancel_design — soft delete (keeps record for audit)
# ---------------------------------------------------------------------------

@mcp.tool()
def cancel_design(design_id: str, reason: Optional[str] = None) -> dict:
    """Mark a draft as cancelled. Record is retained for audit."""
    try:
        record = drafts.get(design_id)
    except KeyError as exc:
        return {"ok": False, "design_id": design_id, "errors": [str(exc)]}

    if record.status == "published":
        return {
            "ok": False,
            "design_id": design_id,
            "errors": ["cannot cancel a design that is already published"],
            "status": "published",
        }
    if record.status == "cancelled":
        return {"ok": True, "design_id": design_id, "status": "cancelled", "note": "already cancelled"}

    drafts.update(design_id, status="cancelled", last_error=reason)
    return {
        "ok": True,
        "design_id": design_id,
        "status": "cancelled",
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_chat_summary(brief: str, slug: str, family: str, manifest_dict: dict) -> str:
    parts = [
        f"# Design chat — {slug}",
        "",
        "## Brief",
        brief,
        "",
        "## Output",
        f"- family: {family}",
        f"- slug: {slug}",
    ]
    if family == "landing-page":
        intent = manifest_dict.get("intent", "")
        features = manifest_dict.get("features") or []
        optional = manifest_dict.get("optional_sections") or []
        theme = manifest_dict.get("theme") or {}
        parts.extend([
            f"- intent: {intent}",
            f"- features: {len(features)}",
            f"- optional sections: {', '.join(optional) or '(none)'}",
            f"- theme: primary={theme.get('color_primary')}, accent={theme.get('color_accent')}",
        ])
    return "\n".join(parts) + "\n"


def main() -> None:
    """Entry point — transport chosen via DESIGN_MCP_TRANSPORT env var.

    Local dev (default):  stdio              — for Claude Code local clients
    EC2 production:       streamable-http    — for claude.ai web/mobile Custom Connectors
    Legacy fallback:      sse                — older HTTP transport variant
    """
    import os
    transport = os.getenv("DESIGN_MCP_TRANSPORT", "stdio").lower()
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise SystemExit(
            f"invalid DESIGN_MCP_TRANSPORT={transport!r}; "
            f"use one of: stdio, sse, streamable-http"
        )
    log.info("design-mcp-server starting (transport=%s, return-prompts mode)", transport)
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
