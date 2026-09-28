"""Dispatch value objects: per-category dispatch mode and provider draft status.

Requirements:
- R17.1: two dispatch modes, create a provider draft or send the reply.
- R16.8, R17.6: create_draft is the default posture; send_reply needs an explicit approval.
- design.md §5.8 step 4, ADR-0009: the provider draft's status after an ambiguous send.
- GEMINI.md: packages/domain imports standard library and packages/core ONLY.
"""

from __future__ import annotations

from enum import StrEnum


class DispatchMode(StrEnum):
    """How an approved draft leaves the system (R17.1)."""

    CREATE_DRAFT = "create_draft"
    SEND_REPLY = "send_reply"


class ProviderDraftStatus(StrEnum):
    """What the provider reports for a stored provider draft id (design.md §5.8 step 4).

    DRAFT: still an unsent draft. SENT: sent under the same id (Graph immutable ids).
    MISSING: gone — sent under a new id (Gmail drafts.send) or deleted by a person.
    """

    DRAFT = "DRAFT"
    SENT = "SENT"
    MISSING = "MISSING"
