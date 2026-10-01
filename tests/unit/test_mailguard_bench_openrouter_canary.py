"""The few-cent canary that runs before an OpenRouter benchmark run (work package R4).

One strict-JSON call per pinned model, through rag-email's own pinned client: it shows that the
pinned provider serves the call, that the answer is schema-valid JSON, and captures the request
and the response for the owner to read. Every request here goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from evaluation.mailguard_bench.model_profiles import get_profile, profile_env
from evaluation.mailguard_bench.openrouter_canary import (
    CANARY_SCHEMA,
    main,
    run_canary,
)
from packages.core.settings import AppSettings, LLMTiersSettings

KEY = "sk-or-canary-secret"


def llm_for(profile_name: str) -> LLMTiersSettings:
    env = profile_env(get_profile(profile_name), {"BENCH_OPENROUTER_API_KEY": KEY})
    with patch.dict(os.environ, env):
        return AppSettings(_env_file=None).llm


def answer(
    model: str,
    provider: str,
    *,
    content: str = '{"answer": "pong"}',
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "id": "gen-canary-1",
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 31, "completion_tokens": 6, "cost": 0.0000082},
        "openrouter_metadata": {
            "summary": f"available=1, selected={provider}",
            "attempt": attempt,
            "endpoints": {"available": [{"provider": provider, "selected": True}]},
        },
    }


def transport(body: dict[str, Any], status: int = 200) -> tuple[httpx.MockTransport, list[Any]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body, headers={"X-Generation-Id": "gen-header"})

    return httpx.MockTransport(handler), seen


@pytest.mark.parametrize(
    ("profile", "model", "provider"),
    [
        ("llama-3.1-8b-openrouter", "meta-llama/llama-3.1-8b-instruct", "CoreWeave"),
        ("qwen2.5-7b-openrouter", "qwen/qwen-2.5-7b-instruct", "Phala"),
    ],
)
async def test_a_call_served_by_the_pinned_provider_passes_and_is_captured(
    tmp_path: Path, profile: str, model: str, provider: str
) -> None:
    mock, seen = transport(answer(model, provider))

    report = await run_canary(
        get_profile(profile), llm_for(profile), transport=mock, out_dir=tmp_path
    )

    assert report.ok, report
    assert report.served_provider == provider
    assert report.checks == {"strict_json": True, "provider_match": True, "captured": True}
    assert len(seen) == 1  # one call: a few cents at most
    sent = json.loads(seen[0].content)
    assert sent["provider"] == get_profile(profile).routing
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["response_format"]["json_schema"]["schema"] == CANARY_SCHEMA
    assert seen[0].headers["x-openrouter-metadata"] == "enabled"

    captured = json.loads((tmp_path / f"{profile}.json").read_text(encoding="utf-8"))
    assert captured["profile"] == profile
    assert captured["request"]["body"]["provider"] == get_profile(profile).routing
    assert captured["response"]["body"]["openrouter_metadata"]["attempt"] == 1
    assert captured["provenance"]["served_provider"] == provider
    assert captured["provenance"]["generation_id"] == "gen-canary-1"
    assert captured["checks"] == report.checks


async def test_the_canary_names_the_field_that_named_the_provider(tmp_path: Path) -> None:
    model = "meta-llama/llama-3.1-8b-instruct"
    body = answer(model, "CoreWeave")
    del body["openrouter_metadata"]
    body["provider"] = "CoreWeave"
    mock, _ = transport(body)

    report = await run_canary(
        get_profile("llama-3.1-8b-openrouter"),
        llm_for("llama-3.1-8b-openrouter"),
        transport=mock,
        out_dir=tmp_path,
    )

    assert report.ok, report
    assert report.provider_source == "response.provider"
    captured = json.loads((tmp_path / "llama-3.1-8b-openrouter.json").read_text(encoding="utf-8"))
    assert captured["provenance"]["provider_source"] == "response.provider"


async def test_the_capture_never_holds_the_key(tmp_path: Path) -> None:
    mock, _ = transport(answer("qwen/qwen-2.5-7b-instruct", "Phala"))
    await run_canary(
        get_profile("qwen2.5-7b-openrouter"),
        llm_for("qwen2.5-7b-openrouter"),
        transport=mock,
        out_dir=tmp_path,
    )

    text = (tmp_path / "qwen2.5-7b-openrouter.json").read_text(encoding="utf-8")
    assert KEY not in text
    assert "authorization" not in text.lower()


async def test_another_provider_fails_the_canary_and_says_who_served_it(tmp_path: Path) -> None:
    model = "meta-llama/llama-3.1-8b-instruct"
    mock, _ = transport(answer(model, "DeepInfra"))

    report = await run_canary(
        get_profile("llama-3.1-8b-openrouter"),
        llm_for("llama-3.1-8b-openrouter"),
        transport=mock,
        out_dir=tmp_path,
    )

    assert not report.ok
    assert report.checks["provider_match"] is False
    assert report.served_provider == "DeepInfra"
    assert "provider_mismatch" in report.problems[0]
    assert (tmp_path / "llama-3.1-8b-openrouter.json").exists()  # the evidence is kept


async def test_a_strict_json_miss_on_the_pinned_provider_is_a_warning_and_the_run_may_start(
    tmp_path: Path,
) -> None:
    """Owner decision 2026-10-01 (ADR-0014): run anyway and record it with the results."""
    model = "qwen/qwen-2.5-7b-instruct"
    for content in ("Sure! pong", '{"answer": 7}', '{"answer": "pong", "extra": 1}', "[]"):
        mock, _ = transport(answer(model, "Phala", content=content))
        report = await run_canary(
            get_profile("qwen2.5-7b-openrouter"),
            llm_for("qwen2.5-7b-openrouter"),
            transport=mock,
            out_dir=tmp_path,
        )
        assert report.ok, content
        assert report.checks == {"strict_json": False, "provider_match": True, "captured": True}
        assert report.problems == [], content
        assert len(report.warnings) == 1, content
        captured = json.loads((tmp_path / "qwen2.5-7b-openrouter.json").read_text(encoding="utf-8"))
        assert captured["warnings"] == report.warnings  # recorded with the capture


async def test_a_strict_json_miss_with_another_provider_still_fails(tmp_path: Path) -> None:
    mock, _ = transport(answer("meta-llama/llama-3.1-8b-instruct", "DeepInfra", content="pong"))

    report = await run_canary(
        get_profile("llama-3.1-8b-openrouter"),
        llm_for("llama-3.1-8b-openrouter"),
        transport=mock,
        out_dir=tmp_path,
    )

    assert not report.ok
    assert report.checks["provider_match"] is False


def test_the_command_exits_0_and_says_warn_on_a_strict_json_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from evaluation.mailguard_bench import openrouter_canary
    from evaluation.mailguard_bench.openrouter_canary import CanaryReport

    async def fake_run(profile: Any, llm: Any, *, out_dir: Path, **_: Any) -> CanaryReport:
        return CanaryReport(
            profile=profile.name,
            model=profile.model,
            checks={"strict_json": False, "provider_match": True, "captured": True},
            warnings=["the answer is not valid for the strict schema: pong"],
            served_provider="Phala",
        )

    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setattr(openrouter_canary, "run_canary", fake_run)

    # main() writes the profile's LLM__* settings into os.environ; patch.dict restores it all.
    with patch.dict(os.environ, {"BENCH_OPENROUTER_API_KEY": KEY}):
        assert main(["--model-profile", "qwen2.5-7b-openrouter", "--out", str(tmp_path)]) == 0

    out = capsys.readouterr()
    assert "WARN strict_json" in out.out
    assert "ok   provider_match" in out.out
    assert "start the run anyway" in out.err
    assert KEY not in out.out + out.err


@pytest.mark.parametrize(
    ("status", "body", "hint"),
    [
        (402, {"error": {"metadata": {"limit_source": "openrouter_credits"}}}, "credit"),
        (404, {"error": {"message": "No endpoints found"}}, "no provider"),
        (503, {"error": {"message": "no provider meets routing requirements"}}, "no provider"),
    ],
)
async def test_an_http_error_fails_with_a_hint_and_still_captures(
    tmp_path: Path, status: int, body: dict[str, Any], hint: str
) -> None:
    mock, _ = transport(body, status)

    report = await run_canary(
        get_profile("llama-3.1-8b-openrouter"),
        llm_for("llama-3.1-8b-openrouter"),
        transport=mock,
        out_dir=tmp_path,
    )

    assert not report.ok
    assert any(hint in problem.lower() for problem in report.problems), report.problems
    captured = json.loads((tmp_path / "llama-3.1-8b-openrouter.json").read_text(encoding="utf-8"))
    assert captured["response"]["status"] == status
    assert captured["checks"]["captured"] is True


def test_the_command_refuses_a_profile_that_is_not_routed() -> None:
    with pytest.raises(SystemExit):
        main(["--model-profile", "gpt-4o-mini"])


def test_the_command_names_the_missing_key_and_makes_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("BENCH_OPENROUTER_API_KEY", raising=False)

    assert main(["--model-profile", "llama-3.1-8b-openrouter", "--out", str(tmp_path)]) == 1

    assert "BENCH_OPENROUTER_API_KEY" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
