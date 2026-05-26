-- Drafts persistence — replaces the previous in-memory dict.
-- Every draft is scoped to a user_email so cross-user reads/modifications
-- are impossible at the data layer.

CREATE TABLE IF NOT EXISTS design_mcp_drafts (
    design_id      uuid PRIMARY KEY,
    user_email     text NOT NULL,
    family         text NOT NULL,
    brief          text NOT NULL,
    slug_hint      text NOT NULL,
    status         text NOT NULL DEFAULT 'drafted',
    iteration_log  jsonb NOT NULL DEFAULT '[]'::jsonb,
    html           text,
    manifest       jsonb,
    chat_summary   text,
    published_repo_sha text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL DEFAULT (now() + interval '24 hours')
);
CREATE INDEX IF NOT EXISTS design_mcp_drafts_user_email_idx ON design_mcp_drafts(user_email);
CREATE INDEX IF NOT EXISTS design_mcp_drafts_expires_at_idx ON design_mcp_drafts(expires_at);
