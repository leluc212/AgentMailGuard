"""API request and response schemas."""

from services.api.schemas.mailboxes import (
    MailboxResponse,
    ResyncRequest,
    ResyncResponse,
    TimeWindow,
)
from services.api.schemas.messages import (
    AttachmentSummaryResponse,
    EmailAddressResponse,
    MessageDetailResponse,
)
from services.api.schemas.threads import (
    MessageSummaryInThread,
    ThreadDetailResponse,
    ThreadSummaryResponse,
)

__all__ = [
    "AttachmentSummaryResponse",
    "EmailAddressResponse",
    "MailboxResponse",
    "MessageDetailResponse",
    "MessageSummaryInThread",
    "ResyncRequest",
    "ResyncResponse",
    "ThreadDetailResponse",
    "ThreadSummaryResponse",
    "TimeWindow",
]
