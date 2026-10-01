"""Graceful shutdown orchestration for services and message consumers (R20.8).

Coordinates the stop-consuming -> drain in-flight jobs -> close connections -> exit sequence.
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from packages.observability.health import HealthRegistry

logger = logging.getLogger(__name__)

AsyncCallback = Callable[[], Awaitable[Any]]


class GracefulShutdownCoordinator:
    """Coordinates graceful shutdown across consumers, health probes, and resources.

    Sequence:
    1. Signal received (SIGINT/SIGTERM) or trigger_shutdown() invoked.
    2. Enter drain mode: invoke drain callbacks (e.g. HealthRegistry.set_draining, consumer.stop).
    3. Wait up to `drain_timeout_s` for all in-flight jobs to complete (tracked via `track_job()`).
    4. Invoke resource cleanup callbacks (e.g. database pool close, broker close).
    5. Set shutdown completed event.
    """

    def __init__(
        self,
        drain_timeout_s: float = 15.0,
        health_registry: HealthRegistry | None = None,
    ) -> None:
        self.drain_timeout_s = drain_timeout_s
        self.health_registry = health_registry

        self._active_jobs: int = 0
        self._jobs_idle_event: asyncio.Event = asyncio.Event()
        self._jobs_idle_event.set()

        self._drain_callbacks: list[AsyncCallback] = []
        self._cleanup_callbacks: list[AsyncCallback] = []

        self._is_shutting_down: bool = False
        self._shutdown_complete: asyncio.Event = asyncio.Event()

    @property
    def is_shutting_down(self) -> bool:
        """Return True if shutdown has been initiated."""
        return self._is_shutting_down

    @property
    def active_jobs_count(self) -> int:
        """Return current count of active in-flight jobs."""
        return self._active_jobs

    def register_drain_callback(self, callback: AsyncCallback) -> None:
        """Register a callback invoked when shutdown begins (e.g. stop consuming)."""
        self._drain_callbacks.append(callback)

    def register_cleanup_callback(self, callback: AsyncCallback) -> None:
        """Register a callback to be called after in-flight jobs drain (e.g. close pools)."""
        self._cleanup_callbacks.append(callback)

    def attach_signal_handlers(self) -> None:
        """Attach SIGINT and SIGTERM handlers to the running event loop."""
        try:
            loop = asyncio.get_running_loop()

            def _handle_signal(sig_val: signal.Signals) -> None:
                asyncio.create_task(self.trigger_shutdown(signal_name=sig_val.name))

            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _handle_signal, sig)
            logger.info("Graceful shutdown signal handlers (SIGINT, SIGTERM) attached")
        except NotImplementedError:
            # Event loop doesn't support add_signal_handler (e.g. non-main thread or Windows)
            logger.warning("Signal handlers not supported in current environment/thread")

    @contextmanager
    def track_job(self) -> Iterator[None]:
        """Context manager tracking an active in-flight processing job.

        Increments active job counter on entry, decrements on exit, and signals the idle event.
        """
        self._active_jobs += 1
        self._jobs_idle_event.clear()
        try:
            yield
        finally:
            self._active_jobs = max(0, self._active_jobs - 1)
            if self._active_jobs == 0:
                self._jobs_idle_event.set()

    async def trigger_shutdown(self, signal_name: str = "MANUAL") -> None:
        """Execute the graceful shutdown sequence (R20.8)."""
        if self._is_shutting_down:
            logger.warning("Shutdown already in progress; ignoring duplicate trigger")
            await self._shutdown_complete.wait()
            return

        self._is_shutting_down = True
        logger.info("Graceful shutdown initiated (signal: %s)", signal_name)

        # 1. Mark health as draining
        if self.health_registry:
            self.health_registry.set_draining(True)

        # 2. Execute drain callbacks (e.g. cancel consumer tags so no new messages arrive)
        for cb in self._drain_callbacks:
            try:
                await cb()
            except Exception as err:
                logger.error("Error in shutdown drain callback: %s", err)

        # 3. Wait for in-flight jobs to complete
        if self._active_jobs > 0:
            logger.info(
                "Waiting up to %.1fs for %d active in-flight jobs to finish",
                self.drain_timeout_s,
                self._active_jobs,
            )
            try:
                await asyncio.wait_for(self._jobs_idle_event.wait(), timeout=self.drain_timeout_s)
                logger.info("All in-flight jobs completed cleanly")
            except TimeoutError:
                logger.warning(
                    "Drain timeout (%.1fs) expired with %d active jobs; proceeding with shutdown",
                    self.drain_timeout_s,
                    self._active_jobs,
                )

        # 4. Execute resource cleanup callbacks
        for cb in self._cleanup_callbacks:
            try:
                await cb()
            except Exception as err:
                logger.error("Error in shutdown cleanup callback: %s", err)

        self._shutdown_complete.set()
        logger.info("Graceful shutdown sequence completed successfully")

    async def wait_until_complete(self) -> None:
        """Wait until the graceful shutdown sequence has finished."""
        await self._shutdown_complete.wait()
