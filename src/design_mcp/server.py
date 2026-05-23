"""FastMCP server — Day 1 scaffold.

Currently exposes one placeholder tool (`design_ping`) so we can verify
the install + transport works end-to-end before adding the real generation tools.

Day 5 will switch from stdio to HTTP/SSE for claude.ai web access.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

import yaml

from .config import DesignConfig
from .generators import landing_page as landing_gen
from .repo import publish_design

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


@mcp.tool()
def design_landing_page(
    brief: str,
    user_email: str = "sheetal@acquirely.com.au",
    references: list[str] | None = None,
    slug: str | None = None,
    publish: bool = False,
) -> dict:
    """Generate a new Landing Page microsite from a natural-language brief.

    Args:
        brief: what the site sells, target audience, tone, color preferences
        user_email: attribution for the commit (defaults to Sheetal's email)
        references: optional list of URLs / image refs / inspiration notes
        slug: optional override; default is auto-slugified from the brief
        publish: when True, commit + push to microsite-design-skills repo.
                 When False (default), returns the HTML + manifest in-memory
                 so the caller can preview before committing.

    Returns:
        {"slug", "html_size", "manifest", "chat_summary", "committed": bool, "design_dir"?, "commit_sha"?}
    """
    cfg = DesignConfig.from_env()
    html, manifest, chat_summary = landing_gen.generate(cfg, brief, references=references, requested_slug=slug)

    result = {
        "slug": manifest.slug,
        "html_size": len(html),
        "manifest": manifest.model_dump(mode="json"),
        "chat_summary": chat_summary,
        "committed": False,
    }

    if publish:
        manifest_yaml = yaml.dump(manifest.model_dump(mode="json"), sort_keys=False, default_flow_style=False)
        design_dir, sha = publish_design(
            cfg=cfg,
            slug=manifest.slug,
            html=html,
            manifest_yaml=manifest_yaml,
            chat_summary=chat_summary,
            user_email=user_email,
        )
        result["committed"] = True
        result["design_dir"] = str(design_dir)
        result["commit_sha"] = sha

    return result


def main() -> None:
    """Entry point — stdio MCP for local dev (Day 1).

    Day 5 will switch this to HTTP/SSE transport for production.
    """
    log.info("design-mcp-server starting (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
