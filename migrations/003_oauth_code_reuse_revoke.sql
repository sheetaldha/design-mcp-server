-- 003_oauth_code_reuse_revoke.sql
--
-- Adds two capabilities:
--
--   M1: Authorization-code-reuse defence. When an already-consumed code is
--       presented at /token, OAuth 2.1 §4.1.2 requires the AS to revoke any
--       access/refresh tokens that were derived from that code. To do this
--       we need to know which code minted which token, so each access and
--       refresh row records the SHA-256 hash of the originating auth code.
--
--   L2: Public clients (no secret). DCR clients that do not present a
--       client_secret at /token (PKCE-only public clients) must be storable
--       without a fake empty-string secret. Make client_secret and
--       client_secret_hash NULLable. Existing rows are unaffected.
--
-- All changes are additive / NULL-defaulted; no data migration required.

ALTER TABLE design_mcp_oauth_access_tokens
    ADD COLUMN IF NOT EXISTS auth_code_hash text;

ALTER TABLE design_mcp_oauth_refresh_tokens
    ADD COLUMN IF NOT EXISTS auth_code_hash text;

CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_auth_code
    ON design_mcp_oauth_access_tokens(auth_code_hash);

CREATE INDEX IF NOT EXISTS idx_oauth_refresh_tokens_auth_code
    ON design_mcp_oauth_refresh_tokens(auth_code_hash);

ALTER TABLE design_mcp_oauth_clients
    ALTER COLUMN client_secret      DROP NOT NULL,
    ALTER COLUMN client_secret_hash DROP NOT NULL;
