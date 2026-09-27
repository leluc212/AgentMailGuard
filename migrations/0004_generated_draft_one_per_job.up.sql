-- Migration: 0004_generated_draft_one_per_job.up.sql
-- One draft per processing job: the truth layer behind redelivery idempotency
-- (R19.4, R19.7, design.md §9 "Assert COUNT(generated_draft) == 1").
-- Not CONCURRENTLY: the migrator runs each file inside a transaction.
CREATE UNIQUE INDEX IF NOT EXISTS uq_generated_draft_job
    ON generated_draft (job_id)
    WHERE job_id IS NOT NULL;
