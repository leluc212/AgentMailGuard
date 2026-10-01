"""Core pagination models and utilities.

Provides generic pagination models and helpers according to R23.6 so that
both services and core/database packages can handle paginated datasets
without circular or architectural boundary violations.
"""

import base64
import json
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class PageParams(BaseModel):
    """Pagination query parameters defining limit and offset."""

    limit: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Maximum number of items to return (1-100).",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based offset of the first item to return.",
    )


class PaginatedResponse[T](BaseModel):
    """Standard generic wrapper for paginated list endpoints."""

    items: list[T] = Field(description="List of records for the requested page.")
    total_count: int = Field(ge=0, description="Total matching records across all pages.")
    limit: int = Field(ge=1, description="Requested limit for this page.")
    offset: int = Field(ge=0, description="Requested offset for this page.")
    has_more: bool = Field(description="Indicates if additional records exist after this page.")


def paginate[T](
    items: Sequence[T],
    total_count: int,
    params: PageParams,
) -> PaginatedResponse[T]:
    """Construct a PaginatedResponse with computed has_more flag."""
    item_list = list(items)
    has_more = (params.offset + len(item_list)) < total_count
    return PaginatedResponse[T](
        items=item_list,
        total_count=total_count,
        limit=params.limit,
        offset=params.offset,
        has_more=has_more,
    )


class InvalidCursorError(ValueError):
    """A pagination cursor that :func:`encode_cursor` did not produce (R23.6)."""


def encode_cursor(created_at: datetime, item_id: UUID) -> str:
    """Opaque keyset cursor for ``ORDER BY created_at DESC, id DESC`` pages (R23.6).

    The cursor names the last item of a page; the next page holds items strictly
    before it. base64url without padding, so it is safe in a query string.
    """
    if created_at.tzinfo is None:
        raise ValueError("cursor timestamps must be timezone-aware")
    raw = json.dumps({"t": created_at.isoformat(), "id": str(item_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Inverse of :func:`encode_cursor`; anything else raises InvalidCursorError."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(data["t"])
        item_id = UUID(data["id"])
    except (ValueError, KeyError, TypeError) as err:
        raise InvalidCursorError(f"Invalid pagination cursor: {cursor!r}") from err
    if created_at.tzinfo is None:
        raise InvalidCursorError(f"Invalid pagination cursor: {cursor!r}")
    return created_at, item_id
