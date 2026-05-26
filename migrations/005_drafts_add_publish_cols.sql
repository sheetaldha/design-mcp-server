-- 005_drafts_add_publish_cols.sql
--
-- drafts.py records git commit_sha, on-disk design_dir, and the last
-- publish error message on each draft. The original 003_drafts.sql
-- migration didn't include these columns. Additive, NULL-safe.

ALTER TABLE design_mcp_drafts
    ADD COLUMN IF NOT EXISTS commit_sha text,
    ADD COLUMN IF NOT EXISTS design_dir text,
    ADD COLUMN IF NOT EXISTS last_error text;
