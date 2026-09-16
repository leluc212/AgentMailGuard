"""Standard fixture emails covering core operational archetypes (R5.9).

Provides realistic, RFC 822 compliant email test cases across 6 key archetypes:
1. support: Technical bug report requiring assistance and investigation.
2. billing: Invoice and payment inquiry citing invoice identifier.
3. newsletter: Marketing email with List-Unsubscribe header (zero reply required).
4. auto_reply: Out-of-office automated responder (zero reply required).
5. long_thread: Multi-turn thread structure with In-Reply-To and References.
6. identifier_bearing: Order and ticket inquiry citing explicit business IDs.
"""

from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate
from uuid import uuid4


@dataclass(frozen=True)
class ThreadMessage:
    """Individual message within a multi-turn conversation fixture."""

    sender_email: str
    sender_name: str
    body_text: str
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EmailFixture:
    """Encapsulates a test email fixture with metadata and MIME generator."""

    key: str
    category: str
    subject: str
    sender_email: str
    sender_name: str
    recipients: list[str]
    body_text: str
    body_html: str | None = None
    identifiers: list[str] = field(default_factory=list)
    reply_required: bool = True
    workflow_hint: str = "ai_generate"
    headers: dict[str, str] = field(default_factory=dict)
    thread_history: list[ThreadMessage] = field(default_factory=list)

    def to_raw_mime(self, provider_message_id: str | None = None) -> bytes:
        """Construct a standard RFC 822 MIME byte payload."""
        msg = EmailMessage()
        msg["Subject"] = self.subject
        msg["From"] = f"{self.sender_name} <{self.sender_email}>"
        msg["To"] = ", ".join(self.recipients)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = f"<{provider_message_id or uuid4().hex[:16]}@fixture.test>"

        for h_key, h_val in self.headers.items():
            msg[h_key] = h_val

        msg.set_content(self.body_text)
        if self.body_html:
            msg.add_alternative(self.body_html, subtype="html")

        return msg.as_bytes()


FIXTURE_EMAILS: list[EmailFixture] = [
    # 1. Technical Support Bug Report
    EmailFixture(
        key="support_crash",
        category="support",
        subject="Critical Error: Application crashing on PDF export",
        sender_email="alice.smith@clientcorp.com",
        sender_name="Alice Smith",
        recipients=["support@acme.com"],
        body_text=(
            "Hello Support Team,\n\n"
            "When trying to export our monthly analytics reports to PDF, the application "
            "abruptly crashes with HTTP 500: 'MemoryAllocationError at module exporter.py:142'.\n"
            "This is blocking our quarterly audit. Could you please investigate urgently?\n\n"
            "Best regards,\n"
            "Alice Smith\n"
            "Senior Systems Analyst"
        ),
        body_html=(
            "<p>Hello Support Team,</p>"
            "<p>When trying to export our monthly analytics reports to PDF, the application "
            "abruptly crashes with "
            "<code>HTTP 500: MemoryAllocationError at exporter.py:142</code>.</p>"
            "<p>This is blocking our quarterly audit. Could you please investigate urgently?</p>"
            "<p>Best regards,<br>Alice Smith</p>"
        ),
        reply_required=True,
        workflow_hint="ai_generate",
    ),
    # 2. Billing & Invoice Inquiry
    EmailFixture(
        key="billing_invoice",
        category="billing",
        subject="Discrepancy on Invoice INV-2026-8891",
        sender_email="bob.jones@enterprises.org",
        sender_name="Bob Jones",
        recipients=["billing@acme.com"],
        body_text=(
            "Hi Accounts Payable,\n\n"
            "I noticed an unexpected line item on invoice INV-2026-8891 dated September 1st. "
            "We were billed twice for the 'Enterprise Pro Addon' ($450.00 each).\n"
            "Could you please review the charge and issue a corrected invoice or credit note?\n\n"
            "Thanks,\n"
            "Bob Jones\n"
            "Finance Manager"
        ),
        identifiers=["INV-2026-8891"],
        reply_required=True,
        workflow_hint="ai_generate",
    ),
    # 3. Marketing Newsletter (Zero reply required)
    EmailFixture(
        key="newsletter_marketing",
        category="newsletter",
        subject="Cloud Innovations Weekly: Enterprise AI at Scale",
        sender_email="news@techdigest.io",
        sender_name="Cloud Innovations Digest",
        recipients=["team@acme.com"],
        body_text=(
            "Welcome to this week's Cloud Innovations digest!\n\n"
            "In this issue: Microservice architecture trends, vector database optimizations, "
            "and cost containment strategies for LLM workloads.\n\n"
            "To unsubscribe or update your subscription preferences, visit your settings."
        ),
        headers={
            "List-Unsubscribe": "<https://techdigest.io/unsubscribe?id=99281>",
            "Precedence": "bulk",
        },
        reply_required=False,
        workflow_hint="none",
    ),
    # 4. Out-of-Office Automatic Reply (Zero reply required)
    EmailFixture(
        key="auto_reply_vacation",
        category="auto_reply",
        subject="Automatic Reply: Out of Office until Sep 22",
        sender_email="charlie.brown@partnerco.com",
        sender_name="Charlie Brown",
        recipients=["support@acme.com"],
        body_text=(
            "Thank you for contacting me. I am currently out of the office on annual leave "
            "with limited email access through September 22nd.\n"
            "For urgent inquiries regarding pending contracts, "
            "please contact desk@partnerco.com.\n\n"
            "Best,\n"
            "Charlie Brown"
        ),
        headers={
            "Auto-Submitted": "auto-replied",
            "X-Auto-Response-Suppress": "All",
        },
        reply_required=False,
        workflow_hint="none",
    ),
    # 5. Multi-Turn Thread Conversation
    EmailFixture(
        key="long_thread_dispute",
        category="long_thread",
        subject="Re: Subscription renewal and SLA terms for Order ORD-9901",
        sender_email="dana.scully@fbi.gov",
        sender_name="Dana Scully",
        recipients=["sales@acme.com"],
        body_text=(
            "Hi Sales,\n\n"
            "Following up on our earlier discussions regarding ORD-9901, we agree to the "
            "tier-2 SLA revision. Please send the final master services agreement for "
            "countersignature.\n\n"
            "Agent Scully"
        ),
        identifiers=["ORD-9901"],
        reply_required=True,
        workflow_hint="ai_generate",
        headers={
            "In-Reply-To": "<msg-long_thread_dispute-turn-2@fixture.test>",
            "References": (
                "<msg-long_thread_dispute-turn-0@fixture.test> "
                "<msg-long_thread_dispute-turn-1@fixture.test> "
                "<msg-long_thread_dispute-turn-2@fixture.test>"
            ),
        },
        thread_history=[
            ThreadMessage(
                sender_email="dana.scully@fbi.gov",
                sender_name="Dana Scully",
                body_text="Inquiry regarding custom SLA terms for ORD-9901.",
            ),
            ThreadMessage(
                sender_email="sales@acme.com",
                sender_name="Acme Sales",
                body_text="We offer 99.9% uptime with 1-hour priority response under tier-2.",
            ),
            ThreadMessage(
                sender_email="dana.scully@fbi.gov",
                sender_name="Dana Scully",
                body_text="Understood. Can we clarify the data retention warranty?",
            ),
        ],
    ),
    # 6. Identifier-Bearing Order & Ticket Inquiry
    EmailFixture(
        key="identifier_order_ticket",
        category="identifier_bearing",
        subject="Status update for Order ORD-9901 and Ticket TICK-4402",
        sender_email="edward.norton@fightclub.org",
        sender_name="Edward Norton",
        recipients=["support@acme.com"],
        body_text=(
            "Hello Support,\n\n"
            "I am writing to check the status of ticket TICK-4402 regarding the replacement "
            "shipment for order ORD-9901. Has the tracking number been generated yet?\n\n"
            "Thanks,\n"
            "Edward"
        ),
        identifiers=["ORD-9901", "TICK-4402"],
        reply_required=True,
        workflow_hint="ai_generate",
    ),
]
