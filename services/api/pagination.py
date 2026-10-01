"""API pagination dependency and re-exports.

Provides FastAPI-specific dependency extraction for pagination parameters,
re-exporting core models for ease of use in router endpoints.
"""

from typing import Annotated

from fastapi import Depends, Query

from packages.core.pagination import PageParams, PaginatedResponse, paginate

__all__ = [
    "PageParams",
    "PaginatedResponse",
    "PaginationParamsDep",
    "get_pagination_params",
    "paginate",
]


def get_pagination_params(
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=100,
            description="Number of items to return per page (1-100).",
        ),
    ] = 50,
    offset: Annotated[
        int,
        Query(
            ge=0,
            description="Zero-based offset of the first item to return.",
        ),
    ] = 0,
) -> PageParams:
    """Extract and validate pagination parameters from query string."""
    return PageParams(limit=limit, offset=offset)


PaginationParamsDep = Annotated[PageParams, Depends(get_pagination_params)]
