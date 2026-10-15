"""Check that the pinned AgentMailGuard worktree is installed and wired (task 7.19; ADR-0010).

    make mailguard-smoke     # no network, no model call
    make mailguard-probe [MODEL=<profile>]  # owner-run, not CI: ONE live guard-judge call
                                            # (default: the Gemma test model, Gemini API)

Run as a module from the rag-email root, never as a scripts/ file: AgentMailGuard also ships
top-level `services` and `evaluation` packages, and only the cwd-first sys.path of
`python -m` keeps rag-email's in front (check 2). The Make targets set MAILGUARD_DIR,
MAILGUARD_COMMIT and MAILGUARD_ARTIFACTS. The API key is the model profile's (MODEL=), or else
rag-email's LLM__OPENAI_API_KEY read through AppSettings; it is never printed.

Checks, stopping at the first failure:
  1. MAILGUARD_DIR is a clean worktree at MAILGUARD_COMMIT.
  2. `services`/`evaluation` resolve to rag-email, `mailguard` to the worktree.
  3. The L1 classifier artifact exists (sha256 and scikit-learn version printed).
  4. Offline: preset C0 inspects an email with no guard verdicts; preset C3 on the built-in
     "fake" guard model reaches an Inbound Guard verdict and an Inbound Gate decision.
  5. The Gemini guard model is registered: C3 built on it has the classifier loaded and an
     OpenAIProvider for that model on the Inbound Guard's judge and extractor (no call is made).
     With --doc-llm / --output-llm (the C3 of the live v3 benchmark, plan_v3 Task 12) the same
     holds for the Doc Scanner's chunk and set judges and the Output Guard's judge, and without
     them those judges must be off.
  6. --live-probe only: one judge call with a system message and a JSON schema returns the
     requested JSON (verifies system role + json_mode on the Gemini endpoint). On an OpenRouter
     profile it also checks that the pinned provider served the call and prints it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import sklearn
from mailguard.llm.protocol import ChatMessage

from evaluation.mailguard_bench.guard_env import (
    DEFAULT_GUARD_MODEL,
    REPO_ROOT,
    GuardEnvError,
    GuardPaths,
    checkout_line,
    guard_paths_from_env,
    guard_provider_env,
    l1_artifact,
    require_module_origins,
    require_pinned_worktree,
)
from evaluation.mailguard_bench.guard_factory import (
    LiveLayers,
    build_guard_pipeline,
    guard_settings,
    live_layers,
    require_live,
)
from evaluation.mailguard_bench.model_profiles import PROFILES, resolve_profile, with_dot_env
from evaluation.mailguard_bench.route import expected_guard_route, guard_pin_problem, pin_problem
from packages.core.settings import AppSettings

SMOKE_EMAIL: dict[str, Any] = {
    "message_id": "mailguard-smoke-1",
    "sender_email": "smoke@example.invalid",
    "sender_name": "Smoke Test",
    "subject": "Order question",
    "body_text": "Hello, could you tell me where my order ORD-1 is? Thanks.",
}
PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


async def offline_checks(paths: GuardPaths) -> None:
    """Check 4: C0 runs no layer; C3 on the fake guard model reaches an L5 decision."""
    c0 = build_guard_pipeline("C0", guard_settings("fake", l1_model_path=paths.l1_model))
    r0 = await c0.inspect_inbound(SMOKE_EMAIL, category="support")
    if r0.inbound_guard is not None or r0.inbound_decision is not None:
        raise GuardEnvError("preset C0 produced guard verdicts; C0 must run no layer")
    c3 = build_guard_pipeline("C3", guard_settings("fake", l1_model_path=paths.l1_model))
    r3 = await c3.inspect_inbound(SMOKE_EMAIL, category="support")
    if r3.inbound_guard is None or r3.inbound_decision is None:
        raise GuardEnvError(
            "preset C3 did not reach an inbound_guard verdict and an inbound gate decision"
        )
    print(f"ok offline C0 (no verdicts) / C3 (inbound action={r3.inbound_decision.action})")


def check_registered(
    paths: GuardPaths, model_name: str, *, doc_llm: bool = False, output_llm: bool = False
) -> LiveLayers:
    """Check 5: the guard model is registered on every judge the run expects live.

    Builds C3 on ``model_name`` and fails unless the classifier is loaded, the Inbound Guard's
    judge and extractor run the model, and the Doc Scanner's and Output Guard's judges are on
    exactly when asked for. No model call is made.

    Raises:
        GuardEnvError: If a stage is not what ``require_live`` expects.
    """
    pipeline = build_guard_pipeline(
        "C3",
        guard_settings(
            model_name, l1_model_path=paths.l1_model, doc_llm=doc_llm, output_llm=output_llm
        ),
    )
    layers = live_layers(pipeline)
    require_live(layers, model_name=model_name, doc_llm=doc_llm, output_llm=output_llm)
    return layers


async def live_probe(
    paths: GuardPaths, model_name: str, expected_route: Mapping[str, Any] | None = None
) -> None:
    """Check 6: one real judge call on the model's endpoint.

    ``expected_route`` is the OpenRouter pin of the run (None: not routed): the guard's provider
    must send it, or nothing is called.
    """
    pipeline = build_guard_pipeline("C3", guard_settings(model_name, l1_model_path=paths.l1_model))
    judge = pipeline.inbound_guard.judge
    unpinned = guard_pin_problem(judge, expected_route)
    if unpinned is not None:
        raise GuardEnvError(unpinned)
    try:
        result = await judge.generate(
            messages=[
                ChatMessage(role="system", content="You answer only with a JSON object."),
                ChatMessage(role="user", content='Return exactly {"ok": true}.'),
            ],
            schema=PROBE_SCHEMA,
            max_tokens=64,
        )
    finally:
        await judge.aclose()
    if "ok" not in result.content:
        raise GuardEnvError(
            f"probe reply is not the requested JSON (keys={sorted(result.content)}); "
            "try json_mode: none in guard_models.yaml"
        )
    # An OpenRouter route pins one provider: the guard's provider only reports who served the call
    # (a stage that raised would just fall back), so the pin is checked here, as in a real run.
    routing = getattr(judge, "provider_routing", None)
    provenance = getattr(result, "provenance", None)
    problem = pin_problem(routing, provenance.to_dict() if provenance is not None else None)
    if problem is not None:
        raise GuardEnvError(f"provider_mismatch: guard judge call {problem}")
    served = f" served_by={provenance.served_provider}" if routing and provenance else ""
    print(f"ok live probe model={result.model} latency_ms={result.latency_ms}{served}")


def run(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="AgentMailGuard install and wiring check.")
    ap.add_argument("--model", default=DEFAULT_GUARD_MODEL)
    ap.add_argument("--live-probe", action="store_true", help="make ONE real guard-judge call")
    ap.add_argument("--model-profile", choices=sorted(PROFILES), default=None)
    ap.add_argument(
        "--doc-llm",
        action="store_true",
        help="expect the Doc Scanner's chunk and set judges live (live v3 C3)",
    )
    ap.add_argument(
        "--output-llm",
        action="store_true",
        help="expect the Output Guard's judge live (live v3 C3)",
    )
    args = ap.parse_args(argv)
    updates, args.model = resolve_profile(args.model_profile, with_dot_env(os.environ), args.model)
    os.environ.update(updates)  # before AppSettings reads the env

    paths = guard_paths_from_env(os.environ)
    info = require_pinned_worktree(paths.root, paths.commit)
    print(checkout_line(info))
    origins = require_module_origins(REPO_ROOT, paths.root)
    print(f"ok imports services/evaluation from rag-email, mailguard from {origins['mailguard']}")
    model_path, digest = l1_artifact(paths)
    print(f"ok L1 artifact {model_path} sha256={digest} scikit-learn={sklearn.__version__}")
    asyncio.run(offline_checks(paths))

    llm = AppSettings().llm
    os.environ.update(guard_provider_env(llm.openai_base_url, llm.openai_api_key))
    layers = check_registered(paths, args.model, doc_llm=args.doc_llm, output_llm=args.output_llm)
    stages = (
        f" doc_judge={layers.doc_judge} doc_set_judge={layers.doc_set_judge}"
        f" output_judge={layers.output_judge}"
        if args.doc_llm or args.output_llm
        else ""
    )
    print(
        f"ok guard model registered: inbound_judge={layers.inbound_judge} "
        f"inbound_extractor={layers.inbound_extractor}{stages}"
    )
    if args.live_probe:
        asyncio.run(live_probe(paths, args.model, expected_guard_route(llm)))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(argv)
    except GuardEnvError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
