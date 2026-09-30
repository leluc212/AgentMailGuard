"""C0: one case through rag-email's native reply path, no AgentMailGuard code (task 7.19).

    ContextPackage ─▶ SinglePassGenerator.generate_draft ── ONE reply.v1 call
                        (rag-email's own profile template, prompts/*.v3.j2)
                          ▼
                     final draft (nothing blocks, nothing is rewritten)

Owner decision 2026-09-29 (plan, BINDING section): the benchmark compares rag-email as it
runs (C0) with rag-email wrapped by every AgentMailGuard layer (C3). This executor is C0. It
imports nothing from mailguard, renders no guard template and adds no defence logic
(ADR-0010); it only calls the same ``generate_draft`` the live worker calls and writes the
result in the row shape the guarded configs use, so Task 5 scores both the same way.
"""

from __future__ import annotations

import time
from typing import Any

from evaluation.mailguard_bench.case_adapter import PreparedCase
from evaluation.mailguard_bench.guarded_reply import CaseExecution, generation_summary
from packages.llm.generator import SinglePassGenerator

NATIVE_PROMPT_MODE = "native"


class NativeCaseExecutor:
    """rag-email's ``generate_draft`` on the prepared ContextPackage, nothing else."""

    def __init__(self, *, generator: SinglePassGenerator, max_tokens: int = 1000) -> None:
        self.generator = generator
        self.max_tokens = max_tokens

    async def execute(self, prepared: PreparedCase) -> CaseExecution:
        """Run the single generation call and describe it as an unguarded result row.

        Raises:
            LLMError / UnvalidatedDraftError: From rag-email's generation call (error row).
        """
        context = prepared.context
        started = time.perf_counter()
        generation = await self.generator.generate_draft(
            context, category=prepared.classification.category, max_tokens=self.max_tokens
        )
        generation_ms = int((time.perf_counter() - started) * 1000)
        reply_v1 = dict(generation.content)
        action = str(reply_v1.get("action", ""))
        body = str(reply_v1.get("draft", ""))
        record: dict[str, Any] = {
            "blocked_inbound": False,
            "blocked_outbound": False,
            "blocked": False,
            "inbound_action": None,
            "final_action": None,
            "rule": None,
            "decision_stage": None,
            "layers_flagged": [],
            "threat_types": [],
            "detected_layers": [],
            "l3b_quarantined": [],
            "max_severity": None,
            "prompt_mode": NATIVE_PROMPT_MODE,
            "generation": generation_summary(generation),
            "draft": {
                "action": action,
                "body_original": body,
                "body_after_guard": body,
                "redacted_by_l4": False,
            },
            "final_draft": {"action": action, "body": body},
            "system_instructions": context.agent_instructions or "",
            "guard_llm": {"model": "", "calls": 0, "input_tokens": 0, "output_tokens": 0},
            "timings_ms": {
                "guarded_total": generation_ms,
                "generation": generation_ms,
                "guard": 0,
            },
            "job_result": None,
            "report": None,
        }
        return CaseExecution(record=record, guard_errors=())
