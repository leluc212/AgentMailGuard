"""Check that the pinned AgentMailGuard worktree is installed and wired (task 7.19; ADR-0010).

    make mailguard-smoke     # no network, no model call
    make mailguard-probe     # owner-run, not CI: ONE live call to the guard judge (Gemini API)

Run as a module from the rag-email root, never as a scripts/ file: AgentMailGuard also ships
top-level `services` and `evaluation` packages, and only the cwd-first sys.path of
`python -m` keeps rag-email's in front (check 2). The Make targets set MAILGUARD_DIR,
MAILGUARD_COMMIT and MAILGUARD_ARTIFACTS. The API key is read from rag-email's .env
(LLM__OPENAI_API_KEY) through AppSettings and never printed.

Checks, stopping at the first failure:
  1. MAILGUARD_DIR is a clean worktree at MAILGUARD_COMMIT.
  2. `services`/`evaluation` resolve to rag-email, `mailguard` to the worktree.
  3. The L1 classifier artifact exists (sha256 and scikit-learn version printed).
  4. Offline: preset C0 inspects an email with no guard verdicts; preset C3 on the built-in
     "fake" guard model reaches an L1 verdict and an L5 inbound decision.
  5. The Gemini guard model is registered: C3 built on it has the classifier loaded and an
     OpenAIProvider for that model on the L1 judge and the L2 extractor (no call is made).
  6. --live-probe only: one judge call with a system message and a JSON schema returns the
     requested JSON (verifies system role + json_mode on the Gemini endpoint).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from typing import Any

import sklearn
from mailguard.llm.protocol import ChatMessage

from evaluation.mailguard_bench.guard_env import (
    DEFAULT_GUARD_MODEL,
    REPO_ROOT,
    GuardEnvError,
    GuardPaths,
    guard_paths_from_env,
    guard_provider_env,
    l1_artifact,
    require_module_origins,
    require_pinned_worktree,
)
from evaluation.mailguard_bench.guard_factory import (
    build_guard_pipeline,
    guard_settings,
    live_layers,
    require_live,
)
from evaluation.mailguard_bench.model_profiles import PROFILES, resolve_profile
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
    if r0.l1 is not None or r0.inbound_decision is not None:
        raise GuardEnvError("preset C0 produced guard verdicts; C0 must run no layer")
    c3 = build_guard_pipeline("C3", guard_settings("fake", l1_model_path=paths.l1_model))
    r3 = await c3.inspect_inbound(SMOKE_EMAIL, category="support")
    if r3.l1 is None or r3.inbound_decision is None:
        raise GuardEnvError("preset C3 did not reach an L1 verdict and an L5 inbound decision")
    print(f"ok offline C0 (no verdicts) / C3 (inbound action={r3.inbound_decision.action})")


async def live_probe(paths: GuardPaths, model_name: str) -> None:
    """Check 6: one real judge call on the Gemini endpoint."""
    pipeline = build_guard_pipeline("C3", guard_settings(model_name, l1_model_path=paths.l1_model))
    judge = pipeline.l1.judge
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
    print(f"ok live probe model={result.model} latency_ms={result.latency_ms}")


def run(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="AgentMailGuard install and wiring check.")
    ap.add_argument("--model", default=DEFAULT_GUARD_MODEL)
    ap.add_argument("--live-probe", action="store_true", help="make ONE real guard-judge call")
    ap.add_argument("--model-profile", choices=sorted(PROFILES), default=None)
    args = ap.parse_args(argv)
    updates, args.model = resolve_profile(args.model_profile, os.environ, args.model)
    os.environ.update(updates)  # before AppSettings reads the env

    paths = guard_paths_from_env(os.environ)
    info = require_pinned_worktree(paths.root, paths.commit)
    print(f"ok worktree {info.path} @ {info.commit} (clean)")
    origins = require_module_origins(REPO_ROOT, paths.root)
    print(f"ok imports services/evaluation from rag-email, mailguard from {origins['mailguard']}")
    model_path, digest = l1_artifact(paths)
    print(f"ok L1 artifact {model_path} sha256={digest} scikit-learn={sklearn.__version__}")
    asyncio.run(offline_checks(paths))

    llm = AppSettings().llm
    os.environ.update(guard_provider_env(llm.openai_base_url, llm.openai_api_key))
    pipeline = build_guard_pipeline("C3", guard_settings(args.model, l1_model_path=paths.l1_model))
    layers = live_layers(pipeline)
    require_live(layers, model_name=args.model)
    print(f"ok guard model registered: l1_judge={layers.l1_judge} l2_llm={layers.l2_llm}")
    if args.live_probe:
        asyncio.run(live_probe(paths, args.model))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(argv)
    except GuardEnvError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
