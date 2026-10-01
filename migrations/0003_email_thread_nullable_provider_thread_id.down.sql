-- Migration: 0003_email_thread_nullable_provider_thread_id.down.sql
UPDATE email_thread SET provider_thread_id = id::text WHERE provider_thread_id IS NULL;
ALTER TABLE email_thread ALTER COLUMN provider_thread_id SET NOT NULL;
