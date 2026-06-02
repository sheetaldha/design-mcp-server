# GitHub Actions workflows

## `deploy.yml` — test + deploy to microsite-uat

### Triggers
- Push to `main` (every merge auto-deploys)
- Manual run via the **Actions** tab → "Test and deploy to microsite-uat" → **Run workflow** (`workflow_dispatch`)

### What it does
1. **test** job — checks out code, installs Python 3.13 + uv, `uv sync --frozen --extra dev`, runs `pytest -x`. Deploy is gated on this passing.
2. **deploy** job (only runs if tests pass):
   - Loads SSH key from `EC2_SSH_PRIVATE_KEY` secret into ssh-agent
   - `ssh-keyscan` adds EC2 to known_hosts
   - `rsync -av --delete src/design_mcp/` → `ubuntu@13.238.39.183:/home/ubuntu/design-mcp-server/src/design_mcp/`
   - SSH to EC2 → `uv pip install -e .` (picks up any new deps from `pyproject.toml`)
   - SSH to EC2 → `pm2 restart design-mcp-server --update-env`
   - `sleep 3` then `curl` `https://design-mcp.leadloom.com.au/` — any HTTP response (200/301/302/4xx) counts as healthy; connection refused or 5xx fails the job
   - Logs the deployed commit SHA

### Where to see logs
**Actions** tab on GitHub → pick the run → expand each step.

### How to manually trigger
**Actions** tab → "Test and deploy to microsite-uat" → **Run workflow** button (top right). Pick the branch (usually `main`) → Run.

### What this workflow does **not** do (intentional)
- **DB migrations** — never auto-applied. SSH to EC2 and run them manually after human review of the SQL.
- **`.env` editing** — `/home/ubuntu/design-mcp-server/.env` is managed manually by Sheetal. The workflow never touches it.
- **Approval gate** — there's no manual approve-before-deploy step. Small team, fast iteration is the explicit choice. If you want one later, add an `environment:` with required reviewers on the deploy job.

### Common failures + how to debug

| Symptom | Likely cause | Fix |
|---|---|---|
| `Permission denied (publickey)` on SSH/rsync | `EC2_SSH_PRIVATE_KEY` secret missing/corrupt, or matching public key removed from EC2 | Re-check the secret in repo Settings → Secrets and variables → Actions. SSH to EC2 and confirm public key is in `~ubuntu/.ssh/authorized_keys` |
| `rsync: write failed: No space left on device` | EC2 disk full | `ssh ubuntu@13.238.39.183 'df -h /'`, clean up `/var/log` or old PM2 logs |
| `pm2: command not found` or restart fails | PM2 process missing or PM2 not in PATH for non-login shell | `ssh ubuntu@13.238.39.183 'pm2 list'`; if missing, `pm2 start ... && pm2 save` |
| Smoke test returns `000` or 5xx | FastMCP didn't come up after restart | `ssh ubuntu@13.238.39.183 'pm2 logs design-mcp-server --lines 30'`; also check `sudo journalctl -u nginx --since "5 min ago"` for upstream errors |
| Smoke test fails but service is fine | DNS or cert hiccup on `design-mcp.leadloom.com.au` | `curl -v` from your laptop, check Cloudflare/DNS |

### Rotating the SSH key
1. Locally: `ssh-keygen -t ed25519 -f ~/.ssh/design_mcp_deploy_new -N ""`
2. Append `design_mcp_deploy_new.pub` to `ubuntu@13.238.39.183:~/.ssh/authorized_keys`
3. GitHub repo → Settings → Secrets and variables → Actions → update `EC2_SSH_PRIVATE_KEY` with the new private key contents (full PEM, including BEGIN/END lines)
4. Trigger a `workflow_dispatch` run to confirm the new key works
5. Once green, remove the old public key from `authorized_keys` on EC2
