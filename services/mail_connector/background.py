"""Owned background loop with bounded, cancellation-safe shutdown (RA.10, R20.8)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class BackgroundLoop:
    """Runs `run_loop(stop_event)` in one task; stop() signals, waits, then cancels."""

    def __init__(
        self,
        name: str,
        run_loop: Callable[[asyncio.Event], Awaitable[None]],
        stop_timeout_s: float = 15.0,
    ) -> None:
        self.name = name
        self._run_loop = run_loop
        self._stop_timeout_s = stop_timeout_s
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()

        async def _runner() -> None:
            await self._run_loop(self._stop_event)

        self._task = asyncio.create_task(_runner(), name=self.name)
        logger.info("Background loop '%s' started", self.name)

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        try:
            # wait_for cancels the task itself if the timeout elapses.
            await asyncio.wait_for(task, timeout=self._stop_timeout_s)
        except TimeoutError:
            logger.warning(
                "Background loop '%s' did not stop in %.1fs; cancelled",
                self.name,
                self._stop_timeout_s,
            )
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            logger.exception("Background loop '%s' exited with an error", self.name)
        finally:
            self._task = None
            logger.info("Background loop '%s' stopped", self.name)
