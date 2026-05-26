# design-mcp-server

Hosted MCP server that generates new microsite HTML + YAML manifest from natural-language chat. Designed for Hayden/Jeremy to use from claude.ai (web/mobile) without installing Claude Code.

## Architecture

```
Hayden/Jeremy → claude.ai → adds this MCP as Custom Connector
   ↓ chats: "design a landing page for HealthBoost UAT, 50+ demo, blue palette"
[design-mcp-server — running on microsite-uat EC2 :8050, fronted by nginx + LE]
   ↓ tools:
   - design_landing_page(brief, references[])
   - design_survey_funnel(brief, steps, otp)
   - get_design_status(design_id)
   - update_design(design_id, instructions)
   - submit_design(design_id)   ← commits to microsite-design-skills Bitbucket repo
   ↓
Bitbucket → Slack #design-handoffs
   ↓ Sheetal pulls, reviews via cms-mcp-server + orchestrator
   ↓ preview deploys to preview-<slug>.uat.connectxpert.com.au
   ↓ Sheetal approves
Live at uat.<slug>.com.au
```

## Quick start (local dev)

```bash
git clone git@github.com:sheetaldha/design-mcp-server.git
cd design-mcp-server
uv venv --python 3.13 && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env  # fill in ANTHROPIC_API_KEY, TOKEN_DB_PASSWORD
design-mcp            # starts stdio MCP locally
```

## Production deploy (microsite-uat EC2)

See `STATUS.md` (local-only) for the deploy walkthrough.

Roughly: PM2 process on port 8050, nginx vhost at `design-mcp.leadloom.com.au` proxies to it, LetsEncrypt cert via certbot.

## Status

See `STATUS.md` (gitignored) for in-progress notes + Day 1-7 build sequence.
