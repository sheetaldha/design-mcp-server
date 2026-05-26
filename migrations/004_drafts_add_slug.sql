-- 004_drafts_add_slug.sql
--
-- drafts.py writes to a `slug` column (the final chosen slug on submit),
-- distinct from `slug_hint` (the auto-derived suggestion at draft time).
-- The original 003_drafts.sql migration missed it.

ALTER TABLE design_mcp_drafts
    ADD COLUMN IF NOT EXISTS slug text;
