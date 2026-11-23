"""Bulk hospital creation route.

``POST /hospitals/bulk`` accepts a CSV upload, validates it with the shared
CSV validation service, and hands the validated records to the bulk
processing engine, which owns creation, reconciliation, and activation.

The route is deliberately thin: it translates HTTP concerns (file type,
size, rejection of invalid input) and defers every workflow decision to the
service. A *completed* run — including partial failures, ambiguous
outcomes, and activation problems — is a 200 business result; only
request-level problems (bad file, invalid CSV, oversized upload) produce
4xx answers.
"""

import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.dependencies import (
    provide_bulk_processing_service,
    provide_settings,
    provide_validation_service,
)
from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.bulk import BulkProcessingResponse
from app.services.bulk_processing_service import BulkProcessingService
from app.services.validation_service import CsvValidationService

logger = get_logger(__name__)

router = APIRouter(prefix="/hospitals", tags=["bulk"])

#: Media types accepted for the upload, when no file extension is present.
_CSV_MEDIA_TYPES = {"text/csv", "application/csv", "application/vnd.ms-excel"}

#: Clients that do not set a media type, or send a generic one, are judged
#: by their extension alone.
_EXTENSIONLESS_MEDIA_TYPES = {"", "application/octet-stream"}


def _unsupported_type_problem(file: UploadFile) -> str:
    """The 415 detail for an upload that is not plausibly a CSV file.

    A ``.csv`` extension is trusted outright. Without an extension (or with
    any other extension), an explicit CSV media type is accepted; any other
    explicit media type is rejected.
    """
    filename = (file.filename or "").lower()
    extension = os.path.splitext(filename)[1]
    media_type = (file.content_type or "").lower().split(";")[0].strip()

    if extension == ".csv":
        return ""
    if extension:
        return (
            "Unsupported file type '{0}'. Upload a CSV file with a "
            "'.csv' extension.".format(file.filename)
        )
    if media_type in _CSV_MEDIA_TYPES or media_type in _EXTENSIONLESS_MEDIA_TYPES:
        return ""
    return (
        "Unsupported media type '{0}'. Upload a CSV file "
        "(text/csv).".format(file.content_type)
    )


@router.post(
    "/bulk",
    response_model=BulkProcessingResponse,
    summary="Create and activate a hospital batch from a CSV upload",
    description=(
        "Validates the uploaded CSV (columns `name`, `address`, optional "
        "`phone`; at most 20 records), creates every hospital in the "
        "upstream Hospital Directory API under one batch UUID, reconciles "
        "ambiguous outcomes, and activates the batch when safe.\n\n"
        "A 200 response is returned for every *completed* run, including "
        "partial failures and activation problems: inspect `status` and "
        "`results`. Invalid CSV is rejected with 400 before any upstream "
        "call is made."
    ),
    responses={
        200: {
            "description": "Batch processing completed (see `status` for the outcome).",
            "model": BulkProcessingResponse,
        },
        400: {"description": "The CSV is invalid; no hospitals were created."},
        413: {"description": "The upload exceeds the configured byte limit."},
        415: {"description": "The upload is not a CSV file."},
        422: {"description": "Missing file part or malformed multipart body."},
    },
)
async def process_bulk(
    file: UploadFile = File(
        ...,
        description=(
            "CSV file with columns `name`, `address`, and optional "
            "`phone`. Maximum 20 records."
        ),
    ),
    validator: CsvValidationService = Depends(provide_validation_service),
    service: BulkProcessingService = Depends(provide_bulk_processing_service),
    settings: Settings = Depends(provide_settings),
) -> BulkProcessingResponse:
    """Validate and process a hospital CSV as one batch.

    Args:
        file: Multipart file part named ``file``.
        validator: The shared CSV validation service.
        service: The bulk processing engine.
        settings: Application settings supplying the size limit.

    Returns:
        The batch outcome with per-row results.

    Raises:
        HTTPException: 400 for invalid CSV, 413 for an oversized upload,
            415 for an unsupported file type.
    """
    payload = await file.read()

    type_problem = _unsupported_type_problem(file)
    if type_problem:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=type_problem,
        )

    if len(payload) > settings.max_csv_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds the maximum allowed size of {0} bytes.".format(
                settings.max_csv_size_bytes
            ),
        )

    # Validation failures are rejected before any upstream call: the
    # structured payload mirrors the /validate response so callers see the
    # same exhaustive error reporting.
    result = validator.validate_csv(payload)
    if not result.valid:
        logger.info(
            "Bulk upload rejected by validation filename=%s records=%d errors=%d",
            file.filename,
            result.total_records,
            len(result.errors),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.model_dump(mode="json"),
        )

    record = await service.process_batch(result.hospitals)
    logger.info(
        "Bulk processing finished filename=%s batch_id=%s status=%s "
        "succeeded=%d failed=%d unknown=%d",
        file.filename,
        record.batch_id,
        record.status.value,
        record.successful_records,
        record.failed_records,
        record.unknown_records,
    )
    return BulkProcessingResponse.from_batch(record)
