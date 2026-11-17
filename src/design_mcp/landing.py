"""Landing-page editing for the Cloudflare sites (cf-landing-sites repo).

Lets named editors (Jeremy) change copy, layout and styles on the Cloudflare
landing pages from claude.ai, and nothing else:

  - Only the files in EDITABLE can be written. Scripts, Cloudflare functions,
    routing, the release checks and the pipeline are out of reach.
  - HTML edits must leave every script tag, form control id and the consent
    wording exactly as they were, and may not add handlers, iframes or forms.
  - Every save is a commit on the editor's own feature branch. The Bitbucket
    pipeline deploys it to a test-mode preview and runs checks/e2e.js.
  - Publishing opens a pull request to main, only when the checks passed on the
    latest commit. Sheetal approves and merges; the pipeline deploys production.

The server never holds a Cloudflare token: deploys happen in the pipeline.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

import requests

from .config import redact_url

log = logging.getLogger(__name__)

TEXT_EDITABLE = (
    "public/index.html",
    "public/thank-you/index.html",
    "public/dnq/index.html",
    "public/terms/index.html",
    "public/privacy/index.html",
    "public/assets/footer.html",
    "public/assets/app.css",
    "public/assets/b.css",
    "public/tracking.json",
)
IMAGE_DIR = "public/assets/img/"
IMAGE_TYPES = {".png": b"\x89PNG", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".webp": b"RIFF", ".svg": b"<"}
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 400 * 1024


class LandingError(Exception):
    """A request the landing tools refuse, with a reason the editor can act on."""


@dataclass(frozen=True)
class LandingConfig:
    repo_url: str            # https://x-token-auth:<repo access token>@bitbucket.org/acquirelydev/cf-landing-sites.git
    local_clone: str
    api_token: str           # same repository access token, used for the REST API
    workspace: str
    repo_slug: str
    editors: tuple[str, ...]
    owner_email: str
    autopublish: tuple[str, ...] = ()   # editors whose checked changes go live without Sheetal's approval
    slack_webhook: str = ""

    @classmethod
    def from_env(cls) -> "LandingConfig":
        token = os.getenv("LANDING_BB_TOKEN", "")
        if not token:
            raise LandingError("Landing editing is not configured on this server yet (LANDING_BB_TOKEN missing).")
        ws = os.getenv("LANDING_BB_WORKSPACE", "acquirelydev")
        slug = os.getenv("LANDING_BB_REPO", "cf-landing-sites")
        return cls(
            repo_url=f"https://x-token-auth:{token}@bitbucket.org/{ws}/{slug}.git",
            local_clone=os.getenv("LANDING_LOCAL_CLONE", "/home/ubuntu/cf-landing-sites"),
            api_token=token,
            workspace=ws,
            repo_slug=slug,
            editors=tuple(e.strip().lower() for e in os.getenv("LANDING_EDITORS", "").split(",") if e.strip()),
            owner_email=os.getenv("LANDING_OWNER_EMAIL", "sheetal@acquirely.com.au"),
            autopublish=tuple(e.strip().lower() for e in os.getenv("LANDING_AUTOPUBLISH", "").split(",") if e.strip()),
            slack_webhook=os.getenv("LANDING_SLACK_WEBHOOK", ""),
        )

    def can_autopublish(self, user_email: str) -> bool:
        return (user_email or "").lower() in self.autopublish


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def require_editor(cfg: LandingConfig, user_email: str) -> None:
    if (user_email or "").lower() not in cfg.editors:
        raise LandingError("Your account is not set up to edit the landing pages. Ask Sheetal to add you.")


def branch_for(user_email: str, site: str) -> str:
    who = re.sub(r"[^a-z0-9]+", "-", user_email.split("@")[0].lower()).strip("-")
    return f"feature-edit-{who}-{site}"


def preview_url(site_cfg: dict, branch: str) -> str:
    alias = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")[:28].rstrip("-")
    return f"https://{alias}.{site_cfg['pagesProject']}.pages.dev/comparenow"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.ids: set[str] = set()
        self.tags: dict[str, int] = {}
        self.handlers: list[str] = []
        self.js_urls: list[str] = []
        self._in_script = False
        self._buf: list[str] = []
        self._consent_depth = 0
        self.consent: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags[tag] = self.tags.get(tag, 0) + 1
        if a.get("id"):
            self.ids.add(a["id"])
        for k, v in attrs:
            if k.lower().startswith("on"):
                self.handlers.append(f"{tag} {k}")
            if k.lower() in ("href", "src", "action", "formaction") and v and v.strip().lower().startswith("javascript:"):
                self.js_urls.append(f"{tag} {k}")
        if tag == "script":
            self._in_script = True
            self._buf = [json.dumps(sorted(attrs))]
        if self._consent_depth:
            self._consent_depth += 1
        elif "consent" in (a.get("class") or "").split():
            self._consent_depth = 1

    def handle_endtag(self, tag):
        if tag == "script" and self._in_script:
            self.scripts.append("".join(self._buf).strip())
            self._in_script = False
        if self._consent_depth:
            self._consent_depth -= 1

    def handle_data(self, data):
        if self._in_script:
            self._buf.append(data)
        if self._consent_depth:
            self.consent.append(data)


def _scan(html: str) -> _Scan:
    s = _Scan()
    s.feed(html)
    s.close()
    return s


def check_html_change(old: str, new: str) -> list[str]:
    """Problems that stop an HTML edit. Empty list = allowed."""
    o, n = _scan(old), _scan(new)
    problems = []
    if o.scripts != n.scripts:
        problems.append("Scripts can't be added, removed or changed. Ask Sheetal for anything that changes how the page works.")
    missing = sorted(o.ids - n.ids)
    if missing:
        problems.append(f"These page elements must stay (the form relies on them): {', '.join('#' + m for m in missing)}.")
    new_handlers = [h for h in n.handlers if h not in o.handlers]
    if new_handlers:
        problems.append(f"Inline event handlers aren't allowed ({', '.join(new_handlers)}).")
    if n.js_urls:
        problems.append("javascript: links aren't allowed.")
    for tag in ("iframe", "object", "embed", "form", "input", "select", "textarea", "button", "base", "meta"):
        if n.tags.get(tag, 0) > o.tags.get(tag, 0):
            problems.append(f"Adding <{tag}> elements isn't allowed; form and page plumbing is fixed.")
    # Consent and compliance wording are editable by design (Sheetal, 16 Nov 2026).
    return problems


def check_css_change(css: str) -> list[str]:
    problems = []
    for m in re.finditer(r"@import\s+(?:url\()?\s*['\"]?([^'\")\s;]+)", css, re.I):
        if not m.group(1).startswith("https://fonts.googleapis.com/"):
            problems.append(f"@import is only allowed for Google Fonts ({m.group(1)}).")
    if re.search(r"url\(\s*['\"]?\s*javascript:", css, re.I) or re.search(r"expression\s*\(", css, re.I):
        problems.append("Script inside CSS isn't allowed.")
    return problems


TRACKING_KEYS = {"_help", "gtmId", "metaPixelId", "googleTagIds"}


def check_tracking(content: str) -> list[str]:
    """tracking.json may only hold tracking IDs in their known formats."""
    try:
        t = json.loads(content)
    except ValueError as e:
        return [f"tracking.json isn't valid JSON ({e})."]
    if not isinstance(t, dict):
        return ["tracking.json must be an object like {\"gtmId\": \"GTM-XXXXXXX\"}."]
    problems = []
    extra = sorted(set(t) - TRACKING_KEYS)
    if extra:
        problems.append(f"Unknown keys in tracking.json: {', '.join(extra)}. Allowed: gtmId, metaPixelId, googleTagIds.")
    if t.get("gtmId") and not re.fullmatch(r"GTM-[A-Z0-9]{4,12}", str(t["gtmId"])):
        problems.append("gtmId must look like GTM-XXXXXXX.")
    if t.get("metaPixelId") and not re.fullmatch(r"[0-9]{6,20}", str(t["metaPixelId"])):
        problems.append("metaPixelId must be the pixel's number (digits only).")
    ids = t.get("googleTagIds", [])
    if not isinstance(ids, list) or any(not re.fullmatch(r"(G|AW|GT|DC)-[A-Z0-9]{4,16}", str(i)) for i in ids) or len(ids) > 5:
        problems.append("googleTagIds must be a list of up to 5 IDs like G-XXXXXXX or AW-XXXXXXXXX.")
    if not t.get("gtmId") and not t.get("metaPixelId") and not ids:
        problems.append("That would remove all tracking. Keep at least the GTM container.")
    return problems


def check_image(name: str, data: bytes) -> list[str]:
    ext = os.path.splitext(name)[1].lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,80}", name):
        return ["Image names may use lower-case letters, digits, dots, dashes and underscores only."]
    if ext not in IMAGE_TYPES:
        return [f"Only {', '.join(sorted(IMAGE_TYPES))} images are allowed."]
    if len(data) > MAX_IMAGE_BYTES:
        return ["Images must be 2 MB or smaller."]
    if not data.startswith(IMAGE_TYPES[ext]):
        return [f"That file doesn't look like a {ext} image."]
    if ext == ".svg" and re.search(rb"<script|on[a-z]+\s*=|javascript:", data, re.I):
        return ["SVG images can't contain scripts or event handlers."]
    return []


def validate_path(path: str) -> str:
    p = path.strip().lstrip("/")
    if p in TEXT_EDITABLE:
        return p
    raise LandingError(
        f"'{path}' isn't editable. You can edit: {', '.join(TEXT_EDITABLE)}, and add images under {IMAGE_DIR}."
    )


# ---------------------------------------------------------------------------
# Git working copy (one clone, one branch per editor per site)
# ---------------------------------------------------------------------------

Runner = Callable[[list[str], Optional[Path]], str]


def _git(cmd: list[str], cwd: Optional[Path] = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        safe = " ".join(redact_url(c) for c in cmd)
        raise LandingError(f"git failed: {safe}: {redact_url(r.stderr.strip())[:300]}")
    return r.stdout.strip()


class Workspace:
    def __init__(self, cfg: LandingConfig, run: Runner = _git) -> None:
        self.cfg, self.run, self.root = cfg, run, Path(cfg.local_clone)

    def sync(self) -> None:
        if not (self.root / ".git").exists():
            self.root.parent.mkdir(parents=True, exist_ok=True)
            self.run(["git", "clone", self.cfg.repo_url, str(self.root)], None)
        self.run(["git", "remote", "set-url", "origin", self.cfg.repo_url], self.root)
        self.run(["git", "fetch", "--prune", "origin"], self.root)

    def sites(self) -> dict:
        self.sync()
        raw = self.run(["git", "show", "origin/main:checks/sites.json"], self.root)
        return json.loads(raw)

    def checkout(self, branch: str) -> None:
        """Editor branch if it exists on the remote, otherwise a fresh one from main."""
        remote = self.run(["git", "ls-remote", "--heads", "origin", branch], self.root)
        start = f"origin/{branch}" if remote.strip() else "origin/main"
        self.run(["git", "checkout", "-q", "-B", branch, start], self.root)
        self.run(["git", "reset", "-q", "--hard", start], self.root)
        self.run(["git", "clean", "-qfd"], self.root)

    def read(self, rel: str) -> str:
        f = self.root / rel
        if not f.exists():
            raise LandingError(f"{rel} doesn't exist on this site.")
        return f.read_text(encoding="utf-8")

    def commit_and_push(self, branch: str, files: dict[str, bytes], message: str, user_email: str) -> str:
        for rel, data in files.items():
            f = self.root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
            self.run(["git", "add", rel], self.root)
        if not self.run(["git", "status", "--porcelain"], self.root).strip():
            raise LandingError("Nothing changed: the new content is identical to what's there.")
        name = user_email.split("@")[0].replace(".", " ").title()
        self.run(["git", "-c", f"user.name={name}", "-c", f"user.email={user_email}", "commit", "-q", "-m", message], self.root)
        self.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], self.root)
        return self.run(["git", "rev-parse", "HEAD"], self.root)

    def is_up_to_date(self, branch: str) -> bool:
        """True when the editor's branch already contains everything on main."""
        behind = self.run(["git", "rev-list", "--count", f"origin/{branch}..origin/main"], self.root)
        return behind.strip() in ("", "0")

    def update_from_main(self, branch: str, user_email: str) -> str:
        """Merge the latest main into the editor's branch and push it (the checks then rerun)."""
        self.checkout(branch)
        name = user_email.split("@")[0].replace(".", " ").title()
        try:
            self.run(["git", "-c", f"user.name={name}", "-c", f"user.email={user_email}",
                      "merge", "-q", "--no-edit", "origin/main"], self.root)
        except Exception as e:  # noqa: BLE001
            self.run(["git", "merge", "--abort"], self.root)
            raise LandingError("Someone else changed the same part of this page since you started, so your draft "
                               "can't be updated automatically. Ask Sheetal to combine the two.") from e
        self.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], self.root)
        return self.run(["git", "rev-parse", "HEAD"], self.root)

    def changed_files(self, branch: str) -> list[str]:
        out = self.run(["git", "diff", "--name-only", f"origin/main...origin/{branch}"], self.root)
        return [l for l in out.splitlines() if l]


# ---------------------------------------------------------------------------
# Bitbucket API (pipelines + pull requests)
# ---------------------------------------------------------------------------

class Bitbucket:
    def __init__(self, cfg: LandingConfig, session: Optional[requests.Session] = None) -> None:
        self.cfg = cfg
        self.s = session or requests.Session()
        self.base = f"https://api.bitbucket.org/2.0/repositories/{cfg.workspace}/{cfg.repo_slug}"
        self.h = {"Authorization": f"Bearer {cfg.api_token}", "Accept": "application/json"}

    def latest_pipeline(self, branch: str) -> Optional[dict]:
        r = self.s.get(f"{self.base}/pipelines/", headers=self.h, timeout=20,
                       params={"target.branch": branch, "sort": "-created_on", "pagelen": 1})
        r.raise_for_status()
        vals = r.json().get("values", [])
        return vals[0] if vals else None

    def pipeline_log_tail(self, pipeline_uuid: str, lines: int = 40) -> str:
        r = self.s.get(f"{self.base}/pipelines/{pipeline_uuid}/steps/", headers=self.h, timeout=20)
        r.raise_for_status()
        steps = r.json().get("values", [])
        if not steps:
            return ""
        # The log is plain text; asking for JSON gets HTTP 406.
        lr = self.s.get(f"{self.base}/pipelines/{pipeline_uuid}/steps/{steps[0]['uuid']}/log",
                        headers={**self.h, "Accept": "text/plain, */*"}, timeout=30)
        if lr.status_code != 200:
            return ""
        keep = [l for l in lr.text.splitlines() if l.startswith(("PASS", "FAIL", "=== checks", "ERROR"))]
        return "\n".join(keep[-lines:])

    def open_pr(self, branch: str, title: str, description: str) -> dict:
        """Open (or reuse) the pull request for this branch. Returns {"id", "url"}."""
        existing = self.s.get(f"{self.base}/pullrequests", headers=self.h, timeout=20,
                              params={"q": f'source.branch.name="{branch}" AND state="OPEN"'})
        existing.raise_for_status()
        vals = existing.json().get("values", [])
        if vals:
            return {"id": vals[0]["id"], "url": vals[0]["links"]["html"]["href"]}
        r = self.s.post(f"{self.base}/pullrequests", headers=self.h, timeout=20, json={
            "title": title, "description": description,
            "source": {"branch": {"name": branch}}, "destination": {"branch": {"name": "main"}},
            "close_source_branch": True,
        })
        r.raise_for_status()
        j = r.json()
        return {"id": j["id"], "url": j["links"]["html"]["href"]}

    def merge_pr(self, pr_id: int, message: str) -> tuple[bool, str]:
        """Squash-merge a pull request into main. Returns (merged, reason if not)."""
        r = self.s.post(f"{self.base}/pullrequests/{pr_id}/merge", headers=self.h, timeout=60, json={
            "type": "pullrequest_merge_parameters", "message": message,
            "merge_strategy": "squash", "close_source_branch": True,
        })
        if r.status_code in (200, 202):
            return True, ""
        try:
            reason = (r.json().get("error") or {}).get("message", "")
        except ValueError:
            reason = r.text[:200]
        return False, f"HTTP {r.status_code}: {reason}"


def notify_slack(cfg: "LandingConfig", text: str, session: Optional[requests.Session] = None) -> None:
    """Best effort: a failed notification never blocks a publish."""
    if not cfg.slack_webhook:
        return
    try:
        (session or requests).post(cfg.slack_webhook, json={"text": text}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def pipeline_summary(p: Optional[dict], head_sha: Optional[str]) -> dict:
    if not p:
        return {"state": "not started", "for_latest_change": False}
    state = p.get("state", {})
    result = (state.get("result") or {}).get("name")
    sha = ((p.get("target") or {}).get("commit") or {}).get("hash", "")
    return {
        "state": state.get("name"),                  # PENDING / IN_PROGRESS / COMPLETED
        "result": result,                            # SUCCESSFUL / FAILED / STOPPED / None
        "passed": result == "SUCCESSFUL",
        "for_latest_change": bool(head_sha) and sha.startswith(head_sha[:12]),
        "build_number": p.get("build_number"),
        "uuid": p.get("uuid"),
    }
