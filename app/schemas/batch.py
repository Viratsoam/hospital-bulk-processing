"""Schemas for batch processing state.

These models describe the lifecycle of one bulk-processing batch: a batch
record with per-row results, tracked from the moment a batch UUID is generated
until activation (or a failure that prevents it).

The models are frozen so that repository storage and API responses expose
immutable snapshots: a caller who receives a :class:`BatchRecord` cannot
corrupt tracked state by mutating it.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RowStatus(str, Enum):
    """Lifecycle of a single input row within a batch.

    ``pending`` and ``processing`` are transient; every processed row ends in
    one of ``created``, ``failed``, or ``unknown``.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    CREATED = "created"
    FAILED = "failed"
    UNKNOWN = "unknown"


class BatchStatus(str, Enum):
    """Terminal states of a batch.

    * ``processing`` — creation attempts are in flight.
    * ``completed`` — every row was confirmed created and the batch was
      activated.
    * ``partially_failed`` — at least one row failed or could not be
      reconciled to a confirmed creation; the batch was not activated.
    * ``created_not_activated`` — every row was confirmed created, but
      activation failed definitively; the upstream records are preserved.
    * ``requires_attention`` — the outcome cannot be settled automatically
      (ambiguous activation, or an upstream batch that does not match the
      submitted records); a human must inspect it.
    """

    PROCESSING = "processing"
    PARTIALLY_FAILED = "partially_failed"
    CREATED_NOT_ACTIVATED = "created_not_activated"
    REQUIRES_ATTENTION = "requires_attention"
    COMPLETED = "completed"


class RowResult(BaseModel):
    """The outcome for one input row.

    ``row_number`` preserves the original CSV line so results map back to the
    uploaded file even after concurrency reorders completion.
    """

    model_config = ConfigDict(frozen=True)

    row_number: int = Field(..., ge=1, description="Original CSV line number.")
    name: str = Field(..., min_length=1, description="Hospital name.")
    address: str = Field(..., min_length=1, description="Hospital address.")
    phone: Optional[str] = Field(default=None, description="Phone, when present.")
    status: RowStatus = Field(..., description="Terminal state of this row.")
    upstream_hospital_id: Optional[Union[int, str]] = Field(
        default=None,
        description=(
            "Upstream hospital ID from a definitive creation response. "
            "Confirmed-by-reconciliation rows have no attributable ID, "
            "because interchangeable duplicates cannot be matched to a "
            "specific upstream record."
        ),
    )
    error_message: Optional[str] = Field(
        default=None, description="Human-readable failure detail, when applicable."
    )


class BatchRecord(BaseModel):
    """The tracked state of one bulk-processing batch.

    Counts are derived from ``rows`` at persistence time: ``processed`` is the
    number of rows that reached a terminal state, and the three outcome counts
    partition the processed rows.
    """

    model_config = ConfigDict(frozen=True)

    batch_id: UUID = Field(..., description="UUID shared with every creation request.")
    status: BatchStatus = Field(..., description="Current lifecycle state.")
    total_records: int = Field(..., ge=0, description="Input row count.")
    processed_records: int = Field(
        default=0, ge=0, description="Rows that reached a terminal state."
    )
    successful_records: int = Field(
        default=0, ge=0, description="Rows confirmed created upstream."
    )
    failed_records: int = Field(
        default=0, ge=0, description="Rows with a definitive creation failure."
    )
    unknown_records: int = Field(
        default=0,
        ge=0,
        description="Rows whose creation could not be confirmed or refuted.",
    )
    started_at: datetime = Field(..., description="When processing began (UTC).")
    completed_at: Optional[datetime] = Field(
        default=None, description="When the batch reached a terminal state."
    )
    processing_time: Optional[float] = Field(
        default=None,
        description="Seconds from start to completion, when finished.",
    )
    batch_activated: bool = Field(
        default=False, description="True only when activation succeeded."
    )
    activated_count: Optional[int] = Field(
        default=None,
        description="Upstream-confirmed activation count, on success.",
    )
    error_summary: Optional[str] = Field(
        default=None,
        description="One-line summary of what requires attention, if anything.",
    )
    rows: List[RowResult] = Field(
        default_factory=list, description="Per-row outcomes, in input order."
    )
