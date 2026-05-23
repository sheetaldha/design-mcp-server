"""FastMCP server — Day 1 scaffold.

Currently exposes one placeholder tool (`design_ping`) so we can verify
the install + transport works end-to-end before adding the real generation tools.

Day 5 will switch from stdio to HTTP/SSE for claude.ai web access.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from .config import DesignConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

mcp = FastMCP("design-mcp-server")


@mcp.tool()
def design_ping() -> dict:
    """Health check — confirms the MCP server is up and config loads.

    Returns:
        {"mcp": "ok", "version": "...", "public_url": "..."}
    """
    from . import __version__

    try:
        cfg = DesignConfig.from_env()
        return {
            "mcp": "ok",
            "version": __version__,
            "public_url": cfg.public_url,
            "has_anthropic_key": bool(cfg.anthropic_api_key),
            "design_repo": cfg.design_repo_ssh,
        }
    except RuntimeError as e:
        return {"mcp": "ok", "config_error": str(e)}


def main() -> None:
    """Entry point — stdio MCP for local dev (Day 1).

    Day 5 will switch this to HTTP/SSE transport for production.
    """
    log.info("design-mcp-server starting (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
