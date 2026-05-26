-- OAuth 2.1 Authorization Server tables — sit alongside the existing
-- design_mcp_tokens invite-token store. The invite token remains the proof
-- of identity inside the /authorize/login form; the OAuth flow mints
-- short-lived access tokens used by claude.ai (and future) connector
-- clients.
--
-- NOTE on client_secret storage: the MCP SDK's ClientAuthenticator does an
-- hmac.compare_digest on the raw secret returned from get_client(), so we
-- keep BOTH the raw secret (required by the SDK at /token) and a SHA-256
-- hash (audit / sanity). Raw secret is shown to the registering client once
-- in the registration response.

CREATE TABLE IF NOT EXISTS design_mcp_oauth_clients (
    client_id                  text PRIMARY KEY,
    client_secret              text NOT NULL,
    client_secret_hash         text NOT NULL,
    client_name                text,
    redirect_uris              text[] NOT NULL,
    grant_types                text[] NOT NULL DEFAULT ARRAY['authorization_code','refresh_token'],
    response_types             text[] NOT NULL DEFAULT ARRAY['code'],
    token_endpoint_auth_method text NOT NULL DEFAULT 'client_secret_post',
    scope                      text,
    created_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS design_mcp_oauth_codes (
    code_hash             text PRIMARY KEY,
    client_id             text NOT NULL REFERENCES design_mcp_oauth_clients(client_id),
    user_email            text NOT NULL,
    redirect_uri          text NOT NULL,
    redirect_uri_explicit boolean NOT NULL DEFAULT true,
    code_challenge        text NOT NULL,
    code_challenge_method text NOT NULL DEFAULT 'S256',
    scopes                text[] NOT NULL,
    expires_at            timestamptz NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    consumed_at           timestamptz
);

CREATE TABLE IF NOT EXISTS design_mcp_oauth_access_tokens (
    token_hash  text PRIMARY KEY,
    client_id   text NOT NULL REFERENCES design_mcp_oauth_clients(client_id),
    user_email  text NOT NULL,
    scopes      text[] NOT NULL,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

CREATE TABLE IF NOT EXISTS design_mcp_oauth_refresh_tokens (
    token_hash  text PRIMARY KEY,
    client_id   text NOT NULL REFERENCES design_mcp_oauth_clients(client_id),
    user_email  text NOT NULL,
    scopes      text[] NOT NULL,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_oauth_codes_client          ON design_mcp_oauth_codes(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_client  ON design_mcp_oauth_access_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_tokens_client ON design_mcp_oauth_refresh_tokens(client_id);
