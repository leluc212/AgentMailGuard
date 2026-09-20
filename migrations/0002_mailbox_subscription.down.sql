-- Migration: 0002_mailbox_subscription.down.sql
-- Reversible rollback for mailbox_subscription table (R5.5)

DROP TABLE IF EXISTS mailbox_subscription CASCADE;
