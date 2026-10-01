-- Migration: 0003_email_thread_nullable_provider_thread_id.up.sql
-- Align email_thread.provider_thread_id with design.md §6.1 (nullable for IMAP and non-provider threads)

ALTER TABLE email_thread ALTER COLUMN provider_thread_id DROP NOT NULL;
