"""Schemas for CSV validation.

These models are the contract between the CSV parsing layer and the HTTP layer.
They intentionally contain no I/O and no knowledge of CSV mechanics, so they can
be reused unchanged when bulk creation is implemented later.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ValidationErrorCode(str, Enum):
    """Machine-readable reasons a CSV upload can be rejected.

    Clients can branch on these values; the human-readable ``message`` may be
    reworded without breaking integrations.
    """

    EMPTY_FILE = "empty_file"
    HEADER_ONLY = "header_only"
    MISSING_HEADERS = "missing_headers"
    UNEXPECTED_HEADERS = "unexpected_headers"
    DUPLICATE_HEADERS = "duplicate_headers"
    MALFORMED_CSV = "malformed_csv"
    EMPTY_ROW = "empty_row"
    COLUMN_COUNT_MISMATCH = "column_count_mismatch"
    MISSING_NAME = "missing_name"
    MISSING_ADDRESS = "missing_address"
    TOO_MANY_RECORDS = "too_many_records"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_ENCODING = "invalid_encoding"


class HospitalRow(BaseModel):
    """A single validated hospital record read from a CSV row.

    Field values are already normalized (outer whitespace stripped) by the time
    an instance exists; constructing one from raw CSV cells is the parser's job.
    """

    model_config = ConfigDict(frozen=True)

    row: int = Field(
        ...,
        ge=1,
        description="Original CSV line number, for error reporting.",
    )
    name: str = Field(..., min_length=1, description="Hospital name.")
    address: str = Field(..., min_length=1, description="Hospital address.")
    phone: Optional[str] = Field(
        default=None,
        description="Phone number. Absent, empty, or whitespace-only becomes null.",
    )


class ValidationError(BaseModel):
    """A single structured validation problem.

    ``row`` is ``None`` for problems that concern the document as a whole
    (missing headers, too many records) and set for row-level problems.
    """

    row: Optional[int] = Field(
        default=None,
        description="1-based CSV line number, or null for document-level errors.",
    )
    field: Optional[str] = Field(
        default=None,
        description="Offending column name, when the error is field-specific.",
    )
    code: ValidationErrorCode = Field(..., description="Stable machine-readable code.")
    message: str = Field(..., description="Human-readable explanation.")


class CsvValidationResult(BaseModel):
    """Outcome of validating a CSV upload.

    ``hospitals`` is only populated when ``valid`` is true. On failure the
    ``errors`` list is exhaustive rather than first-error-only, so a caller can
    fix every problem in one pass.
    """

    valid: bool = Field(..., description="True only if the whole file is acceptable.")
    total_records: int = Field(
        ...,
        ge=0,
        description="Number of data rows that yielded a hospital record.",
    )
    hospitals: List[HospitalRow] = Field(
        default_factory=list,
        description="Parsed records. Empty when the file is invalid.",
    )
    errors: List[ValidationError] = Field(
        default_factory=list,
        description="Every problem found. Empty when the file is valid.",
    )


class HealthResponse(BaseModel):
    """Response body for the health-check endpoint."""

    status: str = Field(..., description="Always 'ok' when the service is up.")
    service: str = Field(..., description="Service name from configuration.")
    version: str = Field(..., description="Running application version.")
    environment: str = Field(..., description="Deployment environment name.")
