-- Migration: 0005_dispatch_and_review.up.sql
-- Dispatch handle and review feedback (ADR-0009; design.md §5.8, §6; R16.7, R17.3, R19.2, R21.4).
-- generated_draft: the provider draft created or reused in dispatch step 2, and the R19.2
--   dispatch key (operation "dispatch") claimed in dispatch step 1. NULL means unclaimed.
-- feedback: time from opening a draft to deciding, and exactly one decision row per draft.
-- Unique indexes, not CONCURRENTLY: the migrator runs each file inside a transaction.
ALTER TABLE generated_draft ADD COLUMN IF NOT EXISTS provider_draft_id TEXT;
ALTER TABLE generated_draft ADD COLUMN IF NOT EXISTS provider_draft_message_id TEXT;
ALTER TABLE generated_draft ADD COLUMN IF NOT EXISTS dispatch_idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_generated_draft_dispatch_key
    ON generated_draft (dispatch_idempotency_key);

ALTER TABLE feedback ADD COLUMN IF NOT EXISTS review_ms INT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_draft ON feedback (draft_id);
-- uq_feedback_draft serves every lookup the non-unique index did.
DROP INDEX IF EXISTS idx_feedback_draft;
