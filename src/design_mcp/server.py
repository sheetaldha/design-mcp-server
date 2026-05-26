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
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import auth as auth_mod
from . import drafts
from .auth import AuthError, hash_token, new_opaque_token
from .config import DesignConfig, redact_url
from .db import get_conn
from .generators import landing_page as landing_gen
from .generators import survey_funnel as survey_gen
from .manifest import LandingPageManifest
from . import oauth_provider as _op
from .oauth_provider import (
    OAuthProvider,
    consume_csrf_token,
    issue_csrf_token,
    render_login_form,
    verify_oauth_state,
)
from .repo import publish_design
from .token_verifier import DESIGN_WRITE_SCOPE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

# Honor HOST/PORT from env for production HTTP transport.
# FastMCP's internal Uvicorn server reads these from constructor kwargs.
import os as _os

# DNS-rebinding protection is auto-enabled when host=127.0.0.1, which rejects
# our public Host header coming through nginx. Override with an explicit
# allow-list of the public hostname + loopback. PUBLIC_HOSTNAMES is a
# comma-separated list of bare hostnames (no scheme).
_public_hostnames = [
    h.strip() for h in _os.getenv("PUBLIC_HOSTNAMES", "design-mcp.leadloom.com.au").split(",") if h.strip()
]
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[*_public_hostnames, "127.0.0.1:*", "localhost:*", "[::1]:*"],
    allowed_origins=[
        *(f"https://{h}" for h in _public_hostnames),
        "https://claude.ai",
        "http://127.0.0.1:*",
        "http://localhost:*",
    ],
)

_public_url = _os.getenv("PUBLIC_URL", "https://design-mcp.leadloom.com.au")
_auth_settings = AuthSettings(
    issuer_url=_public_url,  # type: ignore[arg-type]
    resource_server_url=_public_url,  # type: ignore[arg-type]
    required_scopes=[DESIGN_WRITE_SCOPE],
    client_registration_options=ClientRegistrationOptions(
        enabled=True,
        valid_scopes=[DESIGN_WRITE_SCOPE],
        default_scopes=[DESIGN_WRITE_SCOPE],
    ),
    revocation_options=RevocationOptions(enabled=True),
)

_oauth_provider = OAuthProvider(
    public_url=_public_url, default_scopes=[DESIGN_WRITE_SCOPE],
)

mcp = FastMCP(
    "design-mcp-server",
    host=_os.getenv("HOST", "127.0.0.1"),
    port=int(_os.getenv("PORT", "8050")),
    transport_security=_security,
    auth=_auth_settings,
    auth_server_provider=_oauth_provider,
)


# ---------------------------------------------------------------------------
# /authorize/login — HTML form that converts an invite token into an OAuth
# authorization code. Mounted via custom_route so it bypasses bearer auth.
# ---------------------------------------------------------------------------

_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _no_cache_html(body: str, status_code: int = 200) -> HTMLResponse:
    """HTMLResponse with M4 cache-suppression headers always applied."""
    return HTMLResponse(body, status_code=status_code, headers=dict(_NO_CACHE_HEADERS))


def _origin_referer_ok(request: Request) -> bool:
    """Reject cross-site POSTs: Origin must equal our public_url; Referer,
    if present, must share the same scheme+host+port. Absent Referer is
    allowed (some browsers / Privacy Badger strip it on same-origin POSTs).
    """
    expected = _public_url.rstrip("/")
    origin = request.headers.get("origin")
    if origin is None or origin.rstrip("/") != expected:
        return False
    referer = request.headers.get("referer")
    if referer:
        from urllib.parse import urlsplit
        try:
            r = urlsplit(referer)
            e = urlsplit(expected)
        except ValueError:
            return False
        if (r.scheme, r.hostname, r.port) != (e.scheme, e.hostname, e.port):
            return False
    return True


def _client_cancel_redirect(payload: dict) -> RedirectResponse:
    """RFC 6749 §4.1.2.1 — user denied. Return access_denied to the client."""
    from urllib.parse import urlencode
    qp: dict[str, str] = {"error": "access_denied"}
    if payload.get("state"):
        qp["state"] = payload["state"]
    target = payload["redirect_uri"]
    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        url=f"{target}{sep}{urlencode(qp)}",
        status_code=302,
        headers=dict(_NO_CACHE_HEADERS),
    )


@mcp.custom_route("/authorize/login", methods=["GET", "POST"])
async def authorize_login(request: Request) -> Response:
    # ----- GET — render the consent screen --------------------------------
    if request.method == "GET":
        blob = request.query_params.get("oauth_state", "")
        try:
            payload = verify_oauth_state(blob)
        except ValueError:
            return _no_cache_html(
                "<h1>Invalid authorization request</h1>"
                "<p>The OAuth state could not be verified. Restart the flow from your client.</p>",
                status_code=400,
            )
        client = await _oauth_provider.get_client(payload["client_id"])
        client_name = client.client_name if client else payload["client_id"]
        csrf = await issue_csrf_token(blob)
        return _no_cache_html(
            render_login_form(
                client_name=client_name or "an OAuth client",
                scopes=payload.get("scopes") or [DESIGN_WRITE_SCOPE],
                oauth_state=blob,
                csrf_token=csrf,
                redirect_uri=payload["redirect_uri"],
            )
        )

    # ----- POST — same-origin + CSRF + consent + invite token -------------
    if not _origin_referer_ok(request):
        log.warning(
            "authorize_login POST blocked: origin=%r referer=%r",
            request.headers.get("origin"), request.headers.get("referer"),
        )
        return _no_cache_html(
            "<h1>Request blocked</h1>"
            "<p>This request did not come from the authorization page. "
            "Restart the flow from your client.</p>",
            status_code=403,
        )

    form = await request.form()
    blob = str(form.get("oauth_state") or "")
    invite_token = str(form.get("invite_token") or "")
    csrf_token = str(form.get("csrf_token") or "")
    consent = str(form.get("consent") or "")
    action = str(form.get("action") or "authorize")

    try:
        payload = verify_oauth_state(blob)
    except ValueError:
        return _no_cache_html(
            "<h1>Invalid authorization request</h1>", status_code=400,
        )

    # Consume the CSRF token whether the user cancelled or authorised.
    csrf_ok = await consume_csrf_token(csrf_token, blob)
    if not csrf_ok:
        return _no_cache_html(
            "<h1>Authorization expired</h1>"
            "<p>The authorization page expired or was reused. Restart the flow from your client.</p>",
            status_code=403,
        )

    # Cancel path — short-circuit before touching invite token / DB.
    if action == "cancel":
        return _client_cancel_redirect(payload)

    if consent != "yes":
        return _no_cache_html(
            "<h1>Consent required</h1>"
            "<p>You must check the &ldquo;I authorize this connection&rdquo; box to continue.</p>",
            status_code=400,
        )

    client = await _oauth_provider.get_client(payload["client_id"])
    client_name = client.client_name if client else payload["client_id"]

    import asyncio as _asyncio
    try:
        info = await _asyncio.to_thread(auth_mod.validate_token, invite_token)
    except AuthError as exc:
        log.warning("authorize_login: invite-token rejected: %s", exc)
        # Re-render with a fresh CSRF token so the user can retry.
        new_csrf = await issue_csrf_token(blob)
        return _no_cache_html(
            render_login_form(
                client_name=client_name or "an OAuth client",
                scopes=payload.get("scopes") or [DESIGN_WRITE_SCOPE],
                oauth_state=blob,
                csrf_token=new_csrf,
                redirect_uri=payload["redirect_uri"],
                error="Invite token is invalid, revoked, or malformed.",
            ),
            status_code=401,
        )

    raw_code = new_opaque_token()
    await _asyncio.to_thread(
        _op._store_auth_code,
        raw_code=raw_code,
        client_id=payload["client_id"],
        user_email=info.user_email,
        redirect_uri=payload["redirect_uri"],
        redirect_uri_explicit=payload.get("redirect_uri_explicit", True),
        code_challenge=payload["code_challenge"],
        code_challenge_method=payload.get("code_challenge_method", "S256"),
        scopes=payload.get("scopes") or [DESIGN_WRITE_SCOPE],
    )

    from urllib.parse import urlencode
    qp: dict[str, str] = {"code": raw_code}
    if payload.get("state"):
        qp["state"] = payload["state"]
    sep = "&" if "?" in payload["redirect_uri"] else "?"
    return RedirectResponse(
        url=f"{payload['redirect_uri']}{sep}{urlencode(qp)}",
        status_code=302,
        headers=dict(_NO_CACHE_HEADERS),
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
# Authenticated user resolution
#
# The MCP SDK's bearer-auth path stores the AccessToken in a contextvar via
# `mcp.server.auth.middleware.auth_context`. The AccessToken model itself
# doesn't carry user_email, so we look it up from the raw bearer token:
#   1. design_mcp_oauth_access_tokens (OAuth grant path) — has user_email
#   2. design_mcp_tokens              (legacy invite-token path) — has user_email
# The lookup is cached for the lifetime of the request via the AccessToken
# instance (a `__user_email` attribute) so we don't hit the DB on every tool.
# ---------------------------------------------------------------------------

class AuthContextError(RuntimeError):
    """Raised when a tool handler can't resolve the authenticated user."""


def _lookup_user_email_in_oauth_table(raw_token: str) -> Optional[str]:
    """Return user_email if raw_token matches an OAuth access token row."""
    h = hash_token(raw_token)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_email
              FROM design_mcp_oauth_access_tokens
             WHERE token_hash = %s AND revoked_at IS NULL
            """,
            (h,),
        )
        row = cur.fetchone()
    return row["user_email"] if row else None


def _lookup_user_email_in_invite_table(raw_token: str) -> Optional[str]:
    """Return user_email if raw_token matches a design_mcp_tokens row (no usage bump)."""
    h = hash_token(raw_token)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_email
              FROM design_mcp_tokens
             WHERE token_hash = %s AND revoked_at IS NULL
            """,
            (h,),
        )
        row = cur.fetchone()
    return row["user_email"] if row else None


def resolve_user_email() -> str:
    """Resolve the authenticated user's email from the current request context.

    Raises AuthContextError if no auth context is present or if the bearer
    token cannot be mapped to a known user. Cached on the AccessToken
    instance for the lifetime of a single request.
    """
    access_token = get_access_token()
    if access_token is None:
        raise AuthContextError(
            "no authenticated user — tool requires a bearer token"
        )

    cached = getattr(access_token, "__user_email", None)
    if cached:
        return cached

    raw = access_token.token
    email = _lookup_user_email_in_oauth_table(raw) or _lookup_user_email_in_invite_table(raw)
    if not email:
        raise AuthContextError(
            f"bearer token did not resolve to a known user "
            f"(client_id={access_token.client_id!r})"
        )
    try:
        object.__setattr__(access_token, "__user_email", email)
    except (AttributeError, TypeError):
        pass
    return email


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@mcp.tool()
def design_ping() -> dict:
    """Health check — confirms the MCP server is up, config loads, auth resolves.

    Returns:
        {"mcp": "ok", "version": "...", "public_url": "...", "user_email": "..."}
    """
    from . import __version__

    # Confirm the auth context is reachable. design_ping is a debug/health
    # tool — return the resolved email so callers can sanity-check their
    # token, but never fail the health check just because auth is absent.
    try:
        user_email: Optional[str] = resolve_user_email()
    except AuthContextError:
        user_email = None

    try:
        cfg = DesignConfig.from_env()
        return {
            "mcp": "ok",
            "version": __version__,
            "public_url": cfg.public_url,
            "mode": "return-prompts (no server-side LLM)",
            "design_repo": redact_url(cfg.design_repo_ssh),
            "user_email": user_email,
        }
    except RuntimeError as e:
        return {"mcp": "ok", "config_error": str(e), "user_email": user_email}


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
    user_email = resolve_user_email()
    brief_payload = landing_gen.make_design_brief(
        design_id="pending",  # real id assigned by drafts.create below
        brief=brief,
        references=references,
        requested_slug=slug,
    )
    record = drafts.create(
        user_email=user_email,
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
    user_email = resolve_user_email()
    brief_payload = survey_gen.make_design_brief(
        brief=brief,
        references=references,
        requested_slug=slug,
    )
    record = drafts.create(
        user_email=user_email,
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
    publish: bool = True,
) -> dict:
    """Validate and (optionally) commit a generated design.

    The git commit author is derived from the authenticated user — the
    caller cannot spoof attribution.

    Args:
        design_id: the id returned by design_landing_page / design_survey_funnel
        html: the full HTML5 document produced by the caller's Claude
        manifest: the parsed manifest dict (matches manifest_schema for the family)
        publish: when True (default) write + commit + push to microsite-design-skills

    Returns:
        On success: {ok: True, design_id, slug, family, status, html_size,
                     committed: bool, design_dir?, commit_sha?, manifest}
        On failure: {ok: False, design_id, errors: [...], status}
    """
    user_email = resolve_user_email()
    record = drafts.get(design_id, user_email)
    if record is None:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [
                f"design_id {design_id!r} not found or not owned by this user."
            ],
            "status": "not-found",
        }

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
        drafts.update(design_id, user_email, last_error="; ".join(errors))
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
            user_email,
            status="published",
            slug=slug,
            html=html,
            manifest=manifest_dict,
            chat_summary=chat_summary,
            commit_sha=sha,
            published_repo_sha=sha,
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
            user_email,
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
    user_email = resolve_user_email()
    record = drafts.get(design_id, user_email)
    if record is None:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [
                f"design_id {design_id!r} not found or not owned by this user."
            ],
        }

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
        user_email,
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
    user_email = resolve_user_email()
    record = drafts.get(design_id, user_email)
    if record is None:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [
                f"design_id {design_id!r} not found or not owned by this user."
            ],
        }

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
    user_email = resolve_user_email()
    record = drafts.get(design_id, user_email)
    if record is None:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [
                f"design_id {design_id!r} not found or not owned by this user."
            ],
        }

    if record.status == "published":
        return {
            "ok": False,
            "design_id": design_id,
            "errors": ["cannot cancel a design that is already published"],
            "status": "published",
        }
    if record.status == "cancelled":
        return {"ok": True, "design_id": design_id, "status": "cancelled", "note": "already cancelled"}

    drafts.update(design_id, user_email, status="cancelled", last_error=reason)
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
