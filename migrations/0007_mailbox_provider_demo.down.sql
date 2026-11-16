-- Migration: 0007_mailbox_provider_demo.down.sql
-- Fails while a `demo` mailbox exists; delete those mailboxes first.
ALTER TABLE mailbox DROP CONSTRAINT IF EXISTS mailbox_provider_check;
ALTER TABLE mailbox ADD CONSTRAINT mailbox_provider_check
    CHECK (provider IN ('gmail', 'graph', 'imap'));
