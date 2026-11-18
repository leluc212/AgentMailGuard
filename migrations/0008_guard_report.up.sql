-- Migration: 0008_guard_report.up.sql
-- AgentMailGuard's report of a C3 job (ADR-0018 decision 7; design.md §5.9; R25.5, R25.6).
-- Written in the persist_drafted transaction, so a DRAFTED C3 job always has its report.
-- One row per job; a C0 job has none (GET /v1/jobs/{id}/guard answers 404).
CREATE TABLE IF NOT EXISTS guard_report (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES processing_job(id) ON DELETE CASCADE,
    guard_mode TEXT NOT NULL CHECK (guard_mode IN ('C3')),
    model_profile TEXT,
    schema_version INT NOT NULL,
    inbound_action TEXT,
    final_action TEXT,
    report JSONB NOT NULL,
    guard_llm_calls INT NOT NULL DEFAULT 0,
    timings_ms JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_guard_report_job ON guard_report (job_id);
CREATE INDEX IF NOT EXISTS idx_guard_report_org ON guard_report (organization_id, created_at DESC);
