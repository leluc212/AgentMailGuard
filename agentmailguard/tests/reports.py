"""Build schema-3 ``GuardReport`` objects in tests from per-stage verdicts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mailguard.contracts.policy import GuardReport
from mailguard.contracts.verdict import (
    ChunkVerdict,
    DocumentVerdict,
    InboundVerdict,
    LayerVerdict,
    OutputVerdict,
    SanitizedIntent,
)


def v3_report(
    *,
    scan: LayerVerdict | None = None,
    extract: SanitizedIntent | None = None,
    chunks: Sequence[ChunkVerdict] | None = None,
    output: OutputVerdict | None = None,
    **fields: Any,
) -> GuardReport:
    """A report the way the pipeline fills it: merged inbound verdict, aggregated documents."""
    return GuardReport(
        inbound_guard=InboundVerdict.merge(scan, extract),
        doc_scanner=DocumentVerdict.from_chunks(chunks) if chunks else None,
        output_guard=output,
        **fields,
    )
