"""Batch lookup of many upload records by id, in one round trip.

Split out from ``management.py`` on purpose (see CLAUDE.md's "Single
Responsibility & Anti-Bloat" rule) -- ``management.py`` is already past the
~400-line threshold that calls for a new file (adding here would only make
that worse), and this route is a single, self-contained concern: fan a
listing page's many `GET /files/{id}` calls (one per photo, across many
parent records) into one request.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.routers.auth import verify_token
from app.schemas import FileBatchResponse
from app.services.metadata import MetadataError, get_metadata_store

logger = logging.getLogger(__name__)
router = APIRouter()

_STORE_UNAVAILABLE_DETAIL = "Metadata store unavailable"
_MAX_BATCH_IDS = 200


@router.get(
    "/files/batch",
    tags=["Files"],
    summary="Get multiple files by id",
    response_model=FileBatchResponse,
    response_model_exclude_unset=True,
)
async def get_files_batch(
    owner: Annotated[str, Depends(verify_token)],
    ids: Annotated[
        list[str],
        Query(
            min_length=1,
            max_length=_MAX_BATCH_IDS,
            description=(
                f"Repeat the param per id, e.g. ?ids=a&ids=b. Up to {_MAX_BATCH_IDS} per request."
            ),
        ),
    ],
):
    """Owner-scoped lookup of many records by id in one call.

    Unknown ids and ids owned by someone else are silently dropped from the
    result rather than 404ing the whole request -- existence never leaks,
    same as every other owner-scoped route. Order is not guaranteed to match
    `ids`; sort by id yourself if you need a stable order.

    **Registered before** ``GET /files/{file_id}`` in `app.main` on purpose:
    Starlette matches routes in registration order, and `{file_id}` would
    otherwise swallow a literal `/files/batch` request as `file_id="batch"`.
    """
    store = await get_metadata_store()
    try:
        records = await store.get_many(owner, ids)
    except MetadataError as exc:
        logger.exception("Failed to batch-load uploads")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STORE_UNAVAILABLE_DETAIL
        ) from exc
    return {"files": [record.to_public() for record in records]}
