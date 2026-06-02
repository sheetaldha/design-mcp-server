-- 006_screenshot_cache.sql
--
-- 24-hour per-URL cache for the multi-provider screenshot orchestrator
-- (design_mcp.screenshots.fetch_screenshots).
--
-- One row per unique URL — keyed by sha256(url) so the index stays compact
-- and the row is rewritten on every fresh fetch.

CREATE TABLE IF NOT EXISTS design_mcp_screenshot_cache (
    url_hash         text PRIMARY KEY,
    url              text NOT NULL,
    mobile_url       text NOT NULL,
    ipad_url         text NOT NULL,
    desktop_url      text NOT NULL,
    mobile_provider  text NOT NULL,
    ipad_provider    text NOT NULL,
    desktop_provider text NOT NULL,
    fetched_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS design_mcp_screenshot_cache_fetched_at_idx
    ON design_mcp_screenshot_cache(fetched_at);
