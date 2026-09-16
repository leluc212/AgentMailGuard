# Evaluation & Benchmark Datasets (`v1.0.0-seed`)

Requirements: **R22.1**, **R22.2**, **R5.9**, **R6.4**, **specs/design.md §11**

---

## 1. Overview & Architectural Dependency

The evaluation subsystem provides versioned, schema-validated ground-truth datasets used to benchmark triage classification, hybrid search retrieval, and draft generation.

> **Critical Dependency Rule (Task 0.13):**
> These seed benchmark datasets **must exist before Phase 2**.
> - Task 2.3 trains the lightweight ML classifier on `train.jsonl` and evaluates held-out macro-F1 on `test.jsonl`.
> - The Phase 3 gate evaluates vector vs lexical vs hybrid retrieval against `queries.jsonl`.
> - Deferring dataset construction to Phase 7 creates a circular dependency where early subsystem gates cannot be measured. Phase 7 (Task 7.5) expands and re-annotates these seed datasets; it does not create them.

---

## 2. Directory Layout

```
evaluation/
├── README.md                                    # This document
├── datasets/
│   ├── schemas.py                              # Pydantic V2 validation models
│   ├── loader.py                               # Type-safe dataset loaders
│   ├── generate_classification.py              # Deterministic classification generator
│   ├── generate_retrieval.py                   # Deterministic retrieval generator
│   ├── classification/
│   │   ├── train.jsonl                         # 252 labelled training emails (80%)
│   │   └── test.jsonl                          # 64 labelled held-out test emails (20%)
│   └── retrieval/
│       └── queries.jsonl                       # 109 search queries with gold chunk IDs
├── experiments/                                # Experiment runners (R22)
└── results/                                    # Versioned run manifests and metrics
```

---

## 3. Classification Seed Dataset (`classification/`)

### A. Specifications
- **Total Examples:** 316 labelled emails.
- **Split:** Frozen 80/20 stratified split:
  - `train.jsonl`: 252 examples
  - `test.jsonl`: 64 examples
- **Schema:** Validated against `ClassificationDatasetItem` (Pydantic V2):
  - `id`: Unique deterministic identifier (e.g. `cls_support_0001`)
  - `subject`: Email subject line
  - `body`: Plaintext email body
  - `sender`: Sender address
  - `headers`: Dictionary containing RFC 822 / MIME headers
  - `gold_category`: Ground truth category from R6.4
  - `gold_intent`: Fine-grained operational intent
  - `gold_reply_required`: Boolean flag (`true` or `false`)
  - `gold_workflow_hint`: Routing hint (`ai_generate`, `template`, `none`)
  - `metadata`: Template and split metadata

### B. Category Distribution (R6.4)
All 9 mandatory categories are covered in both `train.jsonl` and `test.jsonl`:

| Category | Description | Train | Test | Total | Workflow Hint | Reply Required |
|---|---|---|---|---|---|---|
| `support` | Technical bugs, crashes, hardware sensor drift | 36 | 9 | 45 | `ai_generate` | `true` |
| `sales` | Volume quotes, pricing queries, demo requests | 28 | 7 | 35 | `ai_generate` | `true` |
| `billing` | Invoice disputes (INV-), payments, tax exemption | 32 | 8 | 40 | `ai_generate` / `template` | `true` |
| `administration` | Password resets, user roles, audit export | 24 | 6 | 30 | `template` | `true` |
| `scheduling` | Meeting requests, demo booking, rescheduling | 24 | 6 | 30 | `ai_generate` / `template` | `true` |
| `general_inquiry`| Office hours, company address, public FAQs | 28 | 7 | 35 | `ai_generate` | `true` |
| `automated_notification` | CI/CD alerts, Datadog notifications, newsletters | 28 | 7 | 35 | `none` | `false` |
| `acknowledgement` | Order confirmations, ticket receipt acks | 28 | 7 | 35 | `none` | `false` |
| `no_response` | Out-of-office autoreplies, vacation notices, bounces | 24 | 7 | 31 | `none` | `false` |
| **Total** | | **252** | **64** | **316** | | |

### C. Rule Trigger Coverage (Task 2.2)
Includes realistic header and pattern-based rule triggers:
- `List-Unsubscribe: <https://...>` (Marketing newsletters)
- `Auto-Submitted: auto-replied` and `X-Auto-Response-Suppress: All` (Out-of-office replies)
- `Precedence: bulk` (Mailing lists)
- Sender patterns: `no-reply@...`, `alerts@...`, `notifications@...`, `mailer-daemon@...`

---

## 4. Retrieval Seed Dataset (`retrieval/queries.jsonl`)

### A. Specifications
- **Total Queries:** 109 queries.
- **Split Ratio:** ~50% Natural-Language vs ~50% Identifier-Bearing:
  - `natural_language`: 54 queries
  - `identifier_bearing`: 55 queries
- **Target Knowledge Corpus:** Maps to deterministic chunk UUIDs in `packages/db/fixtures/knowledge.py` across 3 tenants (`DEMO_ORG_ID`, `BETA_ORG_ID`, `GAMMA_ORG_ID`).

### B. Purpose of the Deliberate Split (Hypothesis H1)
The 50/50 split provides experimental signal to contrast retrieval techniques:
1. **Natural-Language Semantic Queries (54 queries):**
   - E.g., *"What is the return window for software subscriptions?"*, *"How quickly does Tier-2 respond to critical outages?"*
   - Vector search (dense embeddings) excels; pure keyword search struggles with synonyms.
2. **Identifier-Bearing Lexical Queries (55 queries):**
   - E.g., *"INV-2026-8891 duplicate billing adjustment"*, *"Tracking courier number for order ORD-9901"*, *"SKU-WIDGET-01 price and operating temperature"*, *"Tier-2 escalation for ticket TICK-4402"*.
   - Lexical search (PostgreSQL GIN tsvector) excels on exact tokens; dense embeddings can experience semantic drift.
3. **Hybrid RRF Fusion:**
   - Proves that combining both branches via Reciprocal Rank Fusion (RRF) delivers superior Mean Reciprocal Rank (MRR) and nDCG@K compared to either single branch.

---

## 5. Python Loader API

```python
from evaluation.datasets import (
    ClassificationCategory,
    load_classification_dataset,
    load_retrieval_dataset,
)

# Load training emails for ML classifier (Task 2.3)
train_set = load_classification_dataset(split="train")
print(f"Loaded {len(train_set)} training examples")

# Load held-out test emails for evaluation
test_set = load_classification_dataset(split="test")
print(f"Loaded {len(test_set)} test examples")

# Load retrieval queries for search benchmark (H1)
queries = load_retrieval_dataset()
print(f"Loaded {len(queries)} retrieval benchmark queries")
```
