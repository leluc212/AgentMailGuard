"""Model provider per job (task 9.4, R25.3, R25.4, R14.7, R21.4).

Both providers are built at start-up; each call picks the one its job's run options name. A job
without run options uses the configured provider. The cost counters carry the profile.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.broker.consumer import BaseConsumer
from packages.broker.envelope import JobEnvelope
from packages.core.run_context import (
    current_model_profile,
    model_profile_label,
    run_options_scope,
)
from packages.core.settings import LLMTiersSettings, ModelPricing
from packages.llm.fake import FakeLLMProvider
from packages.llm.model_profile import (
    ModelProfileUnavailableError,
    ProfileRoutedLLMProvider,
    build_profile_provider,
)
from packages.llm.protocol import ChatMessage
from packages.observability.metrics import create_pipeline_metrics, record_ai_cost

MESSAGES = [ChatMessage(role="user", content="hi")]


def _routed(*, live: bool = True) -> tuple[ProfileRoutedLLMProvider, FakeLLMProvider, Any]:
    fake = FakeLLMProvider()
    live_provider = FakeLLMProvider() if live else None
    return ProfileRoutedLLMProvider(fake=fake, live=live_provider), fake, live_provider


@pytest.mark.parametrize(
    ("run_options", "expected"),
    [
        ({"model_profile": "fake", "guard_mode": "C0"}, "fake"),
        ({"model_profile": "live", "guard_mode": "C0"}, "live"),
        (None, "live"),  # no run options: the configured (live) provider
    ],
)
async def test_each_call_goes_to_the_jobs_profile(
    run_options: dict[str, Any] | None, expected: str
) -> None:
    routed, fake, live = _routed()

    with run_options_scope(run_options):
        await routed.generate(messages=MESSAGES)

    used = {"fake": len(fake.recorded_calls), "live": len(live.recorded_calls)}
    assert used == {"fake": int(expected == "fake"), "live": int(expected == "live")}


async def test_without_a_live_provider_a_plain_job_uses_the_fake() -> None:
    routed, fake, _ = _routed(live=False)

    await routed.generate(messages=MESSAGES)

    assert routed.default_profile == "fake"
    assert len(fake.recorded_calls) == 1


async def test_live_without_a_live_provider_fails_the_call() -> None:
    routed, _, _ = _routed(live=False)

    with (
        run_options_scope({"model_profile": "live"}),
        pytest.raises(ModelProfileUnavailableError),
    ):
        await routed.generate(messages=MESSAGES)


def test_the_factory_builds_live_only_for_a_real_provider() -> None:
    assert build_profile_provider(LLMTiersSettings(provider="fake")).live is None
    built = build_profile_provider(LLMTiersSettings(provider="openai", openai_api_key="k"))
    assert built.live is not None and isinstance(built.fake, FakeLLMProvider)


async def test_concurrent_jobs_never_see_each_others_profile() -> None:
    seen: dict[str, str | None] = {}

    async def job(name: str, profile: str) -> None:
        with run_options_scope({"model_profile": profile}):
            await asyncio.sleep(0.01)
            seen[name] = current_model_profile()

    await asyncio.gather(job("a", "fake"), job("b", "live"))

    assert seen == {"a": "fake", "b": "live"}
    assert current_model_profile() is None


class _Probe(BaseConsumer):
    def __init__(self) -> None:
        super().__init__(queue_name="email.billing.normal")
        self.profiles: list[str | None] = []

    async def process_job(self, envelope: JobEnvelope, raw_message: Any) -> None:
        self.profiles.append(current_model_profile())


@pytest.mark.parametrize("profile", ["live", None])
async def test_the_consumer_applies_the_envelopes_run_options(profile: str | None) -> None:
    consumer = _Probe()
    envelope = JobEnvelope(
        idempotency_key="k",
        job_type="generate_reply",
        organization_id="00000000-0000-0000-0000-000000000001",
        run_options={"model_profile": profile, "guard_mode": "C0"} if profile else None,
    )
    message = MagicMock()
    message.ack = AsyncMock()
    message.body = envelope.to_message().body
    message.headers = {}
    message.routing_key = "email.billing.normal"

    async def no_prepare(_: JobEnvelope) -> None:
        return None

    consumer.prepare_delivery = no_prepare  # type: ignore[method-assign,assignment]
    await consumer._handle_message(message)

    assert consumer.profiles == [profile]


@pytest.mark.parametrize(
    ("run_options", "label"), [({"model_profile": "fake"}, "fake"), (None, "default")]
)
def test_cost_is_counted_under_the_profile(run_options: dict[str, Any] | None, label: str) -> None:
    metrics = create_pipeline_metrics()
    prices = {"m": ModelPricing(input_per_m=1.0, output_per_m=2.0)}

    with run_options_scope(run_options):
        assert model_profile_label() == label
        record_ai_cost("m", "fast", 1_000_000, 0, prices, metrics=metrics)

    sample = metrics.registry.get_sample_value(
        "estimated_ai_cost_total", {"model": "m", "tier": "fast", "model_profile": label}
    )
    assert sample == pytest.approx(1.0)
