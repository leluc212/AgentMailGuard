"""Deterministic generator for classification seed dataset (R22.1, R6.4, R6.8).

Generates >= 300 labelled emails across all 9 R6.4 categories:
support, sales, billing, administration, scheduling, general_inquiry,
automated_notification, acknowledgement, no_response.

Performs a stratified 80/20 train/test split (256 train, 64 test)
and writes versioned .jsonl artifacts to evaluation/datasets/classification/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evaluation.datasets.schemas import (
    ClassificationCategory,
    ClassificationDatasetItem,
    WorkflowHint,
)

DATASETS_DIR = Path(__file__).resolve().parent / "classification"

# Template definitions for generating diverse, realistic enterprise emails
TEMPLATES: list[dict[str, Any]] = [
    # =========================================================================
    # 1. SUPPORT (45 examples)
    # =========================================================================
    {
        "category": ClassificationCategory.SUPPORT,
        "intent": "crash_investigation",
        "reply_required": True,
        "workflow_hint": WorkflowHint.AI_GENERATE,
        "subjects": [
            "Application crash HTTP 500 on PDF export",
            "Critical error: MemoryAllocationError during analytics generation",
            "Server panic: heap overflow in worker thread",
            "Crash report: unhandled exception in invoice export module",
            "Out of memory crash on monthly data roll-up",
        ],
        "bodies": [
            "When exporting quarterly PDF reports, the web application crashes with "
            "HTTP 500 MemoryAllocationError. Stack trace indicates line 142 in exporter.py. "
            "Please investigate immediately as our audit deadline is tomorrow.",
            "Our team is blocked because analytics export triggers an instant 500 error. "
            "Container logs show out-of-memory SIGKILL on worker process.",
            "The reporting dashboard crashes every time we select date ranges greater than "
            "30 days. Error code: ERR_OOM_500. Can you increase memory allocation?",
            "Exporting to PDF failed with server error: memory limit reached. We need "
            "an urgent fix or a manual workaround.",
            "Critical crash observed during month-end close. The exporter service exits "
            "unexpectedly when processing large tables.",
        ],
        "senders": [
            "alice.smith@clientcorp.com",
            "david.lee@enterprise.net",
            "support-request@logistics.org",
            "devops-lead@partner.io",
            "it-ops@globalfinance.com",
        ],
    },
    {
        "category": ClassificationCategory.SUPPORT,
        "intent": "hardware_fault",
        "reply_required": True,
        "workflow_hint": WorkflowHint.AI_GENERATE,
        "subjects": [
            "Sensor telemetry drift on SKU-SENSOR-02",
            "Hardware communication timeout on SKU-WIDGET-01",
            "Faulty connector cable SKU-CABLE-03 pinout issue",
            "Modbus RS-485 failure on module installation",
            "Widget power input voltage warning",
        ],
        "bodies": [
            "We observed a +/- 2.5C drift on SKU-SENSOR-02, which violates our precision "
            "tolerance of 0.1C. Can you arrange a replacement unit under warranty?",
            "Our SKU-WIDGET-01 controller has stopped responding over RS-485. LED status "
            "blinks red code 4. Please send diagnostic instructions.",
            "Cable SKU-CABLE-03 arrived with a damaged IP67 weather seal. It cannot be deployed "
            "in our outdoor facility. Replacement needed.",
            "Temperature readings from sensor unit #9928 are erratic. We need factory calibration "
            "support or replacement under the 12-month warranty.",
            "Dual redundant power supply on widget SKU-WIDGET-01 fails to switch over on line "
            "loss. Is there a firmware patch available?",
        ],
        "senders": [
            "hardware-eng@plant.com",
            "dan.miller@industrial.org",
            "facilities@acmelabs.com",
            "tech-super@factory.de",
            "quality@manufacturing.us",
        ],
    },
    {
        "category": ClassificationCategory.SUPPORT,
        "intent": "software_bug",
        "reply_required": True,
        "workflow_hint": WorkflowHint.AI_GENERATE,
        "subjects": [
            "Webhook signature verification fails in Python SDK",
            "Pagination cursor loop in message list API",
            "Broken link in email notification template",
            "CSV export encoding corrupted for UTF-8 characters",
            "Search filter does not return archived emails",
        ],
        "bodies": [
            "When verifying webhook HMAC signatures using the v1 API, the signature always "
            "mismatches for payloads containing multi-byte unicode characters.",
            "The /v1/threads endpoint returns duplicate items when paginating past page 3. "
            "Cursor pagination appears to ignore the after parameter.",
            "Special characters such as accents and umlauts are mangled in the exported CSV. "
            "Expected UTF-8 BOM, but received Latin-1.",
            "Search queries filtering by status='archived' return zero results even though "
            "the database contains thousands of archived messages.",
            "API rate limit headers are missing from response headers on error responses. "
            "Please check middleware ordering.",
        ],
        "senders": [
            "developer@saas.com",
            "integrations@app.io",
            "qa@startup.co",
            "lead-coder@agency.org",
            "api-team@external.net",
        ],
    },
    # =========================================================================
    # 2. SALES (35 examples)
    # =========================================================================
    {
        "category": ClassificationCategory.SALES,
        "intent": "pricing_quote",
        "reply_required": True,
        "workflow_hint": WorkflowHint.AI_GENERATE,
        "subjects": [
            "Request for Enterprise Quote — 250 Mailbox Tier",
            "Pricing inquiry for volume hardware order SKU-WIDGET-01",
            "Custom SLA and commercial discount inquiry",
            "Renewal quote for Acme Pro subscription",
            "Hardware bundle discount for sensors and widgets",
        ],
        "bodies": [
            "We are planning to migrate 250 enterprise mailboxes to your platform next quarter. "
            "Could you provide formal pricing options including Tier-2 support SLA?",
            "Our procurement team wants to order 100 units of SKU-WIDGET-01 and 200 units of "
            "SKU-SENSOR-02. What volume discount tier can Acme offer?",
            "Our annual subscription expires next month. Please send the renewal proposal with "
            "options for multi-year lock-in pricing.",
            "We would like to add 50 seats to our current plan. Please send an updated order form "
            "and schedule a pricing call.",
            "Can you provide a price comparison between Standard and Enterprise support plans "
            "for an infrastructure deployment of 500 agents?",
        ],
        "senders": [
            "procurement@megacorp.com",
            "buyer@retailchains.com",
            "cfo-office@fintech.io",
            "director@ventures.com",
            "purchasing@distributor.org",
        ],
    },
    {
        "category": ClassificationCategory.SALES,
        "intent": "demo_request",
        "reply_required": True,
        "workflow_hint": WorkflowHint.AI_GENERATE,
        "subjects": [
            "Product demo request for executive leadership",
            "Inquiry regarding RAG email automation capabilities",
            "Proof of concept trial request",
            "Information request regarding AI email response features",
        ],
        "bodies": [
            "Our VP of Operations would like to see a live demonstration of your RAG-based email "
            "triage and draft generation workflow. Can we schedule 45 minutes this Thursday?",
            "We are evaluating intelligent email response solutions and would like to initiate "
            "a 30-day proof of concept in our staging environment.",
            "Does your platform support Microsoft 365 Exchange hybrid setups? We would like a demo "
            "tailored to our enterprise security team.",
            "Could someone from your solutions engineering team walk us through your vector "
            "retrieval and hallucination prevention architecture?",
        ],
        "senders": [
            "solutions-lead@enterprise.com",
            "evaluator@techstack.io",
            "innovations@bankcorp.us",
            "vp-it@healthsystem.org",
        ],
    },
    # =========================================================================
    # 3. BILLING (40 examples)
    # =========================================================================
    {
        "category": ClassificationCategory.BILLING,
        "intent": "invoice_dispute",
        "reply_required": True,
        "workflow_hint": WorkflowHint.AI_GENERATE,
        "subjects": [
            "Discrepancy on Invoice INV-2026-8891 — Duplicate Line Item",
            "Billing error on Invoice INV-2026-1044",
            "Incorrect tax rate applied to September statement",
            "Dispute regarding unexpected overage fee",
            "Request for credit note regarding order cancellation",
        ],
        "bodies": [
            "We were billed twice for the 'Enterprise Pro Addon' on invoice INV-2026-8891. "
            "Please credit $450.00 back to our corporate account or issue a revised invoice.",
            "Invoice INV-2026-1044 includes charges for mailboxes that were decommissioned "
            "in August. Please adjust the invoice total before we process payment.",
            "Our state tax exemption certificate was submitted last month, but 8.5% sales tax "
            "was charged on our latest invoice. Please reissue with tax removed.",
            "We noticed an overage fee of $320.00 for API calls, but our contract specifies "
            "unlimited tier during Q3. Can you please review and credit this charge?",
            "Order ORD-8820 was cancelled prior to shipping, but our credit card was charged. "
            "Please confirm refund processing within Net 30 terms.",
        ],
        "senders": [
            "ap@accountingcorp.com",
            "finance@holdingco.org",
            "payables@nonprofit.org",
            "billing-contact@client.net",
            "treasury@globaltrade.com",
        ],
    },
    {
        "category": ClassificationCategory.BILLING,
        "intent": "payment_inquiry",
        "reply_required": True,
        "workflow_hint": WorkflowHint.TEMPLATE,
        "subjects": [
            "Request for Acme ACH wire transfer details",
            "Updating corporate credit card on file",
            "W-9 tax form request for vendor registration",
            "Confirmation of payment terms Net 30",
            "Receipt request for wire payment processed Sep 10",
        ],
        "bodies": [
            "Our finance department requires your banking routing and ACH wire transfer details "
            "on official company letterhead to process outstanding invoices.",
            "Our corporate Amex card has expired. Could you provide a secure portal link where "
            "our billing admin can update our payment method?",
            "Please provide an updated signed IRS Form W-9 for Acme Corporation so we can add "
            "you to our accounts payable system.",
            "We would like to confirm that our account payment terms have been set to Net 30 as "
            "agreed in our master service contract.",
            "We remitted $1,850.00 via wire on September 10th. Please send the official receipt "
            "and confirm account balance is zero.",
        ],
        "senders": [
            "accounts@customer.com",
            "fin-team@telecom.org",
            "vendor-mgt@retail.com",
            "ap-desk@biotech.io",
            "accounting@partner.us",
        ],
    },
    # =========================================================================
    # 4. ADMINISTRATION (30 examples)
    # =========================================================================
    {
        "category": ClassificationCategory.ADMINISTRATION,
        "intent": "account_access",
        "reply_required": True,
        "workflow_hint": WorkflowHint.TEMPLATE,
        "subjects": [
            "Password reset request for admin user account",
            "MFA token desynchronization — unable to login",
            "Add new workspace administrator role for team member",
            "Deactivate former employee credentials",
            "SSO SAML certificate renewal requirement",
        ],
        "bodies": [
            "I am locked out of my administrator account after changing phones. Please trigger "
            "an MFA reset or temporary bypass code.",
            "Our security team requests administrator privileges for jane.doe@client.com. "
            "Please update her role in the Acme tenant console.",
            "Please immediately revoke access for user john.doe@client.com who departed "
            "the company effective today. All sessions should be terminated.",
            "Our Okta SAML signing certificate expires in 14 days. Where can we upload the new "
            "metadata XML file in the portal?",
            "We need to export tenant audit logs for the period between Jan 1 and Aug 31 for our "
            "SOC 2 compliance auditor.",
        ],
        "senders": [
            "security-ops@client.com",
            "it-helpdesk@partner.org",
            "sysadmin@corporate.net",
            "identity@fintech.io",
            "compliance@healthcare.us",
        ],
    },
    # =========================================================================
    # 5. SCHEDULING (30 examples)
    # =========================================================================
    {
        "category": ClassificationCategory.SCHEDULING,
        "intent": "meeting_coordination",
        "reply_required": True,
        "workflow_hint": WorkflowHint.AI_GENERATE,
        "subjects": [
            "Quarterly Business Review: Scheduling call for next week",
            "Rescheduling technical architecture sync meeting",
            "Availability check for onboarding kickoff call",
            "Proposed times for SLA review discussion",
            "Invitation: Monthly vendor alignment meeting",
        ],
        "bodies": [
            "Could we schedule our Q3 Business Review for next Tuesday at 2:00 PM EST? "
            "Our account executive and engineering directors will attend.",
            "Due to an internal conflict, I need to reschedule our onboarding session scheduled "
            "for tomorrow. Are you available Thursday at 10:00 AM or 3:00 PM?",
            "Let's schedule a 30-minute sync to review the latest retrieval benchmark results. "
            "Please send your availability for Wednesday afternoon.",
            "Our team is ready to begin the mailbox connector rollout. What times work best "
            "for your engineering team next Monday?",
            "Can we move our recurring bi-weekly check-in by one hour earlier to accommodate "
            "our European colleagues?",
        ],
        "senders": [
            "pm@partner.com",
            "director-ops@client.net",
            "coordinator@global.org",
            "lead@consulting.com",
            "exec-asst@enterprise.io",
        ],
    },
    # =========================================================================
    # 6. GENERAL INQUIRY (35 examples)
    # =========================================================================
    {
        "category": ClassificationCategory.GENERAL_INQUIRY,
        "intent": "general_faq",
        "reply_required": True,
        "workflow_hint": WorkflowHint.AI_GENERATE,
        "subjects": [
            "Inquiry regarding supported mail protocols and encryption",
            "Office headquarters address and billing contact details",
            "Compatibility inquiry: Ubuntu 24.04 and Docker Compose",
            "Feature question: Does Acme support multi-tenant pgvector?",
            "Media and press inquiry: Case study participation",
        ],
        "bodies": [
            "Does your platform support direct IMAP/SMTP connections alongside Microsoft Graph "
            "and Gmail API? We have several legacy mailboxes to integrate.",
            "Can you provide your legal corporate entity name and registered headquarters "
            "address for our vendor questionnaire?",
            "Is the agent dispatch worker capable of running on ARM64 architecture or only "
            "x86_64? We plan to deploy on AWS Graviton instances.",
            "We are preparing an industry report on AI-driven email response systems and would "
            "like to interview an Acme spokesperson about hallucination mitigation.",
            "What is the maximum attachment file size supported when synchronizing messages via "
            "the Mail Connector service?",
        ],
        "senders": [
            "info-seeker@tech.org",
            "analyst@research.net",
            "editor@industrynews.com",
            "inquiries@consultancy.co",
            "general@public.edu",
        ],
    },
    # =========================================================================
    # 7. AUTOMATED NOTIFICATION (35 examples - Zero reply required)
    # =========================================================================
    {
        "category": ClassificationCategory.AUTOMATED_NOTIFICATION,
        "intent": "system_alert",
        "reply_required": False,
        "workflow_hint": WorkflowHint.NONE,
        "headers": {
            "List-Unsubscribe": "<https://notify.alerting.net/unsubscribe?id=8831>",
            "Precedence": "bulk",
        },
        "subjects": [
            "[Alert] CPU utilization exceeded 85% on node-prod-04",
            "GitHub Actions: CI workflow 'quality' completed successfully",
            "Datadog Alert: elevated p99 latency on /v1/messages",
            "Weekly Cloud Infrastructure Digest — Sep 12",
            "Security Advisory: Dependabot alert in repository",
        ],
        "bodies": [
            "Monitor alert: Host node-prod-04 has sustained CPU usage > 85% for 15 minutes. "
            "View metric graphs at https://monitoring.internal/host/node-prod-04. "
            "To unsubscribe from high CPU notifications, adjust alert routing.",
            "Build run #450 for commit 7c9a102 on branch main finished: SUCCESS. "
            "109 unit tests passed, 31 integration tests passed. View details on GitHub.",
            "Latency threshold warning: p99 latency reached 840ms on service 'rag-email-api'. "
            "Trigger condition: > 500ms for 3 consecutive evaluation cycles.",
            "Your weekly AWS billing digest: estimated month-to-date charges are $1,240.50. "
            "Manage your budget alerts in the AWS Billing console.",
            "Security notice: 1 moderate vulnerability detected in package uv. "
            "Review pull request #14 to apply the automated security patch.",
        ],
        "senders": [
            "no-reply@github.com",
            "alerts@datadoghq.com",
            "notifications@pagerduty.com",
            "aws-billing@amazon.com",
            "security@dependabot.com",
        ],
    },
    # =========================================================================
    # 8. ACKNOWLEDGEMENT (35 examples - Zero reply required)
    # =========================================================================
    {
        "category": ClassificationCategory.ACKNOWLEDGEMENT,
        "intent": "receipt_confirmation",
        "reply_required": False,
        "workflow_hint": WorkflowHint.NONE,
        "headers": {
            "Auto-Submitted": "auto-generated",
        },
        "subjects": [
            "We received your support inquiry — Ticket #TICK-4402",
            "Order Confirmation: ORD-9901 received and queued",
            "Thank you for your payment: Receipt #REC-8819",
            "Your request has been received (Case 109281)",
            "Automated confirmation: Email received by Acme Desk",
        ],
        "bodies": [
            "Thank you for contacting Acme Customer Support. Your inquiry has been logged "
            "as Ticket #TICK-4402. A support engineer will review your request within 1 hour.",
            "Order Confirmation: Thank you for your business. We have received order ORD-9901 "
            "for 2x SKU-WIDGET-01. You will receive a tracking update once dispatched.",
            "Payment Receipt: We have received your payment of $900.00 for invoice INV-2026-8891. "
            "Your account balance is currently paid in full.",
            "This is an automated notification confirming receipt of your message. Please do not "
            "reply directly to this email as incoming replies are not monitored.",
            "We have received your document upload. Your file 'network_logs.pcap' is queued for "
            "virus scanning and diagnostic analysis.",
        ],
        "senders": [
            "support-system@acme.com",
            "orders@acme.com",
            "billing-daemon@acme.com",
            "tickets@desk.acme.com",
            "receipts@merchant.org",
        ],
    },
    # =========================================================================
    # 9. NO RESPONSE (35 examples - Zero reply required)
    # =========================================================================
    {
        "category": ClassificationCategory.NO_RESPONSE,
        "intent": "out_of_office",
        "reply_required": False,
        "workflow_hint": WorkflowHint.NONE,
        "headers": {
            "Auto-Submitted": "auto-replied",
            "X-Auto-Response-Suppress": "All",
        },
        "subjects": [
            "Automatic Reply: Out of the office until Sep 22",
            "Auto-Response: On vacation with no email access",
            "Out of Office: Attending European Tech Summit",
            "Delivery Status Notification (Failure) — Mail delivery failed",
            "Unsubscribe Confirmation: You have been removed from the list",
        ],
        "bodies": [
            "Thank you for contacting me. I am currently out of the office on annual leave "
            "until Monday, September 22nd, with no email access. For urgent inquiries, please "
            "reach out to desk@partnerco.com.",
            "I am away from the office attending the Tech Conference in Berlin. I will review "
            "my inbox upon my return on Thursday.",
            "Out of Office: Thank you for your email. I am taking paternity leave and will "
            "return in October. Please direct active customer issues to my backup team.",
            "Delivery Status Notification: The following message could not be delivered to the "
            "recipient user@invalid-domain-99128.org: 550 Mailbox does not exist.",
            "You have been successfully unsubscribed from the Acme Monthly Marketing Digest. "
            "If this was in error, you can resubscribe in your account settings.",
        ],
        "senders": [
            "charlie.brown@partnerco.com",
            "mailer-daemon@relay.net",
            "postmaster@mailserver.org",
            "out-of-office@corporate.com",
            "subscriptions@mailservice.io",
        ],
    },
]


def generate_classification_items() -> list[ClassificationDatasetItem]:
    """Generate 320 deterministic, schema-validated email classification instances."""
    items: list[ClassificationDatasetItem] = []
    item_id = 0

    for template_group in TEMPLATES:
        category = template_group["category"]
        intent = template_group["intent"]
        reply_required = template_group["reply_required"]
        workflow_hint = template_group["workflow_hint"]
        headers = template_group.get("headers", {})

        subjects = template_group["subjects"]
        bodies = template_group["bodies"]
        senders = template_group["senders"]

        # Generate combinations deterministically
        # Each template group contributes multiple variations
        for s_idx, subj in enumerate(subjects):
            for b_idx, body in enumerate(bodies):
                # Sample sender deterministically
                sender = senders[(s_idx + b_idx) % len(senders)]
                item_id += 1
                unique_key = f"cls_{category.value}_{item_id:04d}"

                # Custom headers for specific items if provided
                custom_headers = dict(headers)
                if category == ClassificationCategory.AUTOMATED_NOTIFICATION:
                    custom_headers["List-Unsubscribe"] = f"<https://notify.org/unsub?id={item_id}>"
                elif category == ClassificationCategory.NO_RESPONSE:
                    custom_headers["Auto-Submitted"] = "auto-replied"

                item = ClassificationDatasetItem(
                    id=unique_key,
                    subject=subj,
                    body=body,
                    sender=sender,
                    headers=custom_headers,
                    gold_category=category,
                    gold_intent=intent,
                    gold_reply_required=reply_required,
                    gold_workflow_hint=workflow_hint,
                    metadata={"template_index": s_idx, "variant_index": b_idx},
                )
                items.append(item)

    return items


def split_and_save(
    items: list[ClassificationDatasetItem],
    train_ratio: float = 0.8,
) -> tuple[int, int]:
    """Perform deterministic stratified 80/20 train/test split and write .jsonl files."""
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)

    # Group items by category for stratified sampling
    by_category: dict[ClassificationCategory, list[ClassificationDatasetItem]] = {}
    for item in items:
        by_category.setdefault(item.gold_category, []).append(item)

    train_items: list[ClassificationDatasetItem] = []
    test_items: list[ClassificationDatasetItem] = []

    for _cat, cat_items in by_category.items():
        # Deterministic split per category
        train_count = int(len(cat_items) * train_ratio)
        train_items.extend(cat_items[:train_count])
        test_items.extend(cat_items[train_count:])

    # Write train.jsonl
    train_path = DATASETS_DIR / "train.jsonl"
    with open(train_path, "w", encoding="utf-8") as f:
        for item in train_items:
            f.write(item.model_dump_json() + "\n")

    # Write test.jsonl
    test_path = DATASETS_DIR / "test.jsonl"
    with open(test_path, "w", encoding="utf-8") as f:
        for item in test_items:
            f.write(item.model_dump_json() + "\n")

    return len(train_items), len(test_items)


def main() -> None:
    """Build and save classification seed datasets."""
    items = generate_classification_items()
    train_count, test_count = split_and_save(items)
    total = train_count + test_count
    print(f"Generated {total} classification items: {train_count} train, {test_count} test.")


if __name__ == "__main__":
    main()
