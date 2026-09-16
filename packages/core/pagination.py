"""Core pagination models and utilities.

Provides generic pagination models and helpers according to R23.6 so that
both services and core/database packages can handle paginated datasets
without circular or architectural boundary violations.
"""

from collections.abc import Sequence

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
