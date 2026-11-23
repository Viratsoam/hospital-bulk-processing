"""CSV validation routes."""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.dependencies import provide_settings, provide_validation_service
from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.csv import CsvValidationResult
from app.services.validation_service import CsvValidationService

logger = get_logger(__name__)

router = APIRouter(prefix="/hospitals", tags=["csv"])


@router.post(
    "/validate",
    response_model=CsvValidationResult,
    summary="Validate a hospital CSV",
    description=(
        "Parses and validates an uploaded CSV without creating any hospitals "
        "and without calling the upstream Hospital Directory API.\n\n"
        "The response is always `200 OK` for a well-formed multipart request: "
        "an invalid CSV is a normal business outcome reported through "
        "`valid: false` plus a list of structured errors, not a transport "
        "failure.\n\n"
        "Limits: at most 20 hospital records per file."
    ),
    responses={
        200: {"description": "Validation completed; inspect the `valid` flag."},
        413: {"description": "The upload exceeds the configured byte limit."},
    },
)
async def validate_csv(
    file: UploadFile = File(..., description="CSV file to validate."),
    service: CsvValidationService = Depends(provide_validation_service),
    settings: Settings = Depends(provide_settings),
) -> CsvValidationResult:
    """Validate an uploaded CSV file.

    The file is read fully into memory before parsing. That is deliberate: the
    configured size limit is small, and validation must inspect the entire file
    before any downstream work begins.

    Args:
        file: Multipart file part named ``file``.
        service: CSV validation service.
        settings: Application settings supplying the size limit.

    Returns:
        Validation outcome with parsed records or structured errors.

    Raises:
        HTTPException: 413 when the upload exceeds the configured byte limit.

    Note:
        A *missing* ``file`` part is rejected by FastAPI itself with ``422``
        before this handler runs, because the parameter is declared required.
    """
    payload = await file.read()

    if len(payload) > settings.max_csv_size_bytes:
        # Reject before parsing so an oversized upload never reaches the parser.
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds the maximum allowed size of {0} bytes.".format(
                settings.max_csv_size_bytes
            ),
        )

    logger.info(
        "Validating CSV upload filename=%s bytes=%d", file.filename, len(payload)
    )
    result = service.validate_csv(payload)

    logger.info(
        "Validation result filename=%s valid=%s records=%d errors=%d",
        file.filename,
        result.valid,
        result.total_records,
        len(result.errors),
    )
    return result
