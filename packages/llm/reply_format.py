"""The reply-format rules: one source, rendered into both the native and the guarded prompt.

rag-email's own prompt (``prompts/*.v[23].j2``) ends with numbered instructions. The first two are
specific to a profile; the rest tell the model *how to answer*: cite the knowledge chunks it used
and conform to the reply JSON schema. Those rules live here. The templates render them through the
``reply_format_rules`` Jinja global (``AgentProfileRegistry``), and the benchmark's guarded prompt
puts them in the trusted system instructions (``evaluation/mailguard_bench/guarded_reply.py``), so
a model behind AgentMailGuard is told the same thing as one behind rag-email's own prompt (task
7.20, ADR-0012 2a).

The rules are static text. Nothing from an email, a thread or a knowledge chunk is ever part of
them, which is what allows them to be trusted instructions.
"""

from __future__ import annotations

REPLY_FORMAT_RULES: tuple[str, ...] = (
    "Every cited chunk must be included in `knowledge_chunks` with its exact citation ID.",
    "Output must strictly conform to the required JSON schema.",
)
"""The reply-format rules, in the order the native prompt states them."""


def render_reply_format_rules(start: int = 1) -> str:
    """The rules as numbered lines, the first one numbered ``start``.

    The native templates call it with 3 (two profile-specific instructions come first); the
    guarded instruction with 1. The module attribute is read at call time.
    """
    return "\n".join(f"{start + i}. {rule}" for i, rule in enumerate(REPLY_FORMAT_RULES))
