"""Template rendering of the Guard Playground (task 4.2).

Requirements: 3.1, 3.2, 3.9 (Replay default, C0 vs C3, 9 presets in order), 4.5, 4.6, 5.1-5.6
(column order, draft statement), 12.1 (banner first), 13.8 (7 stages), 14.2-14.4 (evidence,
escaped excerpt), 14.10 (quarantined chunk label).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader

import services.guard_playground as pg
from services.guard_playground.core.policy_text import PolicyText
from services.guard_playground.core.schema import LAYER_ORDER, PRESETS, RunResult
from services.guard_playground.core.view_model import NO_DRAFT, ColumnView, build_column
from tests.unit.guard_playground_support import (  # noqa: F401  (pytest fixtures)
    asgi_client,
    fixture_block_network,
    fixture_guard_import_isolation,
    fixture_no_credentials,
    fixture_playground_app_factory,
)

pytestmark = pytest.mark.usefixtures("block_network", "no_credentials")

TEMPLATES_DIR = Path(pg.__file__).resolve().parent / "templates"
SCRIPT = "<script>x</script>"
POLICY_YAML = """\
version: test
rules:
  - id: P02-exfiltration-or-tool-abuse-block
    reason: High-severity exfiltration.
"""


def _row(layer: str, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "layer": layer,
        "ran": True,
        "not_run_reason": None,
        "severity": "none",
        "score": 0.0,
        "decided_by": "classifier",
        "latency_ms": 12,
        "findings": [],
        "error": None,
        "fallback_reason": None,
        "judge_configured": True,
    }
    row.update(kw)
    return row


def _blocked_at_pass_one() -> RunResult:
    """C3 run blocked at gate pass 1; L1 found a ``<script>`` excerpt; one chunk quarantined."""
    finding = {
        "detector": "classifier",
        "rule_id": "R12",
        "score": 0.93,
        "technique": "html_injection",
        "excerpt": SCRIPT,
    }
    layers = {
        "inbound_guard": {"severity": "high", "score": 0.93, "findings": [finding]},
        "doc_scanner": {"severity": "medium", "score": 0.8},
    }
    return RunResult.model_validate(
        {
            "generated_at": datetime(2026, 10, 26, 14, 5, tzinfo=UTC),
            "mode": "replay",
            "source": "pinned",
            "case_id": "case-1",
            "preset": "C3",
            "draft_model": "gpt-4o-mini",
            "guard_models": {},
            "email": {
                "subject": "Hello",
                "body": f"Please refund. {SCRIPT} Thanks.",
                "chunks": [{"chunk_id": "kb-1", "content": "Refunds take 5 days."}],
                "source": "pinned",
                "case_id": "case-1",
            },
            "layers": [_row(name, **layers.get(name, {})) for name in LAYER_ORDER],
            "user_intent": None,
            "stripped": [],
            "removed_ratio": 0.0,
            "chunks": [
                {
                    "chunk_id": "kb-1",
                    "content": "Refunds take 5 days.",
                    "score": 0.8,
                    "quarantined": True,
                    "reason_rule_id": "D03",
                    "reason_technique": "instruction",
                }
            ],
            "quarantine_ratio": 1.0,
            "inbound": {
                "action": "block",
                "matched_rule_id": "P02-exfiltration-or-tool-abuse-block",
                "risk_tier": "high",
                "requires_human": True,
            },
            "outbound": None,
            "gate_passes": 1,
            "isolation_applied": False,
            "draft_generated": False,
            "draft_body": None,
            "guarded_prompt": None,
            "thresholds": {"block": 0.85, "flag": 0.5, "strip": 0.5, "quarantine": 0.7},
        }
    )


@pytest.fixture(name="column")
def fixture_column(tmp_path: Path) -> ColumnView:
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML, encoding="utf-8")
    return build_column(_blocked_at_pass_one(), PolicyText(policy_path), id_prefix="a")


def _render_column(column: Any) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
    return env.get_template("guard/_column.html").render(column=column)


def test_column_renders_in_order_with_escaped_evidence(column: ColumnView) -> None:
    html = _render_column(column)
    positions = [html.index(f'data-part="{part}"') for part in ("banner", "stepper", "decisions")]
    positions.append(html.index('data-part="layers"'))
    assert positions == sorted(positions)
    assert html.index("BLOCKED") < positions[1]  # banner text comes before everything else
    assert len(re.findall(r'<li class="stage stage--', html)) == 7
    assert "stopped here" in html
    assert "QUARANTINED" in html and "chunk--quarantined" in html
    assert SCRIPT not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert re.search(r'<details id="l-a-inbound_guard" class="layer" tabindex="-1" open>', html)
    assert not re.search(r'<details id="l-a-doc_scanner"[^>]* open>', html)
    assert NO_DRAFT in html
    assert 'href="#l-a-inbound_guard"' in html  # stepper stage links to its layer section
    assert "↑ evidence (1)" in html


def test_run_status_inside_the_run_form_targets_itself() -> None:
    """Regression (browser check 2026-10-27): #run-status sits inside #run-form, whose
    hx-target is #results; without its own hx-target the toolbar refresh replaced the whole
    result area and the next Run had no #results to swap into."""
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
    html = env.get_template("guard/_run_status.html").render(guard_available=True)
    assert 'id="run-status"' in html and 'hx-target="this"' in html


def test_error_column_renders_alert() -> None:
    html = _render_column({"error": "connection error", "preset": "C3"})
    assert 'class="column column--error" role="alert"' in html
    assert "Run failed: C3 — connection error" in html


async def test_guard_page_defaults(
    playground_app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    cases_path = tmp_path / "evaluation/datasets/mailguard/cases.jsonl"
    cases_path.parent.mkdir(parents=True)
    good = (
        '{"case_id": "attack-1", "kind": "attack", "source": "llmail",'
        ' "email": {"subject": "Hi", "body_text": "Body"}}'
    )
    cases_path.write_text(f"{good}\nnot json\n", encoding="utf-8")
    async with asgi_client(playground_app_factory()) as client:
        response = await client.get("/guard")
    assert response.status_code == 200
    html = response.text
    assert re.search(r'name="mode" value="replay" checked', html)
    assert not re.search(r'name="mode" value="live"[^>]* checked', html)
    for select, preset in (("left_preset", "C0"), ("right_preset", "C3")):
        block = html.split(f'name="{select}"', 1)[1].split("</select>", 1)[0]
        assert re.findall(r'<option value="([^"]+)"', block) == list(PRESETS)
        assert re.findall(r'<option value="([^"]+)" selected', block) == [preset]
    assert 'id="run-form"' in html and 'hx-post="/guard/run"' in html
    assert "11 cases" in html  # 1 pinned case + the guard fixtures (5 benign + 5 attacks)
    assert "1 lines excluded from cases.jsonl" in html
    assert 'hx-get="/guard/cases/attack-1"' in html
    assert 'href="http://127.0.0.1:3001"' in html
    assert "REPLAY" in html
