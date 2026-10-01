-- Migration 0001: Core Schema & Business Subsystem
-- Requirements: R5.1, R5.2, R5.3, R5.4, R5.6, R5.7, R5.10, R13.1

-- Enable authoritative extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1. Organization (Tenant root)
CREATE TABLE IF NOT EXISTS organization (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    settings JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Mailbox
CREATE TABLE IF NOT EXISTS mailbox (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('gmail', 'graph', 'imap')),
    address TEXT NOT NULL,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'needs_reauth')),
    credentials_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, address)
);
CREATE INDEX IF NOT EXISTS idx_mailbox_org ON mailbox(organization_id);

-- 3. Mailbox Checkpoint
CREATE TABLE IF NOT EXISTS mailbox_checkpoint (
    mailbox_id UUID PRIMARY KEY REFERENCES mailbox(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    history_id TEXT,
    delta_link TEXT,
    sync_state TEXT NOT NULL DEFAULT 'idle' CHECK (sync_state IN ('idle', 'syncing', 'full_resync', 'error')),
    last_sync_at TIMESTAMPTZ,
    last_full_sync_at TIMESTAMPTZ,
    pending_followup BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_mailbox_checkpoint_org ON mailbox_checkpoint(organization_id);

-- 4. Email Thread
CREATE TABLE IF NOT EXISTS email_thread (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    mailbox_id UUID NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    provider_thread_id TEXT NOT NULL,
    subject_normalized TEXT,
    participants TEXT[] NOT NULL DEFAULT '{}',
    first_message_at TIMESTAMPTZ,
    last_message_at TIMESTAMPTZ,
    message_count INT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    UNIQUE (organization_id, mailbox_id, provider_thread_id)
);
CREATE INDEX IF NOT EXISTS idx_email_thread_org_mailbox ON email_thread(organization_id, mailbox_id);
CREATE INDEX IF NOT EXISTS idx_email_thread_last_message ON email_thread(organization_id, last_message_at DESC);

-- 5. Email Message
CREATE TABLE IF NOT EXISTS email_message (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    mailbox_id UUID NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    thread_id UUID NOT NULL REFERENCES email_thread(id) ON DELETE CASCADE,
    provider_message_id TEXT NOT NULL,
    rfc822_message_id TEXT,
    in_reply_to TEXT,
    references_ids TEXT[],
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    sender_email TEXT,
    sender_name TEXT,
    recipients JSONB NOT NULL DEFAULT '[]',
    cc JSONB NOT NULL DEFAULT '[]',
    subject TEXT,
    subject_normalized TEXT,
    body_text TEXT,
    body_text_clean TEXT,
    snippet TEXT,
    raw_object_key TEXT,
    html_object_key TEXT,
    received_at TIMESTAMPTZ NOT NULL,
    has_attachments BOOLEAN NOT NULL DEFAULT false,
    normalization_failed BOOLEAN NOT NULL DEFAULT false,
    search_tsv TSVECTOR,
    UNIQUE (organization_id, mailbox_id, provider_message_id)
);
CREATE INDEX IF NOT EXISTS idx_email_message_search_tsv ON email_message USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS idx_email_message_org_thread ON email_message(organization_id, thread_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_message_received_at ON email_message(organization_id, received_at DESC);

-- 6. Attachment
CREATE TABLE IF NOT EXISTS attachment (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    message_id UUID NOT NULL REFERENCES email_message(id) ON DELETE CASCADE,
    filename TEXT,
    mime_type TEXT,
    size_bytes BIGINT,
    object_key TEXT NOT NULL,
    checksum TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_attachment_message ON attachment(message_id);
CREATE INDEX IF NOT EXISTS idx_attachment_org ON attachment(organization_id);

-- 7. Classification Result
CREATE TABLE IF NOT EXISTS classification_result (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    message_id UUID NOT NULL REFERENCES email_message(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    intent TEXT,
    priority TEXT NOT NULL,
    reply_required BOOLEAN NOT NULL,
    retrieval_required BOOLEAN NOT NULL,
    confidence NUMERIC(4,3) NOT NULL,
    decided_by TEXT NOT NULL CHECK (decided_by IN ('rule', 'ml', 'llm', 'default')),
    model_name TEXT,
    latency_ms INT,
    raw JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_classification_message ON classification_result(message_id);
CREATE INDEX IF NOT EXISTS idx_classification_org ON classification_result(organization_id);

-- 8. Processing Job
CREATE TABLE IF NOT EXISTS processing_job (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    message_id UUID REFERENCES email_message(id) ON DELETE SET NULL,
    thread_id UUID REFERENCES email_thread(id) ON DELETE SET NULL,
    job_type TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    idempotency_key TEXT NOT NULL UNIQUE,
    result_ref JSONB,
    queue_name TEXT,
    priority TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    next_retry_at TIMESTAMPTZ,
    trace_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_processing_job_state_retry ON processing_job(state, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_processing_job_org ON processing_job(organization_id);

-- 9. Thread State
CREATE TABLE IF NOT EXISTS thread_state (
    thread_id UUID PRIMARY KEY REFERENCES email_thread(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    topic TEXT,
    current_intent TEXT,
    summary TEXT,
    open_questions JSONB NOT NULL DEFAULT '[]',
    resolved_items JSONB NOT NULL DEFAULT '[]',
    summarized_through_message_id UUID REFERENCES email_message(id) ON DELETE SET NULL,
    token_estimate INT,
    version INT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_thread_state_org ON thread_state(organization_id);

-- 10. Generated Draft
CREATE TABLE IF NOT EXISTS generated_draft (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    job_id UUID REFERENCES processing_job(id) ON DELETE SET NULL,
    message_id UUID NOT NULL REFERENCES email_message(id) ON DELETE CASCADE,
    thread_id UUID NOT NULL REFERENCES email_thread(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    confidence NUMERIC(4,3),
    citations JSONB NOT NULL DEFAULT '[]',
    citation_mismatch BOOLEAN NOT NULL DEFAULT false,
    model_name TEXT,
    model_tier TEXT,
    escalation_reason TEXT,
    prompt_version TEXT,
    input_tokens INT,
    output_tokens INT,
    cost_estimate NUMERIC(10,6),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected', 'dispatched')),
    provider_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_generated_draft_org ON generated_draft(organization_id);
CREATE INDEX IF NOT EXISTS idx_generated_draft_msg ON generated_draft(message_id);
CREATE INDEX IF NOT EXISTS idx_generated_draft_thread ON generated_draft(thread_id);

-- 11. Knowledge Document
CREATE TABLE IF NOT EXISTS knowledge_document (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source_uri TEXT,
    mime_type TEXT,
    category TEXT,
    version INT NOT NULL DEFAULT 1,
    checksum TEXT,
    object_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'parsing', 'chunking', 'embedding', 'active', 'superseded', 'failed')),
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_knowledge_document_org ON knowledge_document(organization_id);

-- 12. Knowledge Chunk
CREATE TABLE IF NOT EXISTS knowledge_chunk (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES knowledge_document(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    external_id TEXT,
    heading_path TEXT[],
    section TEXT,
    category TEXT,
    content TEXT NOT NULL,
    token_count INT,
    content_checksum TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    version INT NOT NULL,
    content_tsv TSVECTOR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_content_tsv ON knowledge_chunk USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_org_cat ON knowledge_chunk(organization_id, category);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_doc ON knowledge_chunk(document_id);

-- 13. Embedding Record
CREATE TABLE IF NOT EXISTS embedding_record (
    chunk_id UUID PRIMARY KEY REFERENCES knowledge_chunk(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INT NOT NULL,
    embedding VECTOR(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_embedding_record_embedding_hnsw ON embedding_record USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_embedding_record_org ON embedding_record(organization_id);

-- 14. Feedback
CREATE TABLE IF NOT EXISTS feedback (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    draft_id UUID NOT NULL REFERENCES generated_draft(id) ON DELETE CASCADE,
    reviewer TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'edited', 'rejected')),
    edited_body TEXT,
    edit_distance INT,
    rating INT,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_feedback_draft ON feedback(draft_id);
CREATE INDEX IF NOT EXISTS idx_feedback_org ON feedback(organization_id);

-- 15. Processing Event
CREATE TABLE IF NOT EXISTS processing_event (
    id BIGSERIAL PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    job_id UUID REFERENCES processing_job(id) ON DELETE SET NULL,
    message_id UUID REFERENCES email_message(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    state_from TEXT,
    state_to TEXT,
    payload JSONB,
    trace_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_processing_event_msg_created ON processing_event(message_id, created_at);
CREATE INDEX IF NOT EXISTS idx_processing_event_org ON processing_event(organization_id);
CREATE INDEX IF NOT EXISTS idx_processing_event_job ON processing_event(job_id);

-- 16-20. Business Subsystem (R13.1)
CREATE TABLE IF NOT EXISTS customer (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    account_status TEXT NOT NULL DEFAULT 'active',
    tier TEXT NOT NULL DEFAULT 'standard',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_customer_org_email ON customer(organization_id, email);

CREATE TABLE IF NOT EXISTS product (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    price NUMERIC(12,2) NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_product_org_sku ON product(organization_id, sku);

CREATE TABLE IF NOT EXISTS "order" (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES customer(id) ON DELETE CASCADE,
    order_number TEXT NOT NULL,
    status TEXT NOT NULL,
    total NUMERIC(12,2) NOT NULL,
    placed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    shipped_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_order_org_customer ON "order"(organization_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_order_org_number ON "order"(organization_id, order_number);

CREATE TABLE IF NOT EXISTS order_item (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    order_id UUID NOT NULL REFERENCES "order"(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES product(id) ON DELETE RESTRICT,
    quantity INT NOT NULL,
    unit_price NUMERIC(12,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_item_order ON order_item(order_id);
CREATE INDEX IF NOT EXISTS idx_order_item_org ON order_item(organization_id);

CREATE TABLE IF NOT EXISTS ticket (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES customer(id) ON DELETE CASCADE,
    ticket_number TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    subject TEXT NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ticket_org_customer ON ticket(organization_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_ticket_org_number ON ticket(organization_id, ticket_number);
