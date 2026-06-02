"""design-mcp-server — hosted MCP for generating new microsite HTML from chat."""

from pathlib import Path as _Path

_BASE_VERSION = "0.0.1"


def _read_deployed_sha() -> str:
    """Return the 7-char git SHA of the deployed code, or 'local' if unknown.

    The GitHub Actions deploy step writes the full SHA to a `.deployed-sha`
    file on the EC2 host. design_ping surfaces this so callers (and Sheetal
    in claude.ai) can confirm a deploy actually landed.
    """
    candidates = [
        _Path("/home/ubuntu/design-mcp-server/.deployed-sha"),  # EC2 production
        _Path(__file__).parent.parent.parent / ".deployed-sha",  # repo root in dev
    ]
    for path in candidates:
        try:
            sha = path.read_text().strip()
            if sha:
                return sha[:7]
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return "local"


__version__ = f"{_BASE_VERSION}+{_read_deployed_sha()}"
