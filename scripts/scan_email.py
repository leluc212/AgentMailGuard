"""Demo CLI: run one email (JSON or .eml) through the guard and print a readable report.

python scripts/scan_email.py tests/fixtures/emails.json --pick attacks:0 --config C3
python scripts/scan_email.py my_message.eml --kb datasets/seed/support_kb
echo '{"subject": "hi", "body_text": "..."}' | python scripts/scan_email.py -
"""

from __future__ import annotations

import argparse
import asyncio
import email as email_lib
import json
import sys
from email import policy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mailguard.contracts.email import DraftCandidate, GuardedEmail  # noqa: E402
from mailguard.datasets.seed import load_kb, simple_retrieve  # noqa: E402
from mailguard.pipeline import GuardConfig, MailGuardPipeline  # noqa: E402

SYSTEM = "You are the Acme customer-support assistant. Answer using the knowledge base excerpts."


def load_email(arg: str, pick: str | None) -> GuardedEmail:
    if arg == "-":
        return GuardedEmail.from_any(json.load(sys.stdin))
    path = Path(arg)
    if path.suffix.lower() == ".eml":
        msg = email_lib.message_from_bytes(path.read_bytes(), policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        return GuardedEmail(
            message_id=str(msg.get("Message-ID", "")),
            sender_email=str(msg.get("From", "")),
            subject=str(msg.get("Subject", "")),
            body_text=text,
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if pick:
        group, _, idx = pick.partition(":")
        data = data[group][int(idx or 0)]
    return GuardedEmail.from_any(data)


async def main_async(args: argparse.Namespace) -> int:
    email = load_email(args.email, args.pick)
    chunks = []
    if args.kb:
        kb = load_kb(Path(args.kb))
        chunks = [
            {"chunk_id": c.chunk_id, "document_id": c.document_id, "content": c.content}
            for c in simple_retrieve(email.subject + " " + email.text, kb, 3)
        ]
    pipeline = MailGuardPipeline(config=GuardConfig.preset(args.config), audit=False)

    async def echo_agent(messages):
        return DraftCandidate(
            body=args.draft or "Thank you for your message. We will get back to you shortly."
        )

    report, draft, bundle = await pipeline.run(
        email, chunks, echo_agent, system_instructions=SYSTEM
    )
    print("=" * 72)
    print(f"Subject: {email.subject}\nFrom:    {email.sender_email}\nConfig:  {args.config}")
    print("=" * 72)
    for v in report.verdicts():
        print(
            f"[{v.layer.value:22s}] severity={v.severity.value:8s} score={v.score:.2f} by={v.decided_by:8s} {v.latency_ms}ms"
        )
        for f in v.findings[: args.max_findings]:
            print(f"    - {f.detector:9s} {f.rule_id or '':32s} {f.score:.2f} {f.technique or ''}")
            if f.excerpt and args.verbose:
                print(f"      > {f.excerpt[:160]}")
    if report.l2 is not None:
        print(f"\nIntent: {report.l2.user_intent}")
        if report.l2.stripped_segments:
            print(
                f"Stripped {len(report.l2.stripped_segments)} segment(s), removed_ratio={report.l2.removed_ratio:.2f}"
            )
    if report.inbound_decision:
        d = report.inbound_decision
        print(
            f"\nInbound decision : {d.action.value.upper():15s} rule={d.matched_rule_id} tier={d.risk_tier.value}"
        )
    if report.decision and report.decision is not report.inbound_decision:
        d = report.decision
        print(
            f"Outbound decision: {d.action.value.upper():15s} rule={d.matched_rule_id} tier={d.risk_tier.value}"
        )
    if bundle and args.verbose:
        print("\n--- guarded prompt (user message) ---")
        print(bundle.messages[1].content[:2000])
    if draft is not None:
        print(f"\nDraft (after L4): {draft.body[:300]}")
    if args.json:
        print(json.dumps(pipeline.summary(report), indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("email", help="JSON file, .eml file, or - for stdin JSON")
    ap.add_argument("--pick", help="group:index inside a fixture JSON, e.g. attacks:0")
    ap.add_argument("--config", default="C3")
    ap.add_argument("--kb", default=None, help="folder of markdown KB docs to retrieve from")
    ap.add_argument(
        "--draft", default=None, help="draft text to inspect outbound (default: canned)"
    )
    ap.add_argument("--max-findings", type=int, default=6)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
