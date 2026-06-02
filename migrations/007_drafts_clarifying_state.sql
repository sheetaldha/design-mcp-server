-- 007_drafts_clarifying_state.sql
--
-- Server-driven clarifying-question state machine for the landing-page
-- intake flow. Adds a single jsonb column on `design_mcp_drafts` that the
-- server walks (one field per turn) so the caller's Claude is reduced to
-- a question-asker — the question text, options, and order are owned here,
-- not improvised by the model.
--
-- Payload shape (documentation only — column is untyped jsonb):
--
--   {
--     "current_field_index": 0,            -- pointer into the field list (0-based)
--     "collected": {                       -- user answers recorded so far
--       "page_intent": "New microsite landing page",
--       "site_name": "HealthBoost",
--       ...
--     },
--     "skipped": ["palette"],              -- fields the user explicitly skipped
--     "checkpoint_state": "pending"        -- "pending" | "confirmed"; only meaningful
--                                          --   when the current field is a checkpoint
--   }
--
-- DEFAULT '{}'::jsonb so existing rows back-fill cleanly (treated as a
-- fresh-empty state by the state machine). The state-machine module
-- (`src/design_mcp/intake_state_machine.py`) is the single source of truth
-- for the payload contract; this comment is informational only.

ALTER TABLE design_mcp_drafts
  ADD COLUMN IF NOT EXISTS clarifying_state jsonb NOT NULL DEFAULT '{}'::jsonb;
