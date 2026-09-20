"""Subscription renewal job (R2.10, R1.5).

Implements scheduled push/webhook subscription renewal before expiry for all providers,
records renewal outcomes, translates errors per common taxonomy, and automatically
transitions mailboxes to 'needs_reauth' on AuthExpired to prevent spinning.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from packages.adapters.exceptions import (
    AuthExpired,
    Permanent,
    ProviderError,
    RateLimited,
    Transient,
)
from packages.adapters.protocol import MailProviderAdapter
from packages.adapters.registry import get_adapter_for_mailbox
from packages.core.settings import AppSettings
from packages.db.mailbox import MailboxStore
from packages.db.subscription import SubscriptionStore
from packages.domain.entities import Mailbox
from packages.observability.metrics import PipelineMetrics, get_metrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenewalSummary:
    """Summary metrics of a single subscription renewal cycle pass."""

    total_evaluated: int
    succeeded: int
    failed: int
    needs_reauth: int
    rate_limited: int
    skipped: int


class SubscriptionRenewalJob:
    """Provider-neutral subscription renewal scheduled job (R2.10, R1.5)."""

    def __init__(
        self,
        subscription_store: SubscriptionStore,
        mailbox_store: MailboxStore,
        adapter_resolver: Callable[[Mailbox], MailProviderAdapter] | None = None,
        metrics: PipelineMetrics | None = None,
        settings: AppSettings | None = None,
    ) -> None:
        self.subscription_store = subscription_store
        self.mailbox_store = mailbox_store
        self.adapter_resolver = adapter_resolver or get_adapter_for_mailbox
        self.metrics = metrics or get_metrics()
        self.settings = settings or AppSettings()

    async def run_once(self, now: datetime | None = None) -> RenewalSummary:
        """Run a single evaluation and renewal pass for expiring subscriptions (R2.10).

        1. Finds subscriptions expiring on or before now() + renewal_threshold_hours.
        2. Validates corresponding mailbox operational status.
        3. Skips mailboxes in 'needs_reauth' status to prevent spinning.
        4. Calls provider adapter renew_subscription().
        5. Records success outcome with updated expiration timestamp.
        6. On AuthExpired: updates mailbox status to 'needs_reauth', logs error,
           records outcome, and halts renewal for that mailbox without spinning (R1.5, R2.10).
        7. On RateLimited or Transient/Permanent errors: records outcome with error details.
        """
        current_time = now or datetime.now(UTC)
        threshold_hours = self.settings.subscription_renewal.renewal_threshold_hours
        batch_size = self.settings.subscription_renewal.batch_size
        cutoff = current_time + timedelta(hours=threshold_hours)

        expiring_subs = await self.subscription_store.list_expiring(before=cutoff, limit=batch_size)

        succeeded = 0
        failed = 0
        needs_reauth = 0
        rate_limited = 0
        skipped = 0

        for sub in expiring_subs:
            # 1. Fetch mailbox to verify state
            mailbox = await self.mailbox_store.get(sub.mailbox_id)
            if mailbox is None:
                logger.warning(
                    "Mailbox %s for subscription %s not found; skipping",
                    sub.mailbox_id,
                    sub.subscription_id,
                )
                skipped += 1
                continue

            # If mailbox is already marked needs_reauth, do not spin or retry auth!
            if mailbox.status == "needs_reauth":
                logger.debug(
                    "Mailbox %s is in 'needs_reauth' status; skipping renewal without spinning",
                    mailbox.id,
                )
                skipped += 1
                continue

            # 2. Resolve provider adapter via neutral resolver (GEMINI.md §4)
            adapter = self.adapter_resolver(mailbox)

            # 3. Attempt subscription renewal
            try:
                renewed_sub = await adapter.renew_subscription(sub)
                await self.subscription_store.record_renewal_outcome(
                    subscription_id=sub.subscription_id,
                    status="success",
                    new_expires_at=renewed_sub.expires_at,
                    error=None,
                    organization_id=sub.organization_id,
                )
                self.metrics.subscription_renewals_total.labels(
                    provider=sub.provider, status="success"
                ).inc()
                succeeded += 1
                logger.info(
                    "Successfully renewed subscription %s for mailbox %s (new expiry: %s)",
                    sub.subscription_id,
                    sub.mailbox_id,
                    renewed_sub.expires_at,
                )

            except AuthExpired as exc:
                # Mark mailbox status to needs_reauth and halt without spinning (R1.5, R2.10)
                logger.error(
                    "AuthExpired during subscription renewal for mailbox %s: %s",
                    mailbox.id,
                    exc,
                )
                await self.mailbox_store.update_status(mailbox.id, "needs_reauth")
                await self.subscription_store.record_renewal_outcome(
                    subscription_id=sub.subscription_id,
                    status="needs_reauth",
                    error=str(exc),
                    organization_id=sub.organization_id,
                )
                self.metrics.subscription_renewals_total.labels(
                    provider=sub.provider, status="needs_reauth"
                ).inc()
                needs_reauth += 1

            except RateLimited as exc:
                logger.warning(
                    "RateLimited during subscription renewal for mailbox %s (retry_after=%s): %s",
                    mailbox.id,
                    exc.retry_after,
                    exc,
                )
                await self.subscription_store.record_renewal_outcome(
                    subscription_id=sub.subscription_id,
                    status="rate_limited",
                    error=str(exc),
                    organization_id=sub.organization_id,
                )
                self.metrics.subscription_renewals_total.labels(
                    provider=sub.provider, status="rate_limited"
                ).inc()
                rate_limited += 1

            except (Transient, Permanent, ProviderError, Exception) as exc:
                logger.error(
                    "Error renewing subscription %s for mailbox %s: %s",
                    sub.subscription_id,
                    mailbox.id,
                    exc,
                )
                await self.subscription_store.record_renewal_outcome(
                    subscription_id=sub.subscription_id,
                    status="failed",
                    error=str(exc),
                    organization_id=sub.organization_id,
                )
                self.metrics.subscription_renewals_total.labels(
                    provider=sub.provider, status="failed"
                ).inc()
                failed += 1

        return RenewalSummary(
            total_evaluated=len(expiring_subs),
            succeeded=succeeded,
            failed=failed,
            needs_reauth=needs_reauth,
            rate_limited=rate_limited,
            skipped=skipped,
        )

    async def run_loop(
        self,
        stop_event: asyncio.Event | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        """Run continuous renewal cycles on schedule until stop_event is set."""
        interval = (
            interval_seconds
            if interval_seconds is not None
            else float(self.settings.subscription_renewal.check_interval_seconds)
        )
        logger.info("Starting subscription renewal job loop with interval %.1fs", interval)

        while stop_event is None or not stop_event.is_set():
            try:
                summary = await self.run_once()
                logger.debug("Completed subscription renewal cycle: %s", summary)
            except Exception:
                logger.exception("Unexpected error in subscription renewal cycle")

            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)
