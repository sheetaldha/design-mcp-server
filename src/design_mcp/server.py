"""FastMCP server — return-prompts pattern (no LLM on the server).

The server hands the caller a structured brief (instructions + contract +
manifest JSON schema) and a `design_id`. The caller's Claude generates the
HTML + manifest using its own subscription, then submits both back via
`submit_design`. The server validates, optionally commits to the
microsite-design-skills repo, and tracks lifecycle in a draft store.

Tools exposed:
    design_ping            — health check
    start_landing_page_intake    — kick off a landing-page draft (returns brief)
    start_survey_funnel_intake   — kick off a survey-funnel draft (returns brief)
    submit_design          — validate + commit a completed design
    update_design          — issue iteration instructions for a draft
    get_design_status      — inspect a draft
    cancel_design          — void a draft (records audit trail)
    get_preview_url        — signed URL to open the generated HTML in a browser
    fetch_url_screenshots  — multi-provider screenshot of an external URL (3 viewports)

Day 5 will switch from stdio to HTTP/SSE for claude.ai web access.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
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
from . import images as images_mod
from .auth import AuthError, hash_token, new_opaque_token
from .config import DesignConfig, redact_url
from .db import get_conn
from .generators import landing_page as landing_gen
from .generators import survey_funnel as survey_gen
from . import intake_state_machine as intake_sm
from .manifest import LandingPageManifest
from . import oauth_provider as _op
from .oauth_provider import (
    OAuthProvider,
    consume_csrf_token,
    issue_csrf_token,
    render_login_form,
    verify_oauth_state,
)
from .preview import (
    DEFAULT_TTL_SECONDS as _PREVIEW_TTL_SECONDS,
    signed_preview_url,
    verify_preview_signature,
)
from .repo import publish_design
from .screenshots import ScreenshotError, fetch_screenshots
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

_SERVER_INSTRUCTIONS = """ACQUIRELY DESIGN MCP — CRITICAL USAGE RULES (read before calling ANY tool)

This MCP designs landing pages and survey funnels. ALL clarifying questions
are asked INSIDE the tools via a server-driven state machine. DO NOT
pre-interview the user under any circumstances.

When the user expresses ANY intent to design a landing page (e.g. "design a
landing page", "I want a page for my clinic", "start a new design"):
1. Call `start_landing_page_intake()` IMMEDIATELY with NO arguments.
2. Render the returned `next_question` payload VERBATIM via AskUserQuestion
   (or as plain text for checkpoints).
3. Call `submit_clarifying_answer(design_id, field_key, answer)` with the
   user's reply.
4. Loop until `next_question` is null.
5. THEN generate the HTML per `instructions_legacy`.

DO NOT ask the user any of these BEFORE calling the tool:
- "What is the page selling / what's the offer / product?"
- "Who is the target audience?"
- "What tone (professional / playful / etc.)?"
- "What colors / brand palette?"
- "Any references / inspiration?"
- "What's the goal / CTA?"
The server asks ALL of these itself, with curated wording and curated
multi-choice options. Asking them yourself BYPASSES the curated flow and
gives the user a worse, inconsistent experience.

Calling `start_landing_page_intake()` with NO arguments is the correct invocation.
It does NOT immediately generate a page — it starts an interactive intake
the server controls. The optional `brief` parameter is only for forwarding
a descriptive sentence the user ALREADY typed UNPROMPTED; never prompt for
it.

Same rules apply to `start_survey_funnel_intake()`.
"""

mcp = FastMCP(
    "design-mcp-server",
    instructions=_SERVER_INSTRUCTIONS,
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
# /preview/{design_id} — signed-URL HTML preview so users (especially on
# mobile) can open the generated page in a real browser before approving
# submit_design. Auth is the HMAC signature on the URL — no bearer token,
# no cookie, no DB-backed session. See design_mcp.preview for the format.
# ---------------------------------------------------------------------------

_PREVIEW_NO_INDEX_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
}


def _preview_error_html(title: str, body: str, status_code: int) -> HTMLResponse:
    html = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        "max-width:480px;margin:64px auto;padding:0 24px;color:#1d2026;}"
        "h1{font-size:20px;margin:0 0 12px;}"
        "p{color:#5f6b7a;font-size:14px;line-height:1.5;}</style>"
        f"</head><body><h1>{title}</h1><p>{body}</p></body></html>"
    )
    return HTMLResponse(
        html, status_code=status_code, headers=dict(_PREVIEW_NO_INDEX_HEADERS),
    )


@mcp.custom_route("/preview/{design_id}", methods=["GET"])
async def preview_design(request: Request) -> Response:
    design_id = request.path_params.get("design_id", "")
    exp_raw = request.query_params.get("exp", "")
    sig = request.query_params.get("sig", "")
    try:
        exp_int = int(exp_raw)
    except (TypeError, ValueError):
        return _preview_error_html(
            "Preview link expired or invalid",
            "This preview link is malformed. Ask the chat session to mint a fresh one with get_preview_url.",
            status_code=403,
        )

    pair = await asyncio.to_thread(drafts.get_draft_html, design_id)
    if pair is None:
        # Either no such draft, or the draft has no html yet. Surface 404 in
        # the latter case; we deliberately don't distinguish the two to avoid
        # confirming the existence of a design_id to an unauthenticated caller.
        # Either way we still must check the signature so a brute-force
        # design_id probe doesn't get a free oracle — verify with an empty
        # user_email (always fails) to keep timing similar.
        verify_preview_signature(design_id, exp_int, sig, user_email="")
        return _preview_error_html(
            "Design has no HTML yet",
            "submit_design hasn't been called on this draft, or the design_id is unknown.",
            status_code=404,
        )

    html, user_email = pair
    if not verify_preview_signature(design_id, exp_int, sig, user_email=user_email):
        return _preview_error_html(
            "Preview link expired or invalid",
            "This preview link has expired or its signature did not verify. "
            "Ask the chat session to mint a fresh one with get_preview_url.",
            status_code=403,
        )

    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers=dict(_PREVIEW_NO_INDEX_HEADERS),
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
# start_landing_page_intake — return-prompts entrypoint
# ---------------------------------------------------------------------------

@mcp.tool()
def start_landing_page_intake(
    brief: str = "",
    references: Optional[list[str]] = None,
    slug: Optional[str] = None,
) -> dict:
    """Kick off a Landing Page intake. CALL WITH NO ARGUMENTS — the server asks everything.

    ALL PARAMETERS ARE OPTIONAL. The normal invocation is
    `start_landing_page_intake()` with no arguments. The server runs a
    server-driven clarifying-question flow that asks the user every detail
    it needs (intent, brand, brief, CTA, palette, tone, etc.) — Claude does
    NOT need to gather any context before calling.

    DO NOT pre-interview the user. DO NOT ask "what it sells" / "target
    audience" / "tone" / "color preferences" / "references" before calling
    this tool. Those are clarifying questions the server will ask itself.

    The response carries `next_question` — render it VERBATIM (via
    AskUserQuestion or plain text per the payload) and call
    `submit_clarifying_answer(design_id, field_key, answer)` to advance.
    Loop until `next_question` is null, then proceed to the outline ->
    generate -> submit flow described in `instructions_legacy`.

    Args:
        brief: OPTIONAL forwarding hint. If the user happens to have typed a
               descriptive sentence (e.g. "I need a page for my dental
               clinic"), pass it here as-is and the server will pre-populate
               the site_brief field. Otherwise pass nothing — the server
               will ask for the brief as one of its clarifying questions.
               DO NOT prompt the user for a brief just to fill this in.
        references: OPTIONAL list of URLs / image refs the user already
                    provided. DO NOT prompt the user just to fill this in.
        slug: OPTIONAL slug override; server auto-generates otherwise.

    Returns:
        {
          "design_id": <uuid>,
          "family": "landing-page",
          "status": "drafted",
          "instructions_short": <~80-word directive about the new server-driven flow>,
          "instructions_legacy": <full prose runbook for outline/generate/submit>,
          "instructions": <alias for instructions_legacy — kept for back-compat>,
          "contract": <parsed landing_page.yaml>,
          "manifest_schema": <Pydantic JSON schema>,
          "slug_hint": <kebab-case suggestion>,
          "expires_at": <ISO timestamp, 24h from now>,
          "next_question": <NextQuestion dict — see intake_state_machine.NextQuestion>,
          "next_action": <imperative directing the caller to ask next_question + submit>,
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
    record = _seed_brief_into_state(record, user_email, brief)
    return _entry_response(record, brief_payload)


@mcp.tool()
def start_survey_funnel_intake(
    brief: str = "",
    references: Optional[list[str]] = None,
    slug: Optional[str] = None,
) -> dict:
    """Kick off a Survey Funnel intake. CALL WITH NO ARGUMENTS — server asks everything.

    ALL PARAMETERS ARE OPTIONAL. The normal invocation is
    `start_survey_funnel_intake()` with no arguments. DO NOT pre-interview the
    user before calling this tool — no "what does the funnel qualify for",
    no "tone", no "OTP yes/no". Follow the `instructions` payload after
    calling.

    Args:
        brief: OPTIONAL forwarding hint. Pass any descriptive sentence the
               user already typed; otherwise pass nothing. DO NOT prompt the
               user just to fill this in.
        references: OPTIONAL URLs / inspiration. DO NOT prompt for these.
        slug: OPTIONAL slug override; server auto-generates otherwise.

    Returns:
        Same shape as start_landing_page_intake, but family="survey-funnel" and
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
    record = _seed_brief_into_state(record, user_email, brief)
    return _entry_response(record, brief_payload)


def _seed_brief_into_state(record, user_email: str, brief: str):
    """Pre-populate the brief upload into clarifying_state so the review-first
    intake doesn't re-ask for it. No-op for an empty / whitespace brief.

    Shared by both family entry tools so the brief-first / skip-answered pass
    starts from the same place. Returns the (possibly refreshed) record.
    """
    if not brief.strip():
        return record
    drafts.update_clarifying_state(
        record.design_id,
        user_email,
        {
            "current_field_index": 0,
            "collected": {"site_brief": brief.strip()},
            "skipped": [],
            "not_required": [],
            "checkpoint_state": "pending",
        },
    )
    return drafts.get(record.design_id, user_email) or record


def _entry_response(record, brief_payload: dict) -> dict:
    """Shared response shape for both design_* entry tools.

    Landing-page additionally surfaces the server-driven clarifying state
    machine's first ``next_question`` payload + a short imperative under
    ``instructions_short``. The long prose stays available under
    ``instructions_legacy`` (and the legacy ``instructions`` key) for
    back-compat with older callers / tests. Survey-funnel keeps the
    original prose-only response.
    """
    response: dict[str, Any] = {
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

    # Both families now drive the server-owned intake state machine. Surface
    # the first next_question + the shared short directive; fall back to
    # prose-only if a family ever opts out (defensive — none do today).
    try:
        field_list = _field_list_for(record.family)
    except ValueError:
        field_list = None

    if field_list is not None:
        # Fresh draft = empty state. The state machine normalises the missing
        # keys; we hand it `{}` so we don't repeat the defaults here.
        first_question = intake_sm.next_question(field_list, {})
        next_q_payload = first_question.to_dict() if first_question else None
        response.update(
            instructions_short=_instructions_short_for(record.family),
            instructions_legacy=brief_payload["instructions"],
            next_question=next_q_payload,
        )
        if next_q_payload is not None:
            response["next_action"] = (
                f"After the user answers, call submit_clarifying_answer("
                f"design_id='{record.design_id}', "
                f"field_key='{next_q_payload['field_key']}', "
                f"answer=<the option string they picked>) to get the next "
                "question. Loop until next_question is null, then proceed to "
                "STEP 2 (outline) per instructions_legacy."
            )

    return response


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


async def _background_publish(
    design_id: str,
    user_email: str,
    slug: str,
    html: str,
    manifest_yaml: str,
    chat_summary: str,
    brief_md: str = "",
) -> None:
    """Run publish_design off the event loop, then mark the draft published or failed.

    MUST NEVER let an exception escape into the asyncio loop unhandled — every
    failure path catches, logs the traceback, and persists status=failed +
    last_error so the caller can recover via get_design_status.
    """
    try:
        cfg = DesignConfig.from_env()
        design_dir, sha = await asyncio.to_thread(
            publish_design,
            cfg=cfg,
            slug=slug,
            html=html,
            manifest_yaml=manifest_yaml,
            chat_summary=chat_summary,
            user_email=user_email,
            brief_md=brief_md,
        )
        drafts.mark_published(design_id, user_email, repo_sha=sha, design_dir=str(design_dir))
        log.info(
            "submit_design background publish OK: design_id=%s sha=%s dir=%s",
            design_id, sha, design_dir,
        )
    except Exception as exc:  # noqa: BLE001 — boundary, must swallow
        log.error(
            "submit_design background publish FAILED: design_id=%s err=%s\n%s",
            design_id, exc, traceback.format_exc(),
        )
        try:
            drafts.set_status(design_id, user_email, "failed")
            drafts.set_last_error(design_id, user_email, str(exc))
        except Exception:  # noqa: BLE001 — last-resort guard
            log.exception(
                "submit_design background publish: failed to record failure for design_id=%s",
                design_id,
            )


@mcp.tool()
async def submit_design(
    design_id: str,
    html: str,
    manifest: dict,
    publish: bool = True,
) -> dict:
    """Validate and (optionally) commit a generated design — git push runs async.

    The git commit author is derived from the authenticated user — the
    caller cannot spoof attribution.

    Async contract:
      - When publish=True the manifest + HTML are validated SYNCHRONOUSLY,
        persisted to the draft row, status flips to "submitting", and the
        git clone/pull/commit/push runs in a background task. This call
        returns immediately (typically <200ms) so the caller's chat session
        does not block on git.
      - When publish=False the manifest + HTML are validated, persisted,
        and the status flips straight to "submitted" with no background work.

    Args:
        design_id: the id returned by start_landing_page_intake / start_survey_funnel_intake
        html: the full HTML5 document produced by the caller's Claude
        manifest: the parsed manifest dict (matches manifest_schema for the family)
        publish: when True (default) write + commit + push to microsite-design-skills

    Returns:
        On accepted: {ok: True, design_id, slug, family, status: "submitting"|"submitted",
                      manifest_valid: True, html_size, manifest, message,
                      poll_after_seconds: int}
        On validation failure: {ok: False, design_id, errors: [...], status}
        On not-found: {ok: False, design_id, errors: [...], status: "not-found"}
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

    # Completeness gate — the server half of "don't generate until confirmed".
    # The brief must be fully reviewed: every required input answered and every
    # conditional input resolved (provided or explicitly confirmed Not-required)
    # before we accept a design. A missing integration / DNQ / tracking gate can
    # break the live system, so this is strict.
    incompleteness = _brief_incompleteness(record.family, record.clarifying_state)
    if incompleteness:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [
                "brief incomplete — cannot accept a design until the clarifying "
                "intake is confirmed. Outstanding: " + "; ".join(incompleteness)
            ],
            "status": record.status,
            "intake_complete": False,
            "hint": (
                "Finish the clarifying intake via submit_clarifying_answer until "
                "next_question is null — required fields must be answered and "
                "conditional ones provided or confirmed 'Not required' — then "
                "call submit_design again."
            ),
        }

    errors: list[str] = []

    # 1. Manifest validation via the family's Pydantic model — SYNC, fast.
    try:
        manifest_cls = _manifest_class_for(record.family)
    except ValueError as exc:
        return {"ok": False, "design_id": design_id, "errors": [str(exc)], "status": record.status}

    manifest_obj = None
    try:
        manifest_obj = manifest_cls(**manifest)
    except Exception as exc:  # Pydantic ValidationError + anything else
        errors.append(f"manifest validation failed: {exc}")

    # 2. HTML structural sanity — SYNC, fast.
    errors.extend(_html_sanity_check(html))

    if errors or manifest_obj is None:
        drafts.set_last_error(design_id, user_email, "; ".join(errors))
        return {
            "ok": False,
            "design_id": design_id,
            "errors": errors,
            "status": record.status,
            "manifest_valid": False,
            "hint": "Fix the issues above and call submit_design again with the corrected html + manifest.",
        }

    manifest_dict = manifest_obj.model_dump(mode="json")
    slug = manifest_dict.get("slug") or record.slug_hint or "untitled"
    chat_summary = _build_chat_summary(record.brief, slug, record.family, manifest_dict)
    brief_md = _build_brief_md(record, slug, manifest_dict)

    # 3. Persist the validated payload. record_submission flips status to
    #    "submitted"; for publish=True we then re-flip to "submitting" so the
    #    caller sees the right state until the background task lands it.
    drafts.record_submission(
        design_id,
        user_email,
        html=html,
        manifest=manifest_dict,
        chat_summary=chat_summary,
        slug=slug,
    )

    result: dict[str, Any] = {
        "ok": True,
        "design_id": design_id,
        "slug": slug,
        "family": record.family,
        "html_size": len(html),
        "manifest": manifest_dict,
        "manifest_valid": True,
    }

    if publish:
        drafts.set_status(design_id, user_email, "submitting")
        manifest_yaml = yaml.dump(manifest_dict, sort_keys=False, default_flow_style=False)
        # Fire-and-forget: background task records its own success / failure.
        asyncio.create_task(
            _background_publish(
                design_id=design_id,
                user_email=user_email,
                slug=slug,
                html=html,
                manifest_yaml=manifest_yaml,
                chat_summary=chat_summary,
                brief_md=brief_md,
            )
        )
        result.update(
            status="submitting",
            message=(
                "Submission accepted. The git push runs in the background — "
                f"call get_design_status('{design_id}') to check progress "
                "(typically 5-30 seconds)."
            ),
            poll_after_seconds=3,
        )
    else:
        result.update(
            status="submitted",
            message="Validated and persisted; publish=False so no git push was scheduled.",
            poll_after_seconds=0,
        )

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
    """Return the full lifecycle state of a design so the caller can render a checklist.

    Use this to poll after submit_design returns status="submitting", and to
    diagnose any tool failure (the server-side state, including last_error,
    is always authoritative).

    Returns:
        On found: {
            ok: True,
            design_id, status, family, slug, user_email,
            iteration_count, manifest_valid,
            commit_sha, design_dir, published_repo_sha,
            last_error,            # str | None — surface verbatim to the user
            created_at, updated_at, expires_at,  # ISO timestamps
            record,                # full DraftRecord dict (back-compat)
            summary,               # human-readable multi-line string
        }
        On not-found or wrong user: {ok: False, design_id, errors: [...]}
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

    data = record.to_dict()
    # manifest_valid: True if a manifest is persisted (it only lands after a
    # successful Pydantic validation in submit_design); False if status is
    # "failed" with a manifest-validation error; None otherwise (still drafting).
    if record.manifest is not None:
        manifest_valid: Optional[bool] = True
    elif record.status == "failed" and record.last_error and "manifest" in record.last_error:
        manifest_valid = False
    else:
        manifest_valid = None

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
    if record.design_dir:
        summary_lines.append(f"  dir    : {record.design_dir}")
    if record.last_error:
        summary_lines.append(f"  error  : {record.last_error}")

    return {
        "ok": True,
        "design_id": record.design_id,
        "status": record.status,
        "family": record.family,
        "slug": record.slug,
        "user_email": record.user_email,
        "iteration_count": len(record.iteration_log),
        "manifest_valid": manifest_valid,
        "commit_sha": record.commit_sha,
        "design_dir": record.design_dir,
        "published_repo_sha": record.published_repo_sha,
        "last_error": record.last_error,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
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
# submit_clarifying_answer + get_next_question — server-driven intake state
# machine (landing-page only for v1; survey-funnel still uses prose).
# ---------------------------------------------------------------------------

def _field_list_for(family: str):
    """Return the clarifying-field list for ``family`` or raise ValueError."""
    if family == "landing-page":
        return landing_gen.landing_page_field_list()
    if family == "survey-funnel":
        return survey_gen.survey_funnel_field_list()
    raise ValueError(
        f"family {family!r} does not use the server-driven intake state machine"
    )


def _instructions_short_for(family: str) -> str:
    """Return the short intake directive for ``family`` (shared across both)."""
    if family == "survey-funnel":
        return survey_gen.INSTRUCTIONS_SHORT
    return landing_gen.INSTRUCTIONS_SHORT


def _brief_incompleteness(family: str, clarifying_state: Optional[dict]) -> list[str]:
    """Return human-readable reasons the brief is incomplete, or [] if complete.

    The server hard-gate for "don't generate until confirmed": every REQUIRED
    field must be collected, and every CONDITIONAL field must be resolved
    (either collected or explicitly confirmed Not-required). Optional fields
    and checkpoints don't gate. Families that don't use the state machine
    return [] (no gate).

    This is an EXPLICIT per-field check (not just "next_question is None") so a
    malformed / hand-seeded state can't slip an unresolved required field past
    the gate.
    """
    try:
        field_list = _field_list_for(family)
    except ValueError:
        return []
    state = clarifying_state or {}
    collected = set((state.get("collected") or {}).keys())
    not_required = set(state.get("not_required") or [])
    missing: list[str] = []
    for cf in field_list:
        if cf.is_checkpoint:
            continue
        if cf.requirement == "required" and cf.key not in collected:
            missing.append(f"required '{cf.key}' not answered")
        elif (
            cf.requirement == "conditional"
            and cf.key not in collected
            and cf.key not in not_required
        ):
            missing.append(
                f"conditional '{cf.key}' unresolved (provide it or confirm 'Not required')"
            )
    return missing


def _next_question_payload(family: str, state: dict) -> Optional[dict]:
    """Compute the next NextQuestion for a draft and return its dict form."""
    field_list = _field_list_for(family)
    nq = intake_sm.next_question(field_list, state)
    return nq.to_dict() if nq else None


def _next_action_for(design_id: str, next_q: Optional[dict]) -> str:
    """Render the imperative the caller should follow next."""
    if next_q is None:
        return (
            "Intake complete. Proceed to STEP 2 (outline) per "
            "instructions_legacy from the original start_landing_page_intake "
            "response, then generate + submit_design."
        )
    return (
        f"After the user answers, call submit_clarifying_answer("
        f"design_id='{design_id}', field_key='{next_q['field_key']}', "
        "answer=<the user's reply>) to get the next question."
    )


@mcp.tool()
def submit_clarifying_answer(
    design_id: str,
    field_key: str,
    answer: str,
) -> dict:
    """Record the user's answer to the most recent clarifying question.

    Returns the next question to ask, or ``null`` when intake is complete.
    The server is the source of truth for which question is current — if
    ``field_key`` does not match the expected next field, the call is
    rejected so a stale chat session can't silently corrupt state.

    Args:
        design_id: The draft id returned by start_landing_page_intake.
        field_key: The field key from the most recent next_question
                   (e.g. "page_intent").
        answer:    The user's reply as a string. For multi-choice questions,
                   the exact option text. For checkpoints, one of:
                     - "continue" / "looks good" / "confirmed" -> advance
                     - "change <field_key> to <new value>" -> update that
                       field, re-show the checkpoint
                     - "go back to <field_key>" -> rewind to that field

    Returns:
        On success: {
            ok: True,
            design_id, field_key_recorded, next_question,
            intake_complete, collected_so_far, next_action,
        }
        On not-found / wrong user: {ok: False, design_id, errors: [...]}
        On wrong field_key (out of sync): {
            ok: False, design_id, errors: [...], expected_field_key,
            next_question, hint,
        }
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

    try:
        field_list = _field_list_for(record.family)
    except ValueError as exc:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [str(exc)],
        }

    state = dict(record.clarifying_state or {})
    # Surface a structured "wrong field" error rather than letting ValueError
    # escape — gives the caller a payload it can render + recover from.
    try:
        new_state = intake_sm.submit_answer(field_list, state, field_key, answer)
    except ValueError as exc:
        msg = str(exc)
        # Re-derive the expected field_key from the (unchanged) state so the
        # caller can resync without a separate round-trip.
        nq = intake_sm.next_question(field_list, state)
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [msg],
            "expected_field_key": nq.field_key if nq else None,
            "next_question": nq.to_dict() if nq else None,
            "hint": (
                "Re-ask the question in `next_question` then resubmit with "
                "field_key=expected_field_key."
            ),
        }

    drafts.update_clarifying_state(design_id, user_email, new_state)

    next_q = _next_question_payload(record.family, new_state)
    return {
        "ok": True,
        "design_id": design_id,
        "field_key_recorded": field_key,
        "next_question": next_q,
        "intake_complete": next_q is None,
        "collected_so_far": dict(new_state.get("collected") or {}),
        "next_action": _next_action_for(design_id, next_q),
    }


@mcp.tool()
def get_next_question(design_id: str) -> dict:
    """Return the current `next_question` without changing state.

    Useful when the caller's chat session has lost track of the conversation
    (e.g. after a refresh) or for debugging the intake flow. Read-only —
    no side effects.

    Returns:
        On found: {
            ok: True,
            design_id, family, intake_complete,
            next_question, collected_so_far, next_action,
        }
        On not-found / wrong user: {ok: False, design_id, errors: [...]}
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

    try:
        next_q = _next_question_payload(record.family, dict(record.clarifying_state or {}))
    except ValueError as exc:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": [str(exc)],
        }

    return {
        "ok": True,
        "design_id": design_id,
        "family": record.family,
        "intake_complete": next_q is None,
        "next_question": next_q,
        "collected_so_far": dict((record.clarifying_state or {}).get("collected") or {}),
        "next_action": _next_action_for(design_id, next_q),
    }


# ---------------------------------------------------------------------------
# get_preview_url — signed link to open the generated HTML in a browser
# ---------------------------------------------------------------------------

@mcp.tool()
def get_preview_url(design_id: str) -> dict:
    """Return a signed, time-limited URL that renders the draft HTML in a browser.

    The link works on any device (desktop or mobile) without an MCP client —
    handy when the user wants to actually see the page before approving
    submit_design. The URL contains an HMAC over (design_id, user_email,
    exp) so a tampered link fails closed.

    Args:
        design_id: an existing draft id owned by the current user

    Returns:
        On found: {ok: True, url, expires_at (ISO), ttl_seconds, note}
        On not-found: {ok: False, design_id, errors: [...]}
        On no-html-yet: {ok: False, design_id, errors: [...], hint}
    """
    from datetime import datetime, timedelta, timezone

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
    if not record.html:
        return {
            "ok": False,
            "design_id": design_id,
            "errors": ["draft has no html yet — submit_design hasn't been called"],
            "hint": (
                "Generate the HTML + manifest first, then call "
                "submit_design(publish=False) to persist it, then retry get_preview_url."
            ),
        }

    ttl = _PREVIEW_TTL_SECONDS
    url = signed_preview_url(design_id, user_email, ttl_seconds=ttl)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    return {
        "ok": True,
        "design_id": design_id,
        "url": url,
        "expires_at": expires_at.isoformat(),
        "ttl_seconds": ttl,
        "note": (
            "Open this in any browser — works on mobile. "
            "Link expires in 1 hour."
        ),
    }


# ---------------------------------------------------------------------------
# fetch_url_screenshots — multimodal context for "Enhancement" / "Replica"
# landing-page intents. Takes 3 screenshots (mobile / iPad / desktop) of an
# external URL via a multi-provider orchestrator and returns the image URLs
# so the caller's Claude can read them with its multimodal vision.
# ---------------------------------------------------------------------------

@mcp.tool()
async def fetch_url_screenshots(url: str) -> dict:
    """Take screenshots of an external URL at mobile, iPad, and desktop viewports.

    Use this when the user picks "Enhancement to an existing landing page" or
    "Replica of an existing landing page" and provides a URL. Returns 3 image
    URLs you can read via your multimodal vision so you have visual context
    for the design work.

    The first matching provider (Microlink → ApiFlash → ScreenshotMachine)
    wins per viewport. Results are cached for 24h per URL so repeat calls
    inside the cache window do no HTTP at all.

    Args:
        url: HTTP/HTTPS URL of the page to screenshot. Internal / private
             / loopback IPs and non-http(s) schemes are blocked (SSRF guard).

    Returns:
        {
            "url": <input url>,
            "mobile":  {"url": "...", "viewport": "390x844",  "provider": "..."},
            "ipad":    {"url": "...", "viewport": "820x1180", "provider": "..."},
            "desktop": {"url": "...", "viewport": "1440x900", "provider": "..."},
            "cached":  <bool — True if served from the 24h cache>,
        }
    """
    user_email = resolve_user_email()
    log.info("fetch_url_screenshots user=%s url=%s", user_email, url)

    try:
        results = await fetch_screenshots(url)
    except ValueError as exc:
        # URL validation failure (SSRF / bad scheme / non-resolving host)
        raise ValueError(f"URL invalid: {exc}") from exc
    except ScreenshotError as exc:
        raise RuntimeError(f"All screenshot providers failed: {exc}") from exc

    return {
        "url": url,
        "mobile":  {"url": results["mobile"].url,  "viewport": "390x844",  "provider": results["mobile"].provider},
        "ipad":    {"url": results["ipad"].url,    "viewport": "820x1180", "provider": results["ipad"].provider},
        "desktop": {"url": results["desktop"].url, "viewport": "1440x900", "provider": results["desktop"].provider},
        "cached":  results["mobile"].cached,
    }


# ---------------------------------------------------------------------------
# search_stock_images / fetch_icons / search_icons — server-controlled image
# sourcing. Stops Claude from fabricating Unsplash / Pexels photo URLs (the
# prod issue: hallucinated football stadiums for "lead generation") and from
# writing inline <svg> markup for icons.
# ---------------------------------------------------------------------------

@mcp.tool()
def search_stock_images(
    query: str,
    count: int = 6,
    orientation: str = "landscape",
    source: str = "pexels",
) -> dict:
    """Search free stock photos (Pexels + Unsplash) matching a keyword. Returns
    real candidates with URLs, photographer credit, alt text, and source link.

    NEVER fabricate Pexels or Unsplash URLs in HTML — both providers go through
    the real API. ALWAYS call this tool to get real, working image URLs for any
    image slot in the design. Pass source="both" to pull from BOTH providers
    (results are interleaved + deduped + capped at `count`).

    Args:
        query: search keywords (e.g. "lead generation marketing")
        count: 1-15, defaults to 6 (the total cap when source="both")
        orientation: "landscape" | "portrait" | "square"
        source: "pexels" (default) | "unsplash" | "both"

    Returns:
        {
          "query": <echoed>,
          "source": <echoed: "pexels" | "unsplash" | "both">,
          "results": [
            {
              "id": <provider_id>,
              "url_large": "<full-size embed URL>",      # use in <img src>
              "url_medium": "<~350px thumbnail URL>",     # use for inline preview
              "photographer": "Name",
              "photographer_url": "<author profile URL>",
              "alt": "<description>",
              "source": "<provider photo page URL>",
              "provider": "pexels" | "unsplash"
            },
            ...
          ],
          # str for a single provider; dict {"pexels": ..., "unsplash": ...}
          # when source="both". Unsplash requires "Photo by {name} on Unsplash"
          # with UTM-tagged links + an Unsplash credit; Pexels keeps its note.
          "attribution_note": <str | {"pexels": str, "unsplash": str}>
        }
    """
    # Auth context is required so we don't expose the upstream API key as
    # an open relay. resolve_user_email raises on unauthenticated callers.
    resolve_user_email()
    try:
        return images_mod.search_stock_images(
            query=query, count=count, orientation=orientation, source=source,
        )
    except images_mod.ImagesError as exc:
        log.error("search_stock_images failed: %s", exc)
        raise RuntimeError(str(exc)) from exc


@mcp.tool()
def fetch_icons(
    slots: dict,
    color: str = "#0F2A4A",
    size: int = 48,
) -> dict:
    """Fetch SVG icons for multiple slots in one call from Lucide via Iconify.
    Call this DURING initial HTML generation to embed real icons.

    NEVER write inline <svg> markup for icons. ALWAYS call this tool.

    Args:
        slots: {"hero_badge": "verified secure", "feature_1": "fast delivery", ...}
               keys are arbitrary slot names; values are search keywords
        color: hex color (with #, e.g. "#0F2A4A") — bakes into stroke/fill
        size: pixels (square)

    Returns:
        {
          "icons": {
            "hero_badge": {
              "icon_id": "lucide:shield-check",
              "svg": "<svg ...>...</svg>"  # ready to inline
            },
            ...
          }
        }
    """
    resolve_user_email()
    try:
        return images_mod.fetch_icons(slots=slots, color=color, size=size)
    except images_mod.ImagesError as exc:
        log.error("fetch_icons failed: %s", exc)
        raise RuntimeError(str(exc)) from exc


@mcp.tool()
def search_icons(query: str, count: int = 8) -> dict:
    """Search Iconify for icon alternatives matching a keyword. Use DURING
    ITERATION when user wants to swap a specific icon (e.g. "change the
    verified icon, show alternatives"). Returns candidates Claude can render
    as AskUserQuestion options.

    Args:
        query: search keywords
        count: 1-20

    Returns:
        {
          "results": [
            {
              "icon_id": "lucide:shield-check",
              "preview_url": "https://api.iconify.design/lucide:shield-check.svg?width=24",
              "svg": "<svg ...>...</svg>"  # full SVG for embedding
            },
            ...
          ]
        }
    """
    resolve_user_email()
    try:
        return images_mod.search_icons(query=query, count=count)
    except images_mod.ImagesError as exc:
        log.error("search_icons failed: %s", exc)
        raise RuntimeError(str(exc)) from exc


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


def _build_brief_md(record, slug: str, manifest_dict: dict) -> str:
    """Build the `brief.md` deliverable (the MDF): YAML front-matter (a
    machine-parseable map of every confirmed clarifying input) plus a
    human-readable body.

    Sourced from the CONFIRMED clarifying intake (`record.clarifying_state`),
    so it captures the inputs that don't live in the render manifest —
    integrations, tracking, DNQ rules, question dependencies, reference layout,
    page path — each with an explicit `not_required` marker when the user
    confirmed it doesn't apply. This is both an operator handoff doc and a
    build-input definition for downstream tooling (Skill B / the backend).
    """
    family = record.family
    state = record.clarifying_state or {}
    collected = dict(state.get("collected") or {})
    not_required = list(state.get("not_required") or [])
    skipped = list(state.get("skipped") or [])

    try:
        field_list = _field_list_for(family)
    except ValueError:
        field_list = []

    inputs: dict[str, Any] = {}
    body_lines: list[str] = []
    for cf in field_list:
        if cf.is_checkpoint:
            continue
        if cf.key in collected:
            val = collected[cf.key]
            inputs[cf.key] = val
            body_lines.append(f"- **{cf.key}** ({cf.requirement}): {val}")
        elif cf.key in not_required:
            inputs[cf.key] = {"not_required": True}
            body_lines.append(f"- **{cf.key}** ({cf.requirement}): _Not required_")
        elif cf.key in skipped:
            inputs[cf.key] = {"skipped": True}
            body_lines.append(f"- **{cf.key}** ({cf.requirement}): _(skipped)_")
        # Unanswered fields are omitted; the completeness gate guarantees every
        # required + conditional field is resolved before we ever get here.

    front = {
        "slug": slug,
        "family": family,
        "manifest": "page-meta.yaml",
        "page": f"{slug}.html",
        "inputs": inputs,
        "not_required": not_required,
        "skipped": skipped,
    }
    fm = yaml.dump(
        front, sort_keys=False, default_flow_style=False, allow_unicode=True
    ).rstrip()
    body = "\n".join(body_lines) if body_lines else "_(no clarifying inputs recorded)_"
    return (
        f"---\n{fm}\n---\n\n"
        f"# Confirmed brief — {slug}\n\n"
        f"Family: **{family}**. This file is the confirmed intake (the MDF) — an "
        f"operator handoff doc and a machine-parseable build input. The render "
        f"manifest is in `page-meta.yaml`; the page is `{slug}.html`.\n\n"
        f"## Confirmed inputs\n{body}\n"
    )


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
