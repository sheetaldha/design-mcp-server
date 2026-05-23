"""Config — loads env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class DesignConfig:
    # Anthropic
    anthropic_api_key: str

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
            "ANTHROPIC_API_KEY",
            "TOKEN_DB_PASSWORD",
        ]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise RuntimeError(
                f"Missing required env vars: {', '.join(missing)}. "
                f"Copy .env.example to .env and fill in values."
            )
        return cls(
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8050")),
            public_url=os.getenv("PUBLIC_URL", "https://design-mcp.acquirely.com.au"),
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
