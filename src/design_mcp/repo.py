"""Git helper for the microsite-design-skills Bitbucket repo.

Skill A's outputs land here as `designs/<YYYY-MM-DD>-<slug>/{html, manifest, chat, assets}`.
Sheetal pulls this repo to review; Slack notification fires on push (Bitbucket integration).
"""

from __future__ import annotations

import logging
import subprocess
from datetime import date
from pathlib import Path
from typing import Optional

from .config import DesignConfig

log = logging.getLogger(__name__)


class GitError(Exception):
    """git command failed."""


def _run(cmd: list[str], cwd: Optional[Path] = None) -> str:
    """Run a git command, raise GitError on failure."""
    log.debug("git: %s (cwd=%s)", " ".join(cmd), cwd)
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(
            f"{' '.join(cmd)} failed (exit {result.returncode}):\n"
            f"  stdout: {result.stdout.strip()}\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def ensure_repo(cfg: DesignConfig) -> Path:
    """Clone the design repo if missing, otherwise pull latest. Returns local path."""
    local = Path(cfg.design_repo_local_clone)
    if not (local / ".git").exists():
        log.info("cloning %s -> %s", cfg.design_repo_ssh, local)
        local.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", cfg.design_repo_ssh, str(local)])
    else:
        log.info("pulling latest into %s", local)
        _run(["git", "fetch", "origin"], cwd=local)
        _run(["git", "checkout", cfg.design_repo_branch], cwd=local)
        _run(["git", "pull", "origin", cfg.design_repo_branch], cwd=local)
    return local


def publish_design(
    cfg: DesignConfig,
    slug: str,
    html: str,
    manifest_yaml: str,
    chat_summary: str,
    user_email: str,
    assets: dict[str, bytes] | None = None,
) -> tuple[Path, str]:
    """Write design files into a new dir, commit, push. Returns (design_dir, commit_sha)."""
    repo = ensure_repo(cfg)

    design_dir = repo / "designs" / f"{date.today().isoformat()}-{slug}"
    design_dir.mkdir(parents=True, exist_ok=True)
    (design_dir / f"{slug}.html").write_text(html, encoding="utf-8")
    (design_dir / "page-meta.yaml").write_text(manifest_yaml, encoding="utf-8")
    (design_dir / "chat.md").write_text(chat_summary, encoding="utf-8")
    (design_dir / "status.md").write_text("drafted\n", encoding="utf-8")

    if assets:
        assets_dir = design_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
        for name, content in assets.items():
            (assets_dir / name).write_bytes(content)

    _run(["git", "add", str(design_dir.relative_to(repo))], cwd=repo)
    # Use the user's email so Bitbucket attribution shows who created the design
    msg = f"design: {slug}\n\nGenerated for {user_email} via design-mcp-server."
    _run(
        [
            "git",
            "-c", f"user.email={user_email}",
            "-c", "user.name=design-mcp-server",
            "commit",
            "-m", msg,
        ],
        cwd=repo,
    )
    _run(["git", "push", "origin", cfg.design_repo_branch], cwd=repo)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    return design_dir, sha
