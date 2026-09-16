# TECHNICAL PROPOSAL

## Enterprise RAG-Based Intelligent Email Management and Response System

**Project Type:** Final-Year Project  
**System Domain:** Artificial Intelligence, Retrieval-Augmented Generation, Email Automation, Distributed Systems  
**Proposed Architecture:** Event-Driven Email Processing + Intelligent Routing + Hybrid RAG + LLM-Based Response Generation

---

## Abstract

Enterprise email remains one of the primary communication channels for customer support, sales, billing, internal operations, and service delivery. Medium and large organizations may receive tens or hundreds of thousands of emails every day. Although Large Language Models (LLMs) are capable of understanding and generating email responses, directly forwarding every incoming message to a general-purpose LLM is expensive, difficult to scale, and unable to reliably incorporate organization-specific knowledge.

This project proposes an **enterprise-oriented intelligent email management and response system based on Retrieval-Augmented Generation (RAG)**. The system automatically receives email events from providers such as Gmail and Microsoft Outlook, synchronizes new messages into an internal operational database, classifies each message according to business category and intent, routes actionable messages into workload-specific queues, retrieves relevant organizational knowledge through hybrid lexical and semantic search, constructs conversation-aware context, and generates an appropriate response through an LLM-based agent.

The proposed architecture separates inexpensive deterministic and classification operations from computationally expensive retrieval and generation operations. RAG is invoked selectively only when external organizational knowledge is required. Email conversations are modeled separately from the knowledge base through persistent thread state and conversation summarization. Retrieval combines PostgreSQL full-text search with vector similarity using pgvector and Reciprocal Rank Fusion, followed by optional reranking. This approach reduces model usage while improving grounding and retrieval accuracy.

The architecture is deliberately designed to be feasible for implementation by a student team using commodity infrastructure while retaining a clear horizontal scaling path toward medium- and large-enterprise workloads. A reference workload of **10,000 mailboxes and 100,000 incoming emails per day** is used to evaluate scalability. The system targets typical end-to-end processing latency of **2–6 seconds for AI-generated drafts**, while asynchronous email semantics permit significantly greater tolerance during workload spikes.

Security architecture—including authorization, access-control policy, prompt-injection mitigation, data-loss prevention, encryption strategy, and related controls—is intentionally outside the scope of this proposal and is intended to be designed as a separate cross-cutting subsystem.

---

# 1. Introduction

Email-processing automation traditionally relies on manually defined filters, keyword rules, templates, customer-service ticketing systems, or rule-based workflow engines. These systems perform well when email structure and user intent are predictable but become difficult to maintain when incoming messages contain natural language, ambiguous requests, long conversation histories, or organization-specific questions.

LLMs provide a potential solution because they can classify natural-language requests, understand conversational context, summarize long discussions, and generate fluent responses. However, an architecture in which every incoming email is immediately forwarded to an LLM introduces three major engineering problems:

1. excessive inference cost;
2. unnecessary processing latency;
3. insufficient access to current organization-specific information.

Retrieval-Augmented Generation addresses the third problem by supplying the LLM with relevant external knowledge at inference time. Nevertheless, RAG alone does not solve the complete email-management problem.

The proposed system therefore treats email response generation as a **distributed information-processing pipeline**, rather than as a single LLM invocation.

The central architectural principle is:

> **Classify first, retrieve only when required, generate only when necessary.**

The resulting processing model is:

```text
Email
  ↓
Synchronization
  ↓
Normalization
  ↓
Classification
  ↓
Routing / Queueing
  ↓
Context Construction
  ↓
Hybrid RAG
  ↓
LLM Response Generation
  ↓
Draft / Business Workflow
  ↓
Email Dispatch
```

This architecture allows computational resources to be allocated according to the complexity and business value of each email.

---

# 2. Problem Statement

Organizations receive heterogeneous email traffic containing different intentions and requiring different levels of processing.

Examples include:

- support requests;
- billing questions;
- product inquiries;
- sales opportunities;
- scheduling messages;
- notifications;
- acknowledgment messages;
- newsletters;
- automated system reports;
- messages that require no response.

A naïve LLM email-processing implementation might perform the following operation:

```text
Incoming Email
      ↓
LLM
      ↓
Generated Reply
```

Such a design is inefficient because every email consumes model inference capacity even when no reasoning or reply is necessary.

A naïve RAG implementation presents similar problems:

```text
Incoming Email
      ↓
Embedding
      ↓
Vector Search
      ↓
LLM
      ↓
Reply
```

Every email triggers retrieval regardless of whether organizational knowledge is required. Conversation history, operational data, message routing, workload prioritization, state management, failure recovery, and scalability are also inadequately represented.

The engineering problem addressed by this project is therefore:

> **How can an organization automatically process, classify, retrieve contextual knowledge for, and generate responses to large volumes of email while maintaining acceptable response quality, latency, operating cost, and system scalability?**

---

# 3. Project Objectives

The primary objective is to design and implement a realistic RAG-based email-processing architecture capable of supporting enterprise-style workloads.

The system shall:

1. integrate with real email providers;
2. receive mailbox changes asynchronously;
3. maintain synchronized internal email and conversation state;
4. classify incoming email by business category and intent;
5. identify messages requiring no response;
6. route messages into category-specific processing queues;
7. preserve email-thread context;
8. retrieve relevant organizational information using hybrid search;
9. avoid unnecessary retrieval and LLM inference;
10. generate contextual email replies using category-specific agent profiles;
11. support business or operational data alongside RAG knowledge;
12. persist generated drafts and processing state;
13. tolerate temporary processing failures;
14. scale horizontally by adding workers;
15. expose measurable latency, throughput, retrieval, classification, and cost metrics.

The design is intended to satisfy two apparently conflicting requirements:

**FYP feasibility:** the complete system must be deployable by a small student development team.

**Enterprise credibility:** the logical architecture must remain valid when processing significantly larger workloads.

---

# 4. Scope

## 4.1 In Scope

The proposed system covers:

- Gmail integration;
- Microsoft Outlook / Microsoft Graph integration;
- mailbox synchronization;
- MIME and email-content normalization;
- email metadata persistence;
- thread management;
- email categorization;
- intent classification;
- reply-requirement classification;
- priority assignment;
- asynchronous queue processing;
- Retrieval-Augmented Generation;
- semantic search;
- lexical search;
- Reciprocal Rank Fusion;
- retrieval reranking;
- knowledge ingestion;
- document chunking;
- embedding generation;
- conversation summarization;
- model routing;
- LLM response generation;
- draft persistence;
- email dispatch;
- retry processing;
- dead-letter processing;
- observability;
- cost measurement;
- latency measurement;
- retrieval evaluation;
- response-quality evaluation.

## 4.2 Explicitly Out of Scope

The following concerns are intentionally excluded from this proposal:

- authentication architecture;
- authorization architecture;
- access-control enforcement;
- encryption architecture;
- data-loss-prevention controls;
- prompt-injection defenses;
- malware detection;
- phishing detection;
- secret detection;
- email-content security classification;
- security auditing;
- regulatory-compliance design;
- threat modeling.

These areas can be implemented as cross-cutting services surrounding the proposed core architecture without requiring its fundamental processing model to be redesigned.

Provider authentication mechanisms required by Gmail or Microsoft Graph are assumed to exist as integration prerequisites but are not architecturally evaluated in this proposal.

---

# 5. Architectural Principles

The proposed architecture follows six primary principles.

### 5.1 Event-Driven Processing

Email arrival should create work rather than require continuous mailbox polling.

Gmail supports server push notifications through Google Cloud Pub/Sub specifically to avoid the network and compute cost of repeatedly polling mailboxes. Gmail notifications contain mailbox state information that can subsequently be used with `history.list()` for incremental synchronization.

Microsoft Graph similarly supports change-notification subscriptions for Outlook resources and can combine these notifications with delta queries to synchronize only resources that have changed.

### 5.2 Separation of Routing and Retrieval

Classification answers:

> **What kind of email is this?**

RAG answers:

> **What organizational knowledge is relevant to answering it?**

These are different problems and should therefore be separate services.

### 5.3 Selective AI Execution

Not every email requires:

- an embedding;
- vector retrieval;
- reranking;
- an LLM-generated response.

The system should terminate processing early whenever sufficient deterministic information exists.

### 5.4 Asynchronous Workload Decoupling

Email ingestion and email response generation should operate independently.

An email provider should never need to wait for an LLM response.

### 5.5 Stateful Conversations, Stateless Workers

Conversation state should reside in persistent storage.

Processing workers should remain stateless and horizontally scalable.

### 5.6 Progressive Infrastructure Complexity

The first deployment should use the smallest infrastructure capable of satisfying performance requirements.

A dedicated distributed search platform should only be introduced after measurements demonstrate that PostgreSQL-based retrieval is insufficient.

---

# 6. Proposed High-Level Architecture

```text
┌────────────────────────────────────────────────────────────────────┐
│                         EMAIL PROVIDERS                            │
│                                                                    │
│             Gmail API       Microsoft Graph       IMAP             │
└───────────────────────────┬────────────────────────────────────────┘
                            │
                  Push / Webhook / Sync Event
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                    MAIL CONNECTOR SERVICE                          │
│                                                                    │
│  Subscription management                                           │
│  Incremental synchronization                                       │
│  Message/thread retrieval                                          │
│  Provider abstraction                                              │
└───────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼
                     MESSAGE BROKER
                        RabbitMQ
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                  EMAIL PROCESSING SERVICE                          │
│                                                                    │
│ MIME parsing → HTML normalization → text extraction → persistence  │
└───────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                       TRIAGE ENGINE                                │
│                                                                    │
│ Rules → Lightweight Classifier → LLM Fallback                     │
│                                                                    │
│ category | intent | priority | reply_required | confidence         │
└───────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼
                      ROUTING EXCHANGE
                            │
       ┌────────────────────┼─────────────────────┐
       ▼                    ▼                     ▼
 Support Queue         Billing Queue          Sales Queue
       │                    │                     │
       └────────────────────┼─────────────────────┘
                            ▼
                      AI WORKER POOL
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                      CONTEXT BUILDER                               │
│                                                                    │
│   Current Message + Thread State + Business Context + RAG Context  │
└───────────────────────────┬────────────────────────────────────────┘
                            │
             ┌──────────────┴───────────────┐
             ▼                              ▼
       THREAD CONTEXT                  HYBRID RAG
                                            │
                                  ┌─────────┴────────┐
                                  ▼                  ▼
                           Full-Text Search       Vector HNSW
                                  │                  │
                                  └────────┬─────────┘
                                           ▼
                                      RRF Fusion
                                           │
                                           ▼
                                        Reranker
                                           │
                                           ▼
                                         Top-K
             └─────────────────────┬───────────────┘
                                   ▼
                           LLM REPLY AGENT
                                   │
                                   ▼
                           STRUCTURED RESULT
                                   │
                                   ▼
                              DRAFT STORE
                                   │
                                   ▼
                            MAIL DISPATCHER
                                   │
                                   ▼
                              EMAIL PROVIDER
```

A second asynchronous pipeline maintains the knowledge base:

```text
Company Documents
       │
       ▼
Document Parser
       │
       ▼
Structural Chunker
       │
       ▼
Metadata Enrichment
       │
       ├───────────────┐
       ▼               ▼
Full-Text Index    Embedding Worker
       │               │
       └───────┬───────┘
               ▼
      PostgreSQL + pgvector
```

---

# 7. Email Ingestion Architecture

## 7.1 Provider Integration

A provider abstraction layer isolates the application from email-provider-specific APIs.

Conceptually:

```text
MailProviderAdapter

+ subscribe()
+ synchronize()
+ get_message()
+ get_thread()
+ create_draft()
+ send_reply()
```

Implementations may include:

```text
GmailProviderAdapter
MicrosoftGraphProviderAdapter
IMAPProviderAdapter
```

Business logic therefore interacts with a common interface rather than directly depending on Gmail or Microsoft Graph.

---

# 8. Incremental Synchronization

A webhook should be treated as a **change notification**, not as the authoritative application state.

The application maintains a synchronization checkpoint for every mailbox.

For Gmail:

```text
mailbox_id
last_history_id
last_sync_at
```

Gmail supports partial synchronization using `history.list()` with a previously stored `historyId`; when the history window is no longer available, the client performs a full synchronization.

For Microsoft Graph:

```text
mailbox_id
delta_link
last_sync_at
```

Microsoft Graph delta queries return state through `@odata.nextLink` and eventually `@odata.deltaLink`, allowing subsequent requests to retrieve only changes since the previous synchronization.

The synchronization flow therefore becomes:

```text
Provider notification
        ↓
Mailbox ID
        ↓
Read sync checkpoint
        ↓
Fetch incremental changes
        ↓
Merge into PostgreSQL
        ↓
Update checkpoint
```

This architecture reduces provider API requests and enables recovery after temporary worker outages.

---

# 9. Email Data Model

PostgreSQL acts as the authoritative operational database.

Core entities include:

| Entity | Responsibility |
|---|---|
| `organization` | Logical enterprise/customer |
| `mailbox` | Connected email mailbox |
| `mailbox_checkpoint` | Synchronization state |
| `email_thread` | Conversation-level representation |
| `email_message` | Individual email |
| `attachment` | Attachment metadata |
| `classification_result` | Category/intent prediction |
| `processing_job` | Asynchronous unit of work |
| `thread_state` | Summarized conversation state |
| `generated_draft` | LLM-generated response |
| `knowledge_document` | Original RAG document |
| `knowledge_chunk` | Searchable document segment |
| `embedding_record` | Vector representation |
| `feedback` | User acceptance/edit/rejection |
| `processing_event` | Operational telemetry |

A message should retain the original provider identifier so processing can be idempotent.

Example logical key:

```text
organization_id
+
mailbox_id
+
provider_message_id
```

---

# 10. Email Processing and Normalization

Incoming email formats vary substantially.

The processing service converts provider-specific representations into a normalized internal format.

Example:

```json
{
  "message_id": "...",
  "thread_id": "...",
  "mailbox_id": "...",
  "sender": "...",
  "recipients": [],
  "cc": [],
  "subject": "...",
  "body_text": "...",
  "body_html": "...",
  "received_at": "...",
  "attachments": [],
  "provider": "gmail"
}
```

Normalization should perform:

- MIME parsing;
- plain-text extraction;
- HTML-to-text conversion;
- quoted-history separation;
- signature detection where practical;
- attachment metadata extraction;
- subject normalization;
- thread association;
- duplicate detection.

The result is a provider-independent message representation consumed by downstream services.

---

# 11. Intelligent Triage

The triage subsystem minimizes unnecessary AI computation.

Instead of:

```text
100,000 emails
      ↓
100,000 expensive LLM requests
```

the system executes a cascading classifier.

```text
Email
  │
  ▼
Deterministic Rules
  │
  │ uncertain
  ▼
Lightweight Classifier
  │
  │ uncertain
  ▼
Small LLM Classifier
```

A structured classification response may be:

```json
{
  "category": "technical_support",
  "intent": "password_reset",
  "priority": "normal",
  "reply_required": true,
  "retrieval_required": true,
  "confidence": 0.96
}
```

Possible business categories include support, sales, billing, administration, scheduling, general inquiry, automated notification, acknowledgment, and no-response.

The important optimization is:

> **Classification does not automatically imply generation.**

An email may be classified successfully and terminate without ever reaching RAG or an LLM response generator.

---

# 12. Category-Aware Work Scheduling

Emails requiring additional processing are published to category-specific queues.

For example:

```text
email.support.normal
email.support.priority
email.sales.normal
email.billing.normal
email.general.normal
```

RabbitMQ is suitable for the reference implementation because it provides asynchronous work distribution, consumer acknowledgments, retries, dead-letter routing, and horizontal consumers.

For production-style high-availability deployments, RabbitMQ recommends quorum queues when replicated durable queues are required. Quorum queues replicate queue state using Raft and support mechanisms such as dead lettering and poison-message handling.

### Logical Batching vs Prompt Batching

The system supports **worker-level micro-batching**, but does not concatenate unrelated email messages into a single LLM conversation.

Correct:

```text
Worker Batch
   ├── Email Job A → independent inference
   ├── Email Job B → independent inference
   ├── Email Job C → independent inference
   └── Email Job D → independent inference
```

Incorrect:

```text
Prompt:
Email A ...
Email B ...
Email C ...

"Respond to all three."
```

This distinction preserves email independence while allowing efficient worker scheduling and API utilization.

---

# 13. Conversation and Thread Management

Email is conversational rather than document-oriented.

Consider:

```text
Customer:
"The printer does not work."

Agent:
"Please restart the printer."

Customer:
"I already did that."
```

The final message contains insufficient information when processed independently.

Therefore the system maintains a persistent thread state.

Example:

```json
{
  "thread_id": "TH-20198",
  "topic": "printer malfunction",
  "current_intent": "technical_support",
  "summary": "Customer cannot print. Restart has already been attempted.",
  "open_questions": [
    "printer model",
    "displayed error code"
  ],
  "resolved_items": [
    "restart attempted"
  ]
}
```

For short threads, the system may provide all recent messages directly.

For long threads, context should be constructed as:

```text
Thread Summary
+
Latest Relevant Messages
+
Current Email
```

This prevents long conversations from repeatedly consuming large context windows.

Thread summarization occurs only when a configured threshold is reached, such as:

```text
message_count > threshold
OR
estimated_context_tokens > threshold
```

A new summary does not need to be generated for every message.

---

# 14. Retrieval-Augmented Generation Architecture

RAG is invoked **after triage**, not before.

Its purpose is to retrieve organizational knowledge needed to answer the classified email.

Potential RAG sources include:

- product documentation;
- frequently asked questions;
- operating procedures;
- service instructions;
- approved response templates;
- pricing documentation;
- process documentation;
- technical documentation;
- approved historical answers;
- organization-specific reference material.

Incoming email itself should primarily belong to the thread/conversation subsystem rather than being indiscriminately added to the organizational knowledge corpus.

---

# 15. Knowledge Ingestion Pipeline

Knowledge indexing is asynchronous.

```text
Document
   ↓
Parser
   ↓
Document Structure Extraction
   ↓
Chunking
   ↓
Metadata Enrichment
   ↓
Embedding
   ↓
Index Persistence
```

Each chunk should contain both text and metadata.

Example:

```json
{
  "document_id": "DOC-125",
  "chunk_id": "DOC-125-08",
  "title": "Account Recovery Procedure",
  "section": "Locked Account",
  "category": "technical_support",
  "version": 4,
  "content": "...",
  "embedding": [...]
}
```

A reasonable initial experimental chunk size is approximately **350–700 tokens**, but this should be treated as a tunable parameter rather than a fixed optimal value.

Semantic boundaries such as sections, headings, paragraphs, and list groups should take precedence over arbitrary fixed-character splitting.

---

# 16. Hybrid Retrieval

Pure vector retrieval is insufficient for enterprise email.

Emails frequently contain exact identifiers such as:

```text
INV-2026-01829
INC00087219
TCLU1234567
ORDER-58192
SKU-AE-920
```

Lexical retrieval performs particularly well on exact identifiers, whereas semantic vector retrieval is valuable for natural-language similarity.

The proposed system therefore uses:

```text
                 Query
                   │
           ┌───────┴────────┐
           │                │
           ▼                ▼
      Full-Text Search   Vector Search
           │                │
           └───────┬────────┘
                   ▼
                  RRF
                   │
                   ▼
               Reranker
                   │
                   ▼
                 Top-K
```

PostgreSQL includes native full-text search, indexing, ranking, query parsing, dictionaries, and related text-search facilities.

pgvector adds exact and approximate nearest-neighbor vector retrieval to PostgreSQL and supports HNSW and IVFFlat indexes. Its documentation explicitly describes combining pgvector with PostgreSQL full-text search and using Reciprocal Rank Fusion or a cross-encoder for hybrid retrieval.

This allows the initial system to perform both lexical and semantic retrieval without operating a separate search database.

---

# 17. Reciprocal Rank Fusion

The initial implementation should use Reciprocal Rank Fusion to combine lexical and vector results.

For document \(d\):

\[
RRF(d)=\sum_{r \in R}\frac{1}{k+\operatorname{rank}_{r}(d)}
\]

where:

- \(R\) represents retrieval systems;
- `rank` represents a document's position in a result list;
- \(k\) controls ranking decay.

RRF is attractive because BM25 relevance scores and vector-similarity scores are not naturally on the same numerical scale.

OpenSearch similarly supports RRF as a rank-based hybrid retrieval method and identifies it as a useful starting strategy when individual retrieval scores are not directly comparable.

A typical retrieval sequence may be:

```text
FTS       → Top 20
Vector    → Top 20
              ↓
           RRF merge
              ↓
         ~20 candidates
              ↓
           Reranker
              ↓
            Top 4–6
```

The exact `Top-K` parameters should be established experimentally using retrieval-quality evaluation.

---

# 18. Reranking

A semantic or cross-encoder reranker may evaluate the fused candidate set.

This creates a two-stage retrieval architecture:

```text
Cheap retrieval
      ↓
20 candidates
      ↓
More accurate reranking
      ↓
5 final chunks
```

Reranking twenty candidates is substantially less expensive than applying a powerful relevance model to the entire corpus.

If evaluation demonstrates adequate retrieval quality without reranking, the reranking stage can be bypassed for lower latency.

---

# 19. Query Construction

The RAG query should not necessarily be identical to the raw incoming message.

For example:

```text
Customer email:

"I tried what you told me yesterday, but I'm still locked out.
What do I do now?"
```

A useful retrieval query requires thread information:

```text
account locked
previous password reset unsuccessful
account recovery procedure
```

The query builder therefore uses:

```text
Current Email
+
Thread Summary
+
Classification Intent
```

to construct:

- semantic query text;
- lexical keywords;
- extracted identifiers;
- category filter;
- metadata filters.

This improves retrieval without requiring an additional general-purpose autonomous agent.

---

# 20. RAG Context Packing

Retrieval quality is not improved simply by providing more documents to the LLM.

The system should provide the **smallest sufficient context**.

An initial configuration may use approximately four to six top-ranked chunks.

The final generation context becomes:

```text
Agent Instructions
+
Category Instructions
+
Thread Summary
+
Recent Thread Messages
+
Current Email
+
Retrieved Knowledge
+
Business Data
```

Context length should be recorded for every inference request so its relationship with response quality, latency, and cost can be measured.

---

# 21. Operational Data vs RAG Knowledge

A critical architectural distinction exists between **knowledge retrieval** and **transactional data retrieval**.

Suppose a customer asks:

> “What is the current status of order 82915?”

RAG can retrieve:

> “How should order-status questions be answered?”

But the actual status of order `82915` should come from a transactional source.

Therefore:

```text
                     Context Builder
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
         RAG             CRM             ERP
          │               │               │
     Procedures       Customer         Orders
       / FAQ             Data            Data
```

For the FYP implementation, the team can implement a realistic internal business-data subsystem in PostgreSQL containing:

```text
customer
product
order
ticket
```

This demonstrates the architectural distinction without requiring access to a commercial enterprise CRM or ERP platform.

---

# 22. LLM Agent Architecture

The project should avoid a multi-agent chain in which many LLMs repeatedly process the same email.

An architecture such as:

```text
Classifier Agent
       ↓
Planner Agent
       ↓
Retrieval Agent
       ↓
Support Agent
       ↓
Critic Agent
       ↓
Writer Agent
```

would increase latency, inference cost, and operational complexity.

Instead, the system uses **agent profiles**.

```text
                 Agent Profile Registry
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
       Support         Sales          Billing
       Profile         Profile        Profile
```

Profiles may specify:

```json
{
  "profile": "technical_support",
  "knowledge_domain": "support",
  "response_style": "professional",
  "model_tier": "standard",
  "context_policy": "thread_plus_rag"
}
```

The same inference infrastructure can therefore serve multiple business functions.

For a normal email, the preferred execution pattern is approximately:

> **one retrieval operation + one response-generation operation.**

---

# 23. Model Cascading

Not all emails require the same model capability.

A cost-efficient deployment should support multiple model tiers.

```text
                     Classified Email
                            │
                     Complexity Router
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
         Normal Case                 Difficult Case
              │                           │
              ▼                           ▼
        Small/Fast LLM              Stronger LLM
```

Possible escalation criteria include:

- low classification confidence;
- complex thread;
- insufficient retrieval evidence;
- high response complexity;
- multiple requested actions;
- unusually long context.

This model-cascading approach allows the majority of traffic to use lower-cost models while reserving more capable models for difficult cases.

---

# 24. Structured LLM Output

The LLM should not return only free-form prose.

A response schema should resemble:

```json
{
  "action": "reply",
  "draft": "Dear Customer, ...",
  "confidence": 0.94,
  "knowledge_chunks": [
    "DOC-125-08",
    "DOC-772-02"
  ],
  "thread_summary_updated": false,
  "model_tier": "standard"
}
```

Structured responses simplify:

- draft persistence;
- frontend integration;
- evaluation;
- debugging;
- model comparison;
- analytics;
- downstream business workflow.

---

# 25. Processing State Machine

Every email-response job should follow an explicit state model.

```text
RECEIVED
    ↓
NORMALIZED
    ↓
CLASSIFIED
    ↓
QUEUED
    ↓
CONTEXT_READY
    ↓
GENERATING
    ↓
DRAFTED
    ↓
DISPATCHED
    ↓
COMPLETED
```

Failures generate transitions such as:

```text
GENERATING
    ↓
RETRY_PENDING
    ↓
GENERATING
```

After exceeding a retry limit:

```text
FAILED
    ↓
DEAD_LETTER
```

This enables failed operations to be inspected or replayed without corrupting the primary workload.

---

# 26. Delivery Semantics

Distributed message systems can deliver work more than once.

The processing architecture should therefore use:

> **at-least-once delivery + idempotent consumers**

rather than attempting to rely on exactly-once distributed execution.

Each logical email operation receives a deterministic idempotency key.

For example:

```text
organization_id
+
mailbox_id
+
provider_message_id
+
operation_type
```

Before executing a state-changing step, a worker checks whether the logical operation has already been successfully processed.

This prevents message redelivery from producing duplicate logical jobs.

---

# 27. Proposed Technology Stack

| Layer | Initial Implementation | Enterprise Evolution |
|---|---|---|
| API/backend | Python + FastAPI | Same / horizontally replicated |
| Email | Gmail API + Microsoft Graph | Same |
| Database | PostgreSQL | Managed/clustered PostgreSQL |
| Vector search | pgvector | pgvector or dedicated search |
| Lexical search | PostgreSQL FTS | PostgreSQL FTS / OpenSearch |
| Queue | RabbitMQ | RabbitMQ cluster/quorum queues |
| Object storage | MinIO | S3-compatible object storage |
| Cache | Redis where justified | Redis cluster |
| LLM | Provider abstraction | Multi-provider/model routing |
| Embedding | Embedding API or local model | Same / hosted inference |
| Containerization | Docker Compose | Kubernetes/ECS equivalent |
| Metrics | Prometheus | Prometheus |
| Dashboard | Grafana | Grafana |
| Tracing | OpenTelemetry | OpenTelemetry |

The deliberate architectural choice is to **avoid introducing OpenSearch during the initial FYP implementation unless PostgreSQL retrieval becomes measurably insufficient**.

---

# 28. PostgreSQL-First Retrieval Strategy

Using PostgreSQL as both the operational data platform and initial search platform provides several advantages:

- fewer distributed components;
- simpler development;
- simpler deployment;
- fewer network hops;
- lower infrastructure cost;
- easier backups;
- easier local testing;
- transactional metadata management;
- full-text search;
- vector search through pgvector.

pgvector supports HNSW indexing, which provides an approximate nearest-neighbor speed/recall trade-off suitable for increasingly large vector collections.

The initial system can therefore use:

```text
PostgreSQL
│
├── email data
├── thread state
├── operational data
├── knowledge metadata
├── PostgreSQL FTS
└── pgvector HNSW
```

rather than:

```text
PostgreSQL
+
MongoDB
+
Elasticsearch
+
Vector DB
+
Separate document database
```

The second architecture would introduce substantial operational complexity without being justified by the expected FYP workload.

---

# 29. Scaling Beyond PostgreSQL Search

The architecture nevertheless provides a migration path.

If measurements indicate that retrieval throughput, corpus size, filtering complexity, or operational requirements exceed the practical PostgreSQL configuration, the search subsystem can be extracted:

```text
Context Builder
      │
      ▼
Search Interface
      │
      ├── PostgreSQLSearchBackend
      │
      └── OpenSearchBackend
```

OpenSearch currently provides native hybrid search combining keyword and semantic retrieval, including score-based fusion and Reciprocal Rank Fusion.

Because the RAG service depends on an internal search abstraction rather than direct database calls, this migration does not require redesigning the email-processing pipeline.

---

# 30. Deployment Architecture

## 30.1 FYP Deployment

The complete reference implementation can run through Docker Compose:

```text
docker-compose
│
├── frontend
├── api
├── mail-connector
├── email-worker
├── triage-worker
├── ai-worker
├── knowledge-worker
├── postgres + pgvector
├── rabbitmq
├── minio
├── prometheus
└── grafana
```

External dependencies:

```text
Gmail / Microsoft Graph
Embedding Provider
LLM Provider
```

This architecture is realistic enough to demonstrate every major production dataflow without requiring enterprise infrastructure.

## 30.2 Scaled Deployment

The same system can evolve into:

```text
                     Load Balancer
                           │
                      API Replicas
                           │
                    RabbitMQ Cluster
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    Mail Workers     Triage Workers      AI Workers
          │                │                │
          └────────────────┼────────────────┘
                           │
                   Managed PostgreSQL
                           │
                    Object Storage
```

Stateless workers can scale independently based on queue depth.

For example:

```text
AI queue depth ↑
       ↓
increase AI workers

mail synchronization load ↑
       ↓
increase mail workers
```

The expensive component therefore scales independently from the inexpensive components.

---

# 31. Reference Enterprise Workload

To make scalability measurable, the project should evaluate a reference organization containing:

```text
10,000 connected mailboxes
100,000 incoming emails/day
```

Average traffic:

\[
\frac{100,000}{86,400}
\approx 1.16\ emails/second
\]

Average arrival rate is therefore relatively low.

Enterprise email traffic is not uniformly distributed, so a more realistic test should include bursts.

Using a hypothetical 20× burst:

\[
1.16 \times 20
\approx 23.2\ emails/second
\]

A workload of approximately **20–25 incoming messages per second** remains modest for a message broker and horizontally scaled PostgreSQL-backed workers.

This demonstrates an important property:

> Mailbox count is not equivalent to concurrent LLM count.

Ten thousand mailboxes do not require ten thousand active AI agents.

---

# 32. Workload Reduction

Assume:

```text
100,000 incoming emails/day
```

After triage:

```text
45% → no reply required
20% → deterministic/simple workflow
35% → AI-generated response
```

Only:

\[
100,000 \times 0.35
=
35,000
\]

messages reach the response-generation system.

If only 70% of those responses require external organizational knowledge:

\[
35,000 \times 0.70
=
24,500
\]

RAG retrievals are required per day.

This is significantly more efficient than executing:

```text
100,000 embeddings
100,000 RAG searches
100,000 LLM responses
```

for every message.

---

# 33. Latency Objectives

Email does not require conversational chatbot latency.

Nevertheless, the system should target:

| Processing Stage | Target Typical Latency |
|---|---:|
| Notification → ingestion | < 1 s |
| Parse + persistence | < 100 ms |
| Classification | 20–300 ms |
| Queue scheduling | < 100 ms |
| Hybrid retrieval | 50–250 ms |
| Reranking | 20–200 ms |
| Context assembly | < 50 ms |
| LLM generation | 1–5 s |
| Draft persistence | < 50 ms |
| **End-to-end typical** | **2–6 s** |
| **p95 project objective** | **< 10 s** |

These values should be treated as **project SLO targets**, not guaranteed characteristics of the underlying providers.

External LLM latency, provider API delay, message size, model choice, and queue depth affect the final measurement.

---

# 34. Cost Architecture

Embedding cost is unlikely to dominate operating expenses because documents are embedded primarily when they are added or updated.

For example, OpenAI currently lists `text-embedding-3-small` at **$0.02 per million input tokens**.

Indexing a twenty-million-token knowledge corpus would therefore represent approximately:

\[
20 \times \$0.02
=
\$0.40
\]

in embedding inference cost at that published rate.

The primary recurring AI cost is response generation.

Therefore, cost optimization should focus on:

- eliminating unnecessary responses;
- minimizing retrieved context;
- summarizing long threads;
- using cheap classifiers;
- model cascading;
- reusing static prompt prefixes where provider caching supports it;
- avoiding unnecessary multi-agent calls.

---

# 35. Illustrative Model-Cascade Cost

A production deployment should remain provider-independent, but current commercial model pricing can illustrate feasibility.

Suppose the previous reference workload produces:

```text
35,000 AI-generated responses/day
```

Assume:

```text
90% normal/simple
10% complex
```

Normal workload:

```text
31,500 emails/day
2,000 input tokens/email
200 output tokens/email
```

Using a model priced equivalently to GPT-5.4 nano, whose current published rates are $0.20/M input tokens and $1.25/M output tokens:

Input:

\[
31,500 \times 2,000
=
63,000,000
\]

\[
63 \times \$0.20
=
\$12.60/day
\]

Output:

\[
31,500 \times 200
=
6,300,000
\]

\[
6.3 \times \$1.25
=
\$7.88/day
\]

Normal-case generation:

\[
\approx \$20.48/day
\]

Complex workload:

```text
3,500 emails/day
3,000 input tokens/email
300 output tokens/email
```

Using a model priced equivalently to GPT-5.4 Mini, whose current published rates are $0.75/M input tokens and $4.50/M output tokens:

Input:

\[
10.5M \times \$0.75
=
\$7.88
\]

Output:

\[
1.05M \times \$4.50
=
\$4.73
\]

Complex generation:

\[
\approx \$12.61/day
\]

Combined illustrative generation cost:

\[
\$20.48 + \$12.61
\approx \$33.09/day
\]

or approximately:

\[
\$993/month
\]

at thirty operating days.

This example excludes infrastructure, mail-provider charges, reranking services, taxes, regional pricing differences, and other platform costs. It is intended as a **feasibility scenario rather than a vendor quotation**.

Most importantly, the cost scales with the number of emails that actually reach generation—not directly with the number of mailboxes.

---

# 36. Horizontal Scalability

Each major service can scale independently.

### Mail Connector

Scale according to:

```text
mailboxes
+
provider events
```

### Email Processor

Scale according to:

```text
incoming message rate
```

### Triage Worker

Scale according to:

```text
classification queue depth
```

### Retrieval Service

Scale according to:

```text
RAG requests per second
```

### AI Worker

Scale according to:

```text
generation queue depth
+
external inference concurrency
```

### Knowledge Worker

Scale according to:

```text
document ingestion volume
```

This prevents the most expensive service—the AI generation layer—from determining the resource requirements of the entire platform.

---

# 37. Observability

A credible enterprise-oriented FYP should treat observability as a first-class feature.

Every email should receive a processing correlation ID.

Example:

```text
trace_id: ...
message_id: ...
thread_id: ...
job_id: ...
```

OpenTelemetry can propagate the trace across:

```text
mail connector
      ↓
email processor
      ↓
triage
      ↓
queue
      ↓
retrieval
      ↓
LLM
      ↓
draft
```

Prometheus should collect metrics such as:

```text
emails_received_total
emails_classified_total
emails_generated_total

classification_latency_ms
retrieval_latency_ms
generation_latency_ms
end_to_end_latency_ms

queue_depth
queue_wait_ms

retrieval_hit_rate
retrieval_top_k

input_tokens_total
output_tokens_total
embedding_tokens_total

estimated_ai_cost
failed_jobs_total
retry_jobs_total
```

Grafana can then visualize system behavior during experiments.

This produces objective evidence that the architecture performs as claimed.

---

# 38. Evaluation Methodology

The project should not be evaluated solely by showing that an email receives an AI-generated reply.

Evaluation should be divided into four independent dimensions.

## 38.1 Classification Quality

Metrics:

- accuracy;
- precision;
- recall;
- macro F1;
- confusion matrix.

Example research question:

> Can the triage subsystem correctly assign emails to business categories without using the response-generation model?

---

## 38.2 Retrieval Quality

Create a benchmark dataset in which each question/email is associated with known relevant documents.

Evaluate:

- Recall@K;
- Precision@K;
- Mean Reciprocal Rank;
- nDCG@K.

Compare:

```text
Vector only
vs
Full-text only
vs
Hybrid retrieval
vs
Hybrid + reranking
```

This provides direct experimental justification for the chosen RAG architecture.

---

## 38.3 Response Quality

Generated drafts should be evaluated for:

- factual correctness;
- relevance;
- completeness;
- consistency with retrieved evidence;
- response clarity;
- thread-context awareness;
- unnecessary information.

Where human reviewers are available, additionally measure:

- acceptance rate;
- rejection rate;
- percentage requiring edits;
- edit distance;
- reviewer rating.

An especially useful practical metric is:

> **Draft Acceptance Rate**

because it directly measures whether generated responses are usable.

---

## 38.4 System Performance

Measure:

- end-to-end latency;
- p50 latency;
- p95 latency;
- p99 latency;
- maximum sustainable throughput;
- queue depth;
- average queue delay;
- database query latency;
- retrieval latency;
- LLM latency;
- token usage;
- estimated cost/email;
- CPU utilization;
- memory utilization.

---

# 39. Proposed Experiments

A strong final evaluation should contain at least the following experimental scenarios:

| Experiment | Purpose |
|---|---|
| Rule vs ML vs LLM triage | Determine classifier cost/quality trade-off |
| Vector vs FTS vs hybrid | Validate retrieval design |
| Hybrid vs hybrid + reranker | Measure reranking value |
| Top-3 vs Top-5 vs Top-10 context | Determine optimal context size |
| Full thread vs summarized thread | Measure token/quality trade-off |
| Single model vs model cascade | Measure inference cost reduction |
| 1× / 5× / 10× / 20× load | Evaluate scalability |
| Worker failure/retry | Evaluate processing resilience |
| PostgreSQL retrieval under corpus growth | Identify migration threshold |
| End-to-end real email test | Demonstrate real-world functionality |

---

# 40. Success Criteria

The project should define measurable criteria before final evaluation.

Suggested targets are:

| Metric | Proposed Target |
|---|---:|
| Email category Macro F1 | ≥ 0.90 |
| Retrieval Recall@5 | ≥ 0.85 |
| Useful-draft acceptance | ≥ 80% |
| Typical E2E latency | ≤ 6 s |
| p95 E2E latency | ≤ 10 s |
| Duplicate logical processing | 0 |
| Recovery from temporary worker failure | successful |
| Horizontal throughput increase when workers added | demonstrable |
| Cost per generated email | measured and reported |
| RAG improvement over non-RAG baseline | statistically observable |

The actual final values should be determined from the project's dataset rather than artificially adjusted to satisfy these targets.

---

# 41. Implementation Phases

## Phase 1 — Core Mail Pipeline

Implement:

```text
Gmail integration
PostgreSQL schema
email synchronization
MIME processing
thread persistence
basic API
```

Deliverable:

```text
Real Gmail → internal database
```

## Phase 2 — Triage and Queue Architecture

Implement:

```text
classification
routing
RabbitMQ
worker processing
job-state machine
```

Deliverable:

```text
Email → category queue
```

## Phase 3 — Knowledge RAG

Implement:

```text
document ingestion
chunking
embeddings
pgvector
PostgreSQL FTS
hybrid retrieval
RRF
```

Deliverable:

```text
Email → relevant organizational knowledge
```

## Phase 4 — Context and Generation

Implement:

```text
thread-state construction
conversation summarization
agent profiles
LLM abstraction
structured responses
draft persistence
```

Deliverable:

```text
Email + Thread + RAG → generated draft
```

## Phase 5 — Business Data Integration

Implement:

```text
customer
product
order
ticket
```

and context adapters.

Deliverable:

```text
RAG knowledge + live operational data → response
```

## Phase 6 — Mail Dispatch

Implement provider reply/draft functionality.

Deliverable:

```text
Incoming real email
→ processing pipeline
→ generated reply
→ provider mailbox
```

## Phase 7 — Observability and Evaluation

Implement:

```text
OpenTelemetry
Prometheus
Grafana
benchmark dataset
retrieval evaluation
classification evaluation
load testing
cost analysis
```

Deliverable:

> Quantitative evidence supporting or rejecting the project's design hypotheses.

---

# 42. Expected Contributions

The proposed project contributes more than a conventional chatbot or RAG demonstration.

Its primary contribution is a complete architecture combining:

**event-driven email synchronization**

with:

**hierarchical intelligent triage**

with:

**category-aware asynchronous processing**

with:

**conversation-state management**

with:

**hybrid retrieval**

with:

**selective LLM generation**

with:

**measurable cost and performance controls.**

The architecture specifically attempts to address the gap between small-scale RAG demonstrations and systems that could realistically process enterprise communication workloads.

---

# 43. Technical Novelty of the Project

The novelty of the project should not be claimed to be the invention of RAG, email classification, vector retrieval, or LLMs independently.

Instead, the project's technical contribution lies in their **systems-level orchestration and optimization**.

In particular:

```text
                     Incoming Email
                            │
                            ▼
                     Cheap Decisions
                            │
                   "Do we need AI?"
                            │
                            ▼
                  "Do we need RAG?"
                            │
                            ▼
                  Minimum Context
                            │
                            ▼
                  Appropriate Model
                            │
                            ▼
                       Response
```

This architecture applies expensive computation only when additional intelligence creates measurable value.

That principle directly supports lower operating cost and higher throughput.

---

# 44. Feasibility Argument

The proposed system is technically feasible for three principal reasons.

First, modern email providers already expose asynchronous change-notification mechanisms. Gmail push notifications eliminate the need for constant mailbox polling, while Microsoft Graph supports corresponding webhook/change-notification and delta-query patterns.

Second, the initial retrieval architecture can be implemented using PostgreSQL alone. PostgreSQL provides mature full-text search, while pgvector adds HNSW-based vector similarity and explicitly supports hybrid retrieval patterns.

Third, LLM inference is not proportional to total mailbox count because triage eliminates non-actionable messages before generation. Model cascading further allows inexpensive models to process common workloads while reserving stronger inference for difficult cases.

Consequently, the proposed architecture does not depend on experimental infrastructure or unrealistically large hardware.

A small team can implement the reference system using containers and managed/model APIs, while the logical architecture remains compatible with horizontally replicated production services.

---

# 45. Research Hypothesis

The central project hypothesis is:

> **An event-driven, category-routed RAG email-processing architecture can automatically generate useful enterprise email responses with acceptable retrieval quality, response latency, and operating cost by selectively invoking hybrid retrieval and LLM inference only for messages that require them.**

Secondary hypotheses include:

> **H1:** Hybrid lexical-semantic retrieval provides better email-related knowledge retrieval than vector-only retrieval.

> **H2:** Cascaded email triage substantially reduces unnecessary LLM generation without materially reducing routing accuracy.

> **H3:** Conversation summarization reduces LLM token consumption for long email threads while preserving sufficient information for high-quality replies.

> **H4:** Model cascading reduces inference cost relative to a single high-capability model while maintaining acceptable response quality.

> **H5:** An asynchronous queue-based architecture can absorb workload bursts and scale processing throughput through additional stateless workers.

These hypotheses are independently testable using the proposed evaluation methodology.

---

# 46. Final Proposed Architecture

The recommended implementation is therefore:

```text
EMAIL PROVIDER
      │
      ▼
Push / Webhook
      │
      ▼
MAIL CONNECTOR
      │
      ▼
Incremental Synchronization
      │
      ▼
RABBITMQ
      │
      ▼
EMAIL PROCESSOR
      │
      ▼
POSTGRESQL
      │
      ▼
CASCADING TRIAGE
      │
      ├──────────── No Response → COMPLETE
      │
      ▼
CATEGORY QUEUE
      │
      ▼
CONTEXT BUILDER
      │
      ├──────────── THREAD STATE
      │
      ├──────────── BUSINESS DATA
      │
      └──────────── HYBRID RAG
                         │
               ┌─────────┴─────────┐
               ▼                   ▼
         PostgreSQL FTS       pgvector HNSW
               │                   │
               └─────────┬─────────┘
                         ▼
                       RRF
                         │
                         ▼
                     RERANKER
                         │
                         ▼
                       TOP-K
      │
      ▼
AGENT PROFILE
      │
      ▼
MODEL CASCADE
      │
      ▼
STRUCTURED RESPONSE
      │
      ▼
DRAFT STORE
      │
      ▼
MAIL DISPATCHER
      │
      ▼
EMAIL PROVIDER
```

Knowledge follows an independent path:

```text
COMPANY KNOWLEDGE
      │
      ▼
DOCUMENT PARSER
      │
      ▼
STRUCTURED CHUNKING
      │
      ▼
METADATA
      │
      ├───────────── FTS INDEX
      │
      └───────────── EMBEDDING
                           │
                           ▼
                  POSTGRESQL + PGVECTOR
```

---

# 47. Conclusion

This proposal presents a realistic architecture for an enterprise-oriented intelligent email-management and response system based on Retrieval-Augmented Generation.

The principal design decision is to avoid treating every incoming email as an LLM task.

Instead, the system progressively increases computational effort:

```text
receive
   ↓
understand cheaply
   ↓
route
   ↓
retrieve only when necessary
   ↓
generate only when necessary
```

Event-driven mailbox synchronization prevents unnecessary provider polling. Asynchronous queues separate incoming workload from inference capacity. Cascaded classification eliminates emails that do not require AI processing. Persistent thread state prevents long conversations from repeatedly consuming their entire history. Hybrid lexical and semantic retrieval provides both exact-identifier and conceptual search. Reranking limits expensive relevance computation to small candidate sets. Agent profiles provide specialization without introducing unnecessary multi-agent chains. Model cascading allocates inference capability according to email complexity.

PostgreSQL, PostgreSQL full-text search, pgvector, RabbitMQ, Docker, and standard LLM APIs are sufficient to implement the initial system. The architecture nevertheless provides clear migration paths toward clustered message queues, horizontally scaled workers, managed databases, and dedicated hybrid-search infrastructure.

Most importantly, the project is not intended merely to demonstrate that an LLM **can write an email**.

It is intended to demonstrate that an AI email-processing system can be engineered as a measurable, scalable and economically viable distributed system.

The success of the project can therefore be demonstrated quantitatively through classification accuracy, retrieval Recall@K, response acceptance rate, end-to-end latency, throughput, queue behavior, token consumption, operating cost, and failure recovery.

Under these criteria, the proposed architecture is sufficiently practical for implementation as a final-year project while remaining technically credible as the foundation of a system that a medium or large organization could reasonably consider deploying.

---

# References and Technical Basis

The architectural decisions in this proposal are grounded primarily in current upstream technical documentation:

1. **Google Workspace — Gmail API Push Notifications.** Gmail provides mailbox change notifications using Google Cloud Pub/Sub and recommends using the resulting mailbox history information for incremental synchronization.

2. **Google Workspace — Synchronize Clients with Gmail.** Gmail supports full synchronization and lighter-weight partial synchronization using `history.list()` and stored `historyId` values.

3. **Microsoft Graph — Outlook Change Notifications.** Microsoft Graph supports webhook subscriptions for changes to Outlook message resources.

4. **Microsoft Graph — Delta Query.** Delta queries allow applications to maintain synchronized local state by retrieving changes rather than repeatedly reading complete collections.

5. **PostgreSQL — Full Text Search.** PostgreSQL provides built-in document parsing, search queries, indexing, result ranking and text-search configuration.

6. **pgvector.** pgvector adds exact and approximate vector search to PostgreSQL, including HNSW and IVFFlat, metadata filtering, and documented hybrid-search integration with PostgreSQL full-text search.

7. **RabbitMQ — Quorum Queues.** RabbitMQ quorum queues provide replicated durable queueing using Raft and are intended for workloads requiring high availability and reliable failure behavior.

8. **OpenSearch — Hybrid Search and Reciprocal Rank Fusion.** OpenSearch supports keyword-plus-semantic hybrid retrieval and rank-based result fusion through RRF, providing an appropriate future migration path if PostgreSQL-based retrieval becomes insufficient.

9. **OpenAI — Embedding Models.** Current API documentation lists `text-embedding-3-small` at $0.02 per million input tokens, illustrating the relatively low cost of offline knowledge indexing compared with repeated generation workloads.

10. **OpenAI — Current Small-Model Pricing.** GPT-5.4 nano and GPT-5.4 Mini pricing were used solely to produce the proposal's illustrative cost model; the architecture itself is model- and provider-independent.