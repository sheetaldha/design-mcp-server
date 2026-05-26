"""Config — loads env vars.

Return-prompts pattern: this server never calls an LLM itself, so there is
no ANTHROPIC_API_KEY. The calling Claude does all generation; the server
only validates + commits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()


def redact_url(url: str) -> str:
    """Strip userinfo (user:password) from an HTTP/S URL.

    Returns the URL unchanged if it has no userinfo or isn't parseable as HTTP.
    SSH-style URLs (git@host:path) are returned as-is.
    """
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or "@" not in (parts.netloc or ""):
            return url
        host = parts.netloc.split("@", 1)[1]
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    except Exception:
        return url


@dataclass(frozen=True)
class DesignConfig:
    # HTTP server
    host: str
    port: int
    public_url: str

    # Token DB
    token_db_host: str
    token_db_port: int
    token_db_name: str
    token_db_user: str
    token_db_password: str

    # Design repo
    design_repo_ssh: str
    design_repo_branch: str
    design_repo_local_clone: str

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "DesignConfig":
        required = [
            "TOKEN_DB_PASSWORD",
        ]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise RuntimeError(
                f"Missing required env vars: {', '.join(missing)}. "
                f"Copy .env.example to .env and fill in values."
            )
        return cls(
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8050")),
            public_url=os.getenv("PUBLIC_URL", "https://design-mcp.leadloom.com.au"),
            token_db_host=os.getenv("TOKEN_DB_HOST", "170.64.179.248"),
            token_db_port=int(os.getenv("TOKEN_DB_PORT", "5432")),
            token_db_name=os.getenv("TOKEN_DB_NAME", "acquirely_rel"),
            token_db_user=os.getenv("TOKEN_DB_USER", "postgres"),
            token_db_password=os.environ["TOKEN_DB_PASSWORD"],
            design_repo_ssh=os.getenv(
                "DESIGN_REPO_SSH",
                "git@bitbucket.org:acquirelydev/microsite-design-skills.git",
            ),
            design_repo_branch=os.getenv("DESIGN_REPO_BRANCH", "main"),
            design_repo_local_clone=os.getenv(
                "DESIGN_REPO_LOCAL_CLONE",
                "/Users/sgb_m2/microsite-design-skills",
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
