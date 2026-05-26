"""Bearer-token verifier — bridges the MCP SDK's TokenVerifier protocol to the
opaque tokens stored in `design_mcp_tokens` on DO PG 17.

Returns an `AccessToken` populated with the user's email (as `client_id`) and
a fixed scope list on success; `None` for unknown, revoked, or malformed tokens.
DB validation runs in a worker thread so the async event loop is not blocked.
"""

from __future__ import annotations

import asyncio
import logging

from mcp.server.auth.provider import AccessToken, TokenVerifier

from . import auth as auth_mod
from .auth import AuthError, TokenInfo

log = logging.getLogger(__name__)

# Stable scope advertised in protected-resource metadata and required on
# every authenticated request.
DESIGN_WRITE_SCOPE = "design:write"


class BearerTokenVerifier(TokenVerifier):
    """Validates opaque bearer tokens against the design_mcp_tokens table."""

    def __init__(self, scopes: list[str] | None = None) -> None:
        self._scopes = scopes or [DESIGN_WRITE_SCOPE]

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            info: TokenInfo = await asyncio.to_thread(auth_mod.validate_token, token)
        except AuthError as exc:
            log.warning("auth fail: %s", exc)
            return None
        except Exception:
            log.exception("auth fail: unexpected error validating token")
            return None

        log.info("auth ok user=%s token_id=%s", info.user_email, info.id)
        return AccessToken(
            token=token,
            client_id=f"design-mcp-token:{info.id}",
            scopes=list(self._scopes),
            expires_at=None,
        )
