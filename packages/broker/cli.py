"""Broker administration CLI (R3.2): ``python -m packages.broker.cli declare``.

Used by the compose ``init`` job so exchanges, queues and bindings exist before any
service publishes. Every worker also declares idempotently at startup.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aio_pika

from packages.broker.worker_runtime import declare_topology
from packages.core.settings import AppSettings


async def declare_from_settings(settings: AppSettings) -> None:
    """Connect with ``settings.broker`` and declare the full topology idempotently."""
    connection = await aio_pika.connect_robust(settings.broker.url)
    try:
        await declare_topology(connection, settings)
    finally:
        await connection.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="RabbitMQ topology administration (R3.2)")
    parser.add_argument("command", choices=["declare"])
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(declare_from_settings(AppSettings()))
    print("Broker topology declared.")


if __name__ == "__main__":
    main()
