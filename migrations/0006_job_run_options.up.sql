-- Migration: 0006_job_run_options.up.sql
-- Demo run options on the job (ADR-0018 decision 4; design.md §5.9, §7.3; R25.3).
-- {model_profile: fake|live, guard_mode: C0|C3}, copied into every envelope of the job so
-- retries and replays keep it. NULL means the job behaves as before R25.
ALTER TABLE processing_job ADD COLUMN IF NOT EXISTS run_options JSONB;
