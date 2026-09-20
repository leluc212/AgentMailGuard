-- Migration: 0002_mailbox_subscription.up.sql
-- Subscriptions for webhook push notifications (R1.1, R2.2, R2.10)

CREATE TABLE IF NOT EXISTS mailbox_subscription (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    mailbox_id UUID NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    subscription_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    resource TEXT,
    client_state TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    last_renewed_at TIMESTAMPTZ,
    last_renewal_status TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_mailbox_subscription_mailbox UNIQUE (organization_id, mailbox_id),
    CONSTRAINT uq_mailbox_subscription_id UNIQUE (organization_id, subscription_id)
);

CREATE INDEX IF NOT EXISTS idx_mailbox_subscription_expires
    ON mailbox_subscription (expires_at);

CREATE INDEX IF NOT EXISTS idx_mailbox_subscription_mailbox
    ON mailbox_subscription (mailbox_id);
