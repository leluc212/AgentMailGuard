"""Guard worker: runs MailGuardPipeline as a RabbitMQ consumer (or from stdin for demos).

    python -m services.guard_worker.main --stdin < jobs.jsonl        # no broker needed
    python -m services.guard_worker.main --amqp amqp://guest:guest@localhost/ --queue mailguard.inbound

Each job is a JSON object:
    {"job_id": "...", "email": {...NormalizedMessage-like...}, "chunks": [...Candidate-like...],
     "system_instructions": "...", "category_instructions": "...", "category": "support",
     "draft": {"body": "...", "citations": [...]}   # optional: outbound-only inspection}

The worker publishes a ``mailguard.decisions`` message per job (AMQP mode) or prints the
decision as JSON (stdin mode). It never sends email itself: the dispatcher consults
``decision.action`` and ``dispatch_allowed``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from mailguard.contracts.email import DraftCandidate, GuardedEmail, RetrievedChunk
from mailguard.integration.adapters import decision_to_job_result
from mailguard.pipeline import GuardConfig, MailGuardPipeline

logger = logging.getLogger("services.guard_worker")


async def process_job(pipeline: MailGuardPipeline, job: dict[str, Any]) -> dict[str, Any]:
    email = GuardedEmail.from_any(job.get("email") or {})
    chunks = [RetrievedChunk.from_any(c) for c in job.get("chunks") or []]
    category = job.get("category") or email.category
    system = str(job.get("system_instructions") or "You are a customer support assistant.")
    if job.get("draft"):  # outbound-only inspection of an existing draft
        report = await pipeline.inspect_inbound(email, category=category)
        bundle = await pipeline.build_prompt(report, email, chunks, system_instructions=system)
        draft = DraftCandidate.from_any(job["draft"])
        report = await pipeline.inspect_outbound(
            report,
            draft,
            email_like=email,
            kept_chunks=bundle.kept_chunks,
            protected_texts=[system],
            category=category,
        )
        if report.l4 is not None:
            draft = draft.model_copy(update={"body": report.l4.redacted_text})
    else:
        report = await pipeline.inspect_inbound(email, category=category)
        draft = None
    result = decision_to_job_result(report, draft)
    result["job_id"] = job.get("job_id")
    result["summary"] = pipeline.summary(report)
    return result


async def run_stdin(pipeline: MailGuardPipeline) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
            result = await process_job(pipeline, job)
        except Exception as exc:  # keep the worker alive; report the failure
            result = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    return 0


async def run_amqp(pipeline: MailGuardPipeline, url: str, queue: str, out_exchange: str) -> int:
    try:
        import aio_pika
    except ImportError:
        logger.error("aio-pika not installed: pip install 'agentmailguard[runtime]'")
        return 2
    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=8)
        q = await channel.declare_queue(queue, durable=True)
        exchange = await channel.declare_exchange(
            out_exchange, aio_pika.ExchangeType.FANOUT, durable=True
        )
        logger.info("consuming %s -> %s", queue, out_exchange)
        async with q.iterator() as it:
            async for message in it:
                async with message.process(requeue=False):
                    try:
                        job = json.loads(message.body.decode("utf-8"))
                        result = await process_job(pipeline, job)
                    except Exception as exc:
                        logger.exception("job failed")
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                    await exchange.publish(
                        aio_pika.Message(
                            body=json.dumps(result, default=str).encode("utf-8"),
                            content_type="application/json",
                        ),
                        routing_key="",
                    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--stdin", action="store_true", help="read JSON jobs from stdin (demo mode)")
    ap.add_argument("--amqp", default=None, help="AMQP URL, e.g. amqp://guest:guest@localhost/")
    ap.add_argument("--queue", default="mailguard.inbound")
    ap.add_argument("--out-exchange", default="mailguard.decisions")
    ap.add_argument("--config", default="C3", help="guard configuration preset")
    ap.add_argument("--no-audit", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    pipeline = MailGuardPipeline(config=GuardConfig.preset(args.config), audit=not args.no_audit)
    if args.amqp:
        return asyncio.run(run_amqp(pipeline, args.amqp, args.queue, args.out_exchange))
    if args.stdin:
        return asyncio.run(run_stdin(pipeline))
    ap.error("choose --stdin or --amqp")
    return 2


if __name__ == "__main__":
    sys.exit(main())
