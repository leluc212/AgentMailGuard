-- Migration: 0006_job_run_options.down.sql
ALTER TABLE processing_job DROP COLUMN IF EXISTS run_options;
