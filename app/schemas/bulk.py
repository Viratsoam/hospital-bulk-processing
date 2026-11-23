"""Schemas for the bulk processing endpoint response.

The response mirrors the stored :class:`BatchRecord` with one deliberate
difference: per-row outcomes are exposed as ``results`` (the stored field
name is ``rows``), so the API contract is stable independent of storage.
"""

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.batch import BatchRecord, BatchStatus, RowResult


class BulkProcessingResponse(BaseModel):
    """Outcome of one bulk CSV processing run.

    A 200 response is returned for *every* completed run, including partial
    failures and activation problems: those are business outcomes described
    by ``status`` and ``results``, not server errors.
    """

    batch_id: UUID = Field(..., description="UUID shared with every creation request.")
    status: BatchStatus = Field(
        ..., description="Terminal batch state; see the BatchStatus documentation."
    )
    total_records: int = Field(..., description="Number of submitted records.")
    processed_records: int = Field(
        ..., description="Records that reached a terminal state."
    )
    successful_records: int = Field(
        ..., description="Records confirmed created upstream."
    )
    failed_records: int = Field(
        ..., description="Records with a definitive creation failure."
    )
    unknown_records: int = Field(
        ..., description="Records whose creation could not be confirmed."
    )
    processing_time: Optional[float] = Field(
        default=None, description="Seconds from start to completion, when finished."
    )
    batch_activated: bool = Field(
        ..., description="True only when the upstream activation succeeded."
    )
    activated_count: Optional[int] = Field(
        default=None,
        description="Upstream-confirmed activation count, on success.",
    )
    error_summary: Optional[str] = Field(
        default=None,
        description="What requires attention, when the batch did not complete.",
    )
    results: List[RowResult] = Field(
        ..., description="Per-row outcomes, in original CSV row order."
    )

    @classmethod
    def from_batch(cls, record: BatchRecord) -> "BulkProcessingResponse":
        """Project a stored batch record onto the API response shape."""
        return cls(
            batch_id=record.batch_id,
            status=record.status,
            total_records=record.total_records,
            processed_records=record.processed_records,
            successful_records=record.successful_records,
            failed_records=record.failed_records,
            unknown_records=record.unknown_records,
            processing_time=record.processing_time,
            batch_activated=record.batch_activated,
            activated_count=record.activated_count,
            error_summary=record.error_summary,
            results=record.rows,
        )
