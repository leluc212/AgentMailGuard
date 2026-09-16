-- Migration 0001 Down: Core Schema & Business Subsystem Reversible Teardown

-- 1. Drop Business Subsystem Tables
DROP TABLE IF EXISTS ticket CASCADE;
DROP TABLE IF EXISTS order_item CASCADE;
DROP TABLE IF EXISTS "order" CASCADE;
DROP TABLE IF EXISTS product CASCADE;
DROP TABLE IF EXISTS customer CASCADE;

-- 2. Drop Core Processing & Output Tables
DROP TABLE IF EXISTS processing_event CASCADE;
DROP TABLE IF EXISTS feedback CASCADE;
DROP TABLE IF EXISTS embedding_record CASCADE;
DROP TABLE IF EXISTS knowledge_chunk CASCADE;
DROP TABLE IF EXISTS knowledge_document CASCADE;
DROP TABLE IF EXISTS generated_draft CASCADE;
DROP TABLE IF EXISTS thread_state CASCADE;
DROP TABLE IF EXISTS processing_job CASCADE;
DROP TABLE IF EXISTS classification_result CASCADE;
DROP TABLE IF EXISTS attachment CASCADE;

-- 3. Drop Email & Organization Roots
DROP TABLE IF EXISTS email_message CASCADE;
DROP TABLE IF EXISTS email_thread CASCADE;
DROP TABLE IF EXISTS mailbox_checkpoint CASCADE;
DROP TABLE IF EXISTS mailbox CASCADE;
DROP TABLE IF EXISTS organization CASCADE;
