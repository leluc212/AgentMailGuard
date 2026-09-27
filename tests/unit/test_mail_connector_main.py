"""Composition root of the mail connector (RA.10)."""

from __future__ import annotations

from types import MethodType

from packages.core.settings import LeaseReaperSettings, MailConnectorSettings
from services.mail_connector.consumer import MailSyncConsumer
from services.mail_connector.main import build_components
from tests.stubs.worker_resources import fake_worker_resources


def test_lease_reaper_is_disabled_by_default() -> None:
    assert LeaseReaperSettings().enabled is False


async def test_build_components_without_lease_reaper() -> None:
    settings = MailConnectorSettings(_env_file=None, lease_reaper=LeaseReaperSettings())
    start_fns = await build_components(fake_worker_resources(settings))

    assert len(start_fns) == 3  # consumer, renewal loop, queue monitor
    start = start_fns[0]
    assert isinstance(start, MethodType)
    consumer = start.__self__
    assert isinstance(consumer, MailSyncConsumer)
    assert consumer.queue_name == settings.broker.queue_mail_sync
    assert consumer.prefetch_count == settings.concurrency.mail_connector_concurrency


async def test_build_components_with_lease_reaper_enabled() -> None:
    settings = MailConnectorSettings(_env_file=None, lease_reaper=LeaseReaperSettings(enabled=True))
    start_fns = await build_components(fake_worker_resources(settings))
    assert len(start_fns) == 4
