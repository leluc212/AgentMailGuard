-- Migration: 0005_dispatch_and_review.down.sql
CREATE INDEX IF NOT EXISTS idx_feedback_draft ON feedback (draft_id);
DROP INDEX IF EXISTS uq_feedback_draft;
ALTER TABLE feedback DROP COLUMN IF EXISTS review_ms;

DROP INDEX IF EXISTS uq_generated_draft_dispatch_key;
ALTER TABLE generated_draft DROP COLUMN IF EXISTS dispatch_idempotency_key;
ALTER TABLE generated_draft DROP COLUMN IF EXISTS provider_draft_message_id;
ALTER TABLE generated_draft DROP COLUMN IF EXISTS provider_draft_id;
