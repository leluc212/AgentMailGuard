"""Review UI draft queue, detail and decisions over recorded /v1 responses (task 6.8).

Requirements: R23.4 (queue, original email, thread summary, citations, approve/edit/reject),
R23.6 (X-Organization-Id on every call), R16.6/R16.7 (decisions carry review_ms).
The /v1 API is replaced at the HTTP boundary only (httpx.MockTransport); the real API is
exercised by tests/e2e (task 6.8 Playwright) against the isolated database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from packages.core.settings import FrontendServiceSettings, FrontendSettings
from services.frontend.drafts import resolve_review_ms
from services.frontend.main import STATIC_DIR, create_app

ORG = UUID("11111111-1111-4111-8111-111111111111")
DRAFT_ID = UUID("22222222-2222-4222-8222-222222222222")
SUBJECT = "Re: Where is order ORD-82915?"
BODY = (
    "Your order ORD-82915 was dispatched on 26 September. "
    "Refunds are issued within 14 days of the return arriving at our warehouse."
)
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
HX = {"HX-Request": "true"}


def _detail() -> dict[str, Any]:
    return {
        "id": str(DRAFT_ID),
        "job_id": str(uuid4()),
        "message_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "mailbox_id": str(uuid4()),
        "category": "billing",
        "status": "draft",
        "subject": SUBJECT,
        "confidence": 0.82,
        "created_at": "2026-09-28T10:00:00+00:00",
        "body": BODY,
        "citation_mismatch": False,
        "job_state": "DRAFTED",
        "dispatch_mode": "create_draft",
        # Recorded from Task 6's DraftDetailResponse (services/api/schemas/drafts.py).
        "original": {
            "message_id": str(uuid4()),
            "sender_email": "alice.smith@clientcorp.com",
            "sender_name": "Alice Smith",
            "subject": "Where is order ORD-82915?",
            "body_text": "Hello, where is my order ORD-82915? Thanks, Alice",
            "received_at": "2026-09-28T09:00:00+00:00",
            "rfc822_message_id": "orig-1@clientcorp.com",
        },
        "thread_summary": "Alice asks for the delivery status of ORD-82915.",
        "citations": [{"citation_id": "kb-returns-2", "chunk_id": "c-1"}],
        "cited_chunks": [
            {
                "citation_id": "kb-returns-2",
                "chunk_id": "c-1",
                "document_id": "d-1",
                "external_id": "kb-returns-2",
                "content": (
                    "Refunds are issued within 14 days after the return arrives at the warehouse."
                ),
                "heading_path": ["Returns policy"],
            }
        ],
        "business_data": {
            "customer_status": "FOUND",
            "degraded": False,
            "facts": [
                {"entity": "order", "reference": "ORD-82915", "status": "FOUND", "reason": None}
            ],
        },
        "feedback": None,
    }


class FakeV1:
    """Recorded /v1 drafts responses; records every request the UI makes."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        self.requests: list[httpx.Request] = []
        self.approve_error: tuple[int, dict[str, Any]] | None = None

    def calls(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        base = f"/v1/drafts/{self.detail['id']}"
        if method == "GET" and path == "/v1/drafts":
            keys = (
                "id",
                "job_id",
                "message_id",
                "thread_id",
                "mailbox_id",
                "category",
                "status",
                "subject",
                "confidence",
                "created_at",
            )
            item = {k: self.detail[k] for k in keys}
            return httpx.Response(200, json={"items": [item], "next_cursor": "c2"})
        if method == "GET" and path == base:
            return httpx.Response(200, json=self.detail)
        if method == "PATCH" and path == base:
            self.detail["body"] = json.loads(request.content)["body"]
            return httpx.Response(200, json=self.detail)
        if method == "POST" and path == f"{base}/approve":
            if self.approve_error is not None:
                return httpx.Response(self.approve_error[0], json=self.approve_error[1])
            self.detail["status"] = "approved"
            return httpx.Response(200, json={"draft_id": self.detail["id"], "status": "approved"})
        if method == "POST" and path == f"{base}/reject":
            self.detail["status"] = "rejected"
            return httpx.Response(200, json={"draft_id": self.detail["id"], "status": "rejected"})
        return httpx.Response(404, json={"error": "Draft not found", "code": "DRAFT_NOT_FOUND"})


def _settings(org: UUID | None = ORG) -> FrontendServiceSettings:
    return FrontendServiceSettings(
        _env_file=None,
        frontend=FrontendSettings(api_base_url="http://api.test", organization_id=org),
    )


@pytest.fixture
def fake() -> FakeV1:
    return FakeV1(_detail())


@pytest.fixture
async def ui(fake: FakeV1) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        _settings(), transport=httpx.MockTransport(fake.handler), configure_logging=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ui"
    ) as client:
        yield client


async def test_queue_lists_pending_drafts_through_v1_with_the_org_header(
    ui: httpx.AsyncClient, fake: FakeV1
) -> None:
    response = await ui.get("/drafts")

    assert response.status_code == 200
    sent = fake.requests[0]
    assert sent.url.path == "/v1/drafts"
    assert sent.url.params["status"] == "draft"
    assert sent.headers["X-Organization-Id"] == str(ORG)
    html = response.text
    assert f'href="/drafts/{DRAFT_ID}"' in html
    assert SUBJECT in html
    assert 'href="/drafts?cursor=c2"' in html
    assert 'role="status"' in html and 'aria-live="polite"' in html


async def test_root_redirects_to_the_queue(ui: httpx.AsyncClient) -> None:
    response = await ui.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/drafts"


async def test_detail_shows_email_summary_facts_and_citation_beside_its_sentence(
    ui: httpx.AsyncClient,
) -> None:
    response = await ui.get(f"/drafts/{DRAFT_ID}")

    assert response.status_code == 200
    html = response.text
    assert "Hello, where is my order ORD-82915? Thanks, Alice" in html
    assert "Alice asks for the delivery status of ORD-82915." in html
    marked = '<mark class="fact" title="Business data: ORD-82915">ORD-82915</mark>'
    assert f"{marked} was dispatched" in html
    assert html.index("our warehouse.") < html.index('href="#cite-1"')
    assert "Returns policy" in html
    assert "hx-vals='js:{review_ms: reviewElapsedMs()}'" in html
    assert 'name="rendered_at_ms"' in html


async def test_approve_sends_review_ms_and_announces_the_result(
    ui: httpx.AsyncClient, fake: FakeV1
) -> None:
    form = {
        "body": BODY,
        "original_body": BODY,
        "review_ms": "4200",
        "rendered_at_ms": "1",
        "reviewer": " Quan ",
    }
    response = await ui.post(f"/drafts/{DRAFT_ID}/approve", data=form, headers=HX)

    assert response.status_code == 200
    assert fake.calls("PATCH") == []
    approve = fake.calls("POST")[0]
    assert approve.url.path == f"/v1/drafts/{DRAFT_ID}/approve"
    assert json.loads(approve.content) == {"review_ms": 4200, "reviewer": "Quan"}
    assert 'hx-swap-oob="innerHTML"' in response.text
    assert "Draft approved." in response.text
    assert "This draft was approved." in response.text


async def test_an_edited_body_is_patched_before_the_approval(
    ui: httpx.AsyncClient, fake: FakeV1
) -> None:
    edited = "Your order ORD-82915 left today.\r\nThanks."
    form = {"body": edited, "original_body": BODY, "review_ms": "900", "rendered_at_ms": "1"}
    response = await ui.post(f"/drafts/{DRAFT_ID}/approve", data=form, headers=HX)

    assert response.status_code == 200
    methods = [r.method for r in fake.requests if r.url.path.startswith("/v1/drafts/")]
    assert methods[:2] == ["PATCH", "POST"]
    assert json.loads(fake.calls("PATCH")[0].content) == {
        "body": "Your order ORD-82915 left today.\nThanks."
    }
    assert json.loads(fake.calls("POST")[0].content) == {"review_ms": 900, "reviewer": None}


async def test_save_changes_patches_without_a_decision(ui: httpx.AsyncClient, fake: FakeV1) -> None:
    response = await ui.post(
        f"/drafts/{DRAFT_ID}/edit", data={"body": "New text.", "review_ms": "10"}, headers=HX
    )

    assert response.status_code == 200
    assert json.loads(fake.calls("PATCH")[0].content) == {"body": "New text."}
    assert fake.calls("POST") == []
    assert "Changes saved." in response.text


async def test_reject_sends_the_reason(ui: httpx.AsyncClient, fake: FakeV1) -> None:
    form = {"comment": " Wrong order. ", "review_ms": "900", "rendered_at_ms": "1"}
    response = await ui.post(f"/drafts/{DRAFT_ID}/reject", data=form, headers=HX)

    assert response.status_code == 200
    reject = fake.calls("POST")[0]
    assert reject.url.path == f"/v1/drafts/{DRAFT_ID}/reject"
    assert json.loads(reject.content) == {
        "review_ms": 900,
        "reviewer": None,
        "comment": "Wrong order.",
    }
    assert "Draft rejected." in response.text


async def test_without_javascript_a_decision_redirects_back_with_a_notice(
    ui: httpx.AsyncClient,
) -> None:
    response = await ui.post(
        f"/drafts/{DRAFT_ID}/approve", data={"body": BODY, "original_body": BODY}
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/drafts/{DRAFT_ID}?notice=approved"

    page = await ui.get(response.headers["location"])
    assert "Draft approved." in page.text


def test_review_ms_prefers_the_client_measurement_then_the_render_time() -> None:
    assert resolve_review_ms("4200", "1000", now=5000) == 4200
    assert resolve_review_ms(None, "1000", now=5000) == 4000
    assert resolve_review_ms("", "1000", now=5000) == 4000
    assert resolve_review_ms("-3", "1000", now=5000) == 4000
    assert resolve_review_ms("abc", None, now=5000) == 0
    assert resolve_review_ms(None, "9000", now=5000) == 0


async def test_an_api_conflict_on_a_decision_is_announced_and_keeps_the_panel(
    ui: httpx.AsyncClient, fake: FakeV1
) -> None:
    fake.approve_error = (409, {"error": "Draft is not pending", "code": "DRAFT_NOT_PENDING"})
    response = await ui.post(
        f"/drafts/{DRAFT_ID}/approve",
        data={"body": BODY, "original_body": BODY, "review_ms": "5"},
        headers=HX,
    )

    assert response.status_code == 200
    assert response.headers["HX-Reswap"] == "none"
    assert "Error: Draft is not pending" in response.text


async def test_an_unknown_draft_renders_a_not_found_page(ui: httpx.AsyncClient) -> None:
    response = await ui.get(f"/drafts/{uuid4()}")
    assert response.status_code == 404
    assert "Draft not found" in response.text


async def test_missing_organization_renders_a_setup_error_and_calls_nothing(
    fake: FakeV1,
) -> None:
    app = create_app(
        _settings(org=None), transport=httpx.MockTransport(fake.handler), configure_logging=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ui"
    ) as client:
        response = await client.get("/drafts")

    assert response.status_code == 503
    assert "FRONTEND__ORGANIZATION_ID" in response.text
    assert fake.requests == []


async def test_an_unreachable_api_renders_a_502_page() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app = create_app(_settings(), transport=httpx.MockTransport(refuse), configure_logging=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ui"
    ) as client:
        response = await client.get("/drafts")

    assert response.status_code == 502
    assert "The API could not be reached" in response.text


async def test_health_and_vendored_htmx_are_served(ui: httpx.AsyncClient) -> None:
    assert (await ui.get("/healthz")).status_code == 200
    script = await ui.get("/static/htmx.min.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    pinned = hashlib.sha256((STATIC_DIR / "htmx.min.js").read_bytes()).hexdigest()
    assert pinned == HTMX_SHA256, "htmx.min.js must be the vendored 2.0.11 release"
