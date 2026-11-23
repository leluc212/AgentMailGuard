# Demo console: step data audit (task 9.1, R25.7, R23.5)

What the demo console reads for each pipeline step, from `GET /v1/jobs/{id}/timeline` (and
`GET /v1/messages/{id}/timeline`). No new endpoint: every field is in a `processing_event`
payload written by the step itself. Step latency is the difference between consecutive events'
`created_at`.

| Step | Event (state_from → state_to / event_type) | Fields already present | Added by task 9.1 |
|---|---|---|---|
| Triage | `NORMALIZED → CLASSIFIED` | `category`, `confidence`, `decided_by` | `intent`, `priority`, `reply_required`, `workflow_hint`, `retrieval_required`, `triage_latency_ms`, `triage_model` (`triage_event_fields`) |
| Gate: early exit | `CLASSIFIED → COMPLETED` | `early_exit`, `reason`, `category`, `intent`, `confidence`, `decided_by` | `gate_action` |
| Gate: template | `CLASSIFIED → DRAFTED` | `template_reply`, `template_id`, `template_version`, `draft_id`, classification | `gate_action`, `reason` |
| Gate: AI | `CLASSIFIED → QUEUED` | `category`, `intent`, `priority`, `retrieval_required`, `retrieval_from_category`, `workflow_hint`, `classified_workflow_hint`, `confidence`, `decided_by` | `gate_action`, `reason` |
| Context / business data | `QUEUED → CONTEXT_READY` | `retrieval_performed`, `retrieved_chunks_count`, `thread_has_summary`, `tokens_saved`, `business_plan`, business facts | — |
| Retrieval + summarization | `context_built` | per chunk `chunk_id`, `document_id`, `rank`, `rerank_score`; `retrieval_degraded`, `retrieval_underfilled`, `retrieval_vector_error`, `rerank_applied`, `summary_triggered`, `summary_model` | per chunk `lexical_rank`, `lexical_score`, `vector_rank`, `vector_score`, `fused_score`; `thread_summary_used`, `recent_messages_count` |
| Routing | `CONTEXT_READY → GENERATING` | `category`, `escalated_tier`, `escalation_reason` | — |
| Generation | `GENERATING → DRAFTED` | `draft_id`, `model`, `model_tier`, `escalation_reason`, `prompt_version`, `input_tokens`, `output_tokens`, `cost_estimate`, `citation_mismatch` | `citations_count`, `cited_chunk_ids` |
| Dispatch | `DRAFTED → DISPATCHED → COMPLETED` | claim: `draft_id`, `dispatch_idempotency_key`, `operator_replay`; finish: `dispatch_mode`, `provider_ref`, `outbound_message_id` | — |
| Guard | — | nothing yet | out of scope here: `guard_report` + `GET /v1/jobs/{id}/guard` (task 9.7) |

Not added, on purpose:

- Triage token counts and cost: the cascade does not return them on `Classification`; adding
  them changes the triage contract and is not needed for R25.7's "cost" (generation cost is on
  `DRAFTED`, and the cost counters feed task 9.8).
- Full retrieval debug (query text, both branch lists before fusion): `POST /v1/search/debug`
  already returns it (R23.3, scenario S14).
