"""What AgentMailGuard's outcome for a job turns into (task 7.20; ADR-0011; R22.12).

``guard_outcome`` reads only the guard's decision fields from the executor's record, so it is
checked here on plain dicts: no mailguard import, no model call.
"""

from __future__ import annotations

from typing import Any

import pytest

from evaluation.mailguard_bench.live.guarded_drafting import GuardOutcome, guard_outcome


def _record(**overrides: Any) -> dict[str, Any]:
    """A guarded row's decision fields for a job the guard let through."""
    record: dict[str, Any] = {
        "blocked_inbound": False,
        "blocked_outbound": False,
        "inbound_action": "draft_only",
        "final_action": "draft_only",
        "rule": "P99-default",
    }
    return {**record, **overrides}


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        pytest.param(
            _record(
                blocked_inbound=True,
                inbound_action="quarantine",
                final_action="quarantine",
                rule="P01-critical-injection-quarantine",
            ),
            (GuardOutcome.ESCALATED, "agentmailguard:quarantine:P01-critical-injection-quarantine"),
            id="inbound quarantine",
        ),
        pytest.param(
            _record(blocked_inbound=True, inbound_action="block", final_action="block", rule="P02"),
            (GuardOutcome.ESCALATED, "agentmailguard:block:P02"),
            id="inbound block",
        ),
        pytest.param(
            _record(blocked_outbound=True, final_action="block", rule="P03-goal-compliance"),
            (GuardOutcome.ESCALATED, "agentmailguard:block:P03-goal-compliance"),
            id="outbound block takes the final decision, not the inbound one",
        ),
        pytest.param(
            _record(final_action="human_approval", rule="P07-citation-mismatch-review"),
            (
                GuardOutcome.HUMAN_APPROVAL,
                "agentmailguard:human_approval:P07-citation-mismatch-review",
            ),
            id="human approval",
        ),
        pytest.param(_record(), (GuardOutcome.DRAFTED, None), id="draft only"),
        pytest.param(
            _record(final_action="auto_send", rule="P10"),
            (GuardOutcome.DRAFTED, None),
            id="auto send",
        ),
        pytest.param(
            _record(inbound_action=None, final_action=None, rule=None),
            (GuardOutcome.DRAFTED, None),
            id="C0T: no layer decides",
        ),
        pytest.param(
            _record(blocked_inbound=True, inbound_action="block", rule=None),
            (GuardOutcome.ESCALATED, "agentmailguard:block:unknown"),
            id="a decision without a rule id is still named",
        ),
    ],
)
def test_the_outcome_and_the_reason_it_leaves_on_the_draft(
    record: dict[str, Any], expected: tuple[GuardOutcome, str | None]
) -> None:
    assert guard_outcome(record) == expected
