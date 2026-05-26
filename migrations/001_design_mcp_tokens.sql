-- Snapshot of the original token table (documentation only — table already
-- exists in production on DO PG 17 acquirely_rel). Kept here so a fresh DB
-- can be bootstrapped from migrations/ in order.

CREATE TABLE IF NOT EXISTS design_mcp_tokens (
    id            bigserial PRIMARY KEY,
    user_email    text NOT NULL,
    note          text,
    token_hash    text NOT NULL UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    usage_count   bigint NOT NULL DEFAULT 0,
    revoked_at    timestamptz
);
