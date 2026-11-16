"""Landing-page editing: guards, git flow and the publish gate."""

import json
import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("TOKEN_DB_PASSWORD", "test")

from design_mcp import landing as L  # noqa: E402

CF_SITES = Path("/Users/sgb_m2/acquirely/cf-sites")
INDEX = (CF_SITES / "mlic-v2/public/index.html").read_text()


# --------------------------------------------------------------------------- guards

def test_copy_change_is_allowed():
    new = INDEX.replace("in 2 minutes", "in 60 seconds")
    assert new != INDEX
    assert L.check_html_change(INDEX, new) == []


@pytest.mark.parametrize("mutate, expect", [
    (lambda h: h.replace('<script src="/assets/app.js" defer></script>', ""), "Scripts"),
    (lambda h: h.replace("</body>", "<script>alert(1)</script></body>"), "Scripts"),
    (lambda h: h.replace('id="submit"', 'id="go"'), "#submit"),
    (lambda h: h.replace('<input id="email"', '<input id="email" onfocus="x()"'), "handlers"),
    (lambda h: h.replace("</main>", "<iframe src='https://x'></iframe></main>"), "iframe"),
    (lambda h: h.replace("</main>", "<form action='https://x'></form></main>"), "form"),
    (lambda h: h.replace("</main>", "<input name='x'></main>"), "input"),
    (lambda h: h.replace("</main>", '<a href="javascript:steal()">x</a></main>'), "javascript"),
])
def test_unsafe_html_is_refused(mutate, expect):
    new = mutate(INDEX)
    assert new != INDEX
    problems = L.check_html_change(INDEX, new)
    assert any(expect in p for p in problems), problems


def test_css_rules():
    assert L.check_css_change(".pill{color:red}") == []
    assert L.check_css_change("@import url('https://fonts.googleapis.com/css2?family=X');") == []
    assert L.check_css_change("@import url('https://evil.example/x.css');")
    assert L.check_css_change("a{background:url('javascript:alert(1)')}")
    assert L.check_css_change("a{width:expression(alert(1))}")


@pytest.mark.parametrize("path", [
    "public/assets/app.js", "public/assets/site.js", "functions/api/ip.js", "public/_redirects",
    "public/_headers", "../checks/e2e.js", "wrangler.toml", "public/index.html/../assets/app.js",
])
def test_protected_paths_refused(path):
    with pytest.raises(L.LandingError):
        L.validate_path(path)


def test_editable_paths_accepted():
    for p in L.TEXT_EDITABLE:
        assert L.validate_path("/" + p) == p


def test_images():
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 10
    assert L.check_image("hero.png", png) == []
    assert L.check_image("Hero.PNG", png)                      # upper case refused
    assert L.check_image("hero.gif", png)
    assert L.check_image("hero.png", b"<html>")
    assert L.check_image("x.svg", b"<svg onload='x()'></svg>")
    assert L.check_image("big.png", b"\x89PNG" + b"0" * (L.MAX_IMAGE_BYTES + 1))


def test_branch_and_preview_names():
    b = L.branch_for("jeremy@acquirely.com.au", "mlic-v2")
    assert b == "feature-edit-jeremy-mlic-v2" and b.startswith("feature-")
    url = L.preview_url({"pagesProject": "mlic-v2"}, b)
    assert url == "https://feature-edit-jeremy-mlic-v2.mlic-v2.pages.dev/comparenow"


def test_only_listed_editors():
    cfg = L.LandingConfig("u", "/tmp/x", "t", "w", "r", ("jeremy@acquirely.com.au",), "o")
    L.require_editor(cfg, "Jeremy@Acquirely.com.au")
    with pytest.raises(L.LandingError):
        L.require_editor(cfg, "someone@acquirely.com.au")


# --------------------------------------------------------------------------- git flow (local bare repo as "Bitbucket")

def _sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "origin.git"
    _sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _sh("git", "clone", "-q", str(bare), str(seed))
    for rel in ("checks/sites.json", "mlic-v2/public/index.html", "mlic-v2/public/assets/app.css", "mlic-v2/public/assets/app.js"):
        f = seed / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes((CF_SITES / rel).read_bytes())
    _sh("git", "add", "-A", cwd=seed)
    _sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed", cwd=seed)
    _sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    cfg = L.LandingConfig(str(bare), str(tmp_path / "work"), "tok", "acquirelydev", "cf-landing-sites",
                          ("jeremy@acquirely.com.au",), "sheetal@acquirely.com.au")
    return cfg, bare


def test_save_goes_to_editor_branch_never_main(remote):
    cfg, bare = remote
    ws = L.Workspace(cfg)
    sites = ws.sites()
    assert "mlic-v2" in sites
    branch = L.branch_for("jeremy@acquirely.com.au", "mlic-v2")
    ws.checkout(branch)
    old = ws.read("mlic-v2/public/index.html")
    new = old.replace("in 2 minutes", "in 60 seconds")
    sha = ws.commit_and_push(branch, {"mlic-v2/public/index.html": new.encode()}, "mlic-v2: headline", "jeremy@acquirely.com.au")
    assert _sh("git", "--git-dir", str(bare), "rev-parse", branch) == sha
    main_index = _sh("git", "--git-dir", str(bare), "show", "main:mlic-v2/public/index.html")
    assert "in 2 minutes" in main_index                          # main untouched
    ws.sync()
    assert ws.changed_files(branch) == ["mlic-v2/public/index.html"]
    # a second edit builds on the same branch
    ws.checkout(branch)
    assert "in 60 seconds" in ws.read("mlic-v2/public/index.html")
    with pytest.raises(L.LandingError):                           # identical content
        ws.commit_and_push(branch, {"mlic-v2/public/index.html": new.encode()}, "noop", "jeremy@acquirely.com.au")


# --------------------------------------------------------------------------- publish gate

def test_pipeline_summary():
    assert L.pipeline_summary(None, "abc")["state"] == "not started"
    p = {"state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}}, "target": {"commit": {"hash": "abcdef1234567890"}}, "build_number": 7, "uuid": "{u}"}
    s = L.pipeline_summary(p, "abcdef1234567890")
    assert s["passed"] and s["for_latest_change"]
    assert not L.pipeline_summary(p, "0000000000000000")["for_latest_change"]
    p["state"]["result"]["name"] = "FAILED"
    assert not L.pipeline_summary(p, "abcdef1234567890")["passed"]


class _Resp:
    def __init__(self, data, code=200): self._d, self.status_code, self.text = data, code, json.dumps(data)
    def json(self): return self._d
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)


class _Session:
    def __init__(self): self.posted = None
    def get(self, url, **kw):
        if url.endswith("/pullrequests"): return _Resp({"values": []})
        return _Resp({"values": []})
    def post(self, url, **kw):
        self.posted = kw["json"]; return _Resp({"links": {"html": {"href": "https://bitbucket.org/pr/1"}}})


def test_pr_targets_main_from_editor_branch():
    cfg = L.LandingConfig("u", "/tmp/x", "tok", "acquirelydev", "cf-landing-sites", (), "o")
    s = _Session()
    url = L.Bitbucket(cfg, s).open_pr("feature-edit-jeremy-mlic-v2", "t", "d")
    assert url.endswith("/pr/1")
    assert s.posted["destination"]["branch"]["name"] == "main"
    assert s.posted["source"]["branch"]["name"] == "feature-edit-jeremy-mlic-v2"


def test_publish_tool_refuses_until_checks_pass(remote, monkeypatch):
    cfg, bare = remote
    from design_mcp import server as S
    monkeypatch.setattr(S._landing.LandingConfig, "from_env", classmethod(lambda c: cfg))
    monkeypatch.setattr(S, "resolve_user_email", lambda: "jeremy@acquirely.com.au")
    state = {"p": None}
    monkeypatch.setattr(S._landing.Bitbucket, "latest_pipeline", lambda self, b: state["p"])
    monkeypatch.setattr(S._landing.Bitbucket, "pipeline_log_tail", lambda self, u: "PASS x")
    monkeypatch.setattr(S._landing.Bitbucket, "open_pr", lambda self, b, t, d: "https://bitbucket.org/pr/9")

    # nothing changed yet
    assert not S.landing_request_publish("mlic-v2", "x")["ok"]
    # refused edit never commits
    bad = S.landing_save_file("mlic-v2", "public/index.html", INDEX.replace('id="submit"', 'id="x"'), "bad")
    assert bad.get("refused")
    # protected file
    assert not S.landing_save_file("mlic-v2", "public/assets/app.js", "x", "bad")["ok"]
    # good edit
    ok = S.landing_save_file("mlic-v2", "public/index.html", INDEX.replace("in 2 minutes", "in 60 seconds"), "Headline")
    assert ok["ok"], ok
    head = _sh("git", "--git-dir", str(bare), "rev-parse", "feature-edit-jeremy-mlic-v2")
    # checks not run yet / running / failed / passed-but-old-commit -> refused
    assert not S.landing_request_publish("mlic-v2", "x")["ok"]
    state["p"] = {"state": {"name": "IN_PROGRESS"}, "target": {"commit": {"hash": head}}}
    assert "still running" in S.landing_request_publish("mlic-v2", "x")["error"]
    state["p"] = {"state": {"name": "COMPLETED", "result": {"name": "FAILED"}}, "target": {"commit": {"hash": head}}}
    assert "failed" in S.landing_request_publish("mlic-v2", "x")["error"]
    state["p"] = {"state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}}, "target": {"commit": {"hash": "0" * 40}}}
    assert "latest change" in S.landing_request_publish("mlic-v2", "x")["error"]
    # passed on the latest commit -> PR
    state["p"] = {"state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}}, "target": {"commit": {"hash": head}}, "build_number": 3, "uuid": "{u}"}
    res = S.landing_request_publish("mlic-v2", "Headline")
    assert res["ok"] and res["pull_request"].endswith("/pr/9")
    st = S.landing_status("mlic-v2")
    assert st["ok"] and st["changed_files"] == ["mlic-v2/public/index.html"] and st["checks"]["passed"]


def test_non_editor_is_refused(remote, monkeypatch):
    cfg, _ = remote
    from design_mcp import server as S
    monkeypatch.setattr(S._landing.LandingConfig, "from_env", classmethod(lambda c: cfg))
    monkeypatch.setattr(S, "resolve_user_email", lambda: "hayden@acquirely.com.au")
    r = S.landing_list_sites()
    assert not r["ok"] and "not set up" in r["error"]


def test_consent_wording_is_editable_now():
    new = INDEX.replace("consent to being contacted", "agree to be contacted")
    assert L.check_html_change(INDEX, new) == []


@pytest.mark.parametrize("content, ok", [
    ('{"gtmId": "GTM-KXJFBDD", "metaPixelId": "", "googleTagIds": []}', True),
    ('{"gtmId": "GTM-ABC1234", "metaPixelId": "779295220285622", "googleTagIds": ["G-ABCD1234", "AW-123456789"]}', True),
    ('{"gtmId": "GTM-KXJFBDD\"><script>", "googleTagIds": []}', False),
    ('{"gtmId": "GTM-KXJFBDD", "metaPixelId": "abc"}', False),
    ('{"gtmId": "GTM-KXJFBDD", "script": "alert(1)"}', False),
    ('{"gtmId": "", "metaPixelId": "", "googleTagIds": []}', False),
    ('not json', False),
])
def test_tracking_validation(content, ok):
    assert (L.check_tracking(content) == []) is ok


def test_new_editable_files():
    for p in ("public/assets/footer.html", "public/terms/index.html", "public/privacy/index.html", "public/tracking.json"):
        assert L.validate_path(p) == p
