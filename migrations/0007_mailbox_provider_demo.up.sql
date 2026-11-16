-- Migration: 0007_mailbox_provider_demo.up.sql
-- The demo console's mailbox provider (ADR-0018 decision 3; design.md §5.9; R25.1).
-- A `demo` mailbox receives the emails POST /v1/demo/messages injects, and its dispatch adapter
-- records the outbound message instead of sending it, so an approved demo draft never reaches a
-- real mailbox. The dispatch-worker picks the adapter by mailbox.provider, hence a value of its own.
ALTER TABLE mailbox DROP CONSTRAINT IF EXISTS mailbox_provider_check;
ALTER TABLE mailbox ADD CONSTRAINT mailbox_provider_check
    CHECK (provider IN ('gmail', 'graph', 'imap', 'demo'));
