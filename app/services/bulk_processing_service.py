"""Bulk processing engine for hospital creation batches.

This service turns a list of already-validated :class:`HospitalRow` records
into an upstream batch: it generates a batch UUID, submits every record to the
Hospital Directory API concurrently, reconciles ambiguous creation outcomes,
verifies the upstream batch state, and activates the batch — exactly once,
and only when safe. It persists one :class:`BatchRecord` per run.

Safety invariants (the upstream API has no idempotency key beyond
``creation_batch_id``):

* A creation that ends ambiguously (read timeout, ambiguous 5xx) is **never**
  retried blindly; it is confirmed — or not — through a batch retrieval that
  matches on ``(name, address, phone)`` occurrence counts.
* A batch is activated only when every input row is confirmed created *and*
  the upstream batch holds exactly the expected records, all inactive.
* ``activate_batch`` is called at most once per run; an ambiguous activation
  result is never retried and is reported for manual intervention.
* Upstream records are never deleted, and no hospital is ever added to a
  batch that already contains active records.

The service works on a local, mutable list of :class:`RowResult` and
reconstructs an immutable :class:`BatchRecord` snapshot only at each
persistence point, so the repository always receives a stable value object.
"""

import asyncio
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple, Union
from uuid import UUID

from app.clients.hospital_api_client import (
    HospitalAPIClient,
    HospitalAPIError,
    UpstreamBatchState,
    UpstreamHospital,
)
from app.core.config import Settings
from app.core.logging import get_logger
from app.repositories.batch_repository import BatchRepository
from app.schemas.batch import BatchRecord, BatchStatus, RowResult, RowStatus
from app.schemas.csv import HospitalRow

logger = get_logger(__name__)

#: The three record fields used to match an upstream hospital back to a
#: submitted CSV row.
RecordKey = Tuple[str, str, Optional[str]]


def _record_key(name: str, address: str, phone: Optional[str]) -> RecordKey:
    """Build the occurrence-count matching key for a submitted record."""
    return (name, address, phone)


def _upstream_key(upstream: UpstreamHospital) -> RecordKey:
    """Build the matching key for an upstream hospital record."""
    return _record_key(upstream.name or "", upstream.address or "", upstream.phone)


@dataclass(frozen=True)
class _RowOutcome:
    """The result of one creation attempt for a row."""

    index: int
    status: RowStatus
    upstream_hospital_id: Optional[Union[int, str]]
    error_message: Optional[str]


class BulkProcessingService:
    """Creates, reconciles, and activates one hospital batch per run.

    The service is stateless apart from its collaborators; each
    :meth:`process_batch` call owns its own concurrency controls, so a
    service instance may be shared across concurrent runs.
    """

    def __init__(
        self,
        settings: Settings,
        hospital_api: HospitalAPIClient,
        repository: BatchRepository,
    ) -> None:
        """Initialize the service.

        Args:
            settings: Supplies the batch size limit and concurrency limit.
            hospital_api: The shared upstream API client.
            repository: Where batch records are persisted.
        """
        self._settings = settings
        self._hospital_api = hospital_api
        self._repository = repository

    # -- public API ---------------------------------------------------------

    async def process_batch(
        self, hospitals: Sequence[HospitalRow]
    ) -> BatchRecord:
        """Run the full batch workflow for a set of validated records.

        Args:
            hospitals: Already-validated records, in original CSV order.

        Returns:
            The finalized :class:`BatchRecord`, also stored in the repository.

        Raises:
            ValueError: If the input is empty or exceeds the configured
                maximum batch size.
        """
        if not hospitals:
            raise ValueError("A batch requires at least one hospital record.")
        if len(hospitals) > self._settings.max_hospitals_per_batch:
            raise ValueError(
                "Batch of {0} records exceeds the maximum of {1}.".format(
                    len(hospitals), self._settings.max_hospitals_per_batch
                )
            )

        batch_id: UUID = uuid.uuid4()
        started_at = datetime.now(timezone.utc)

        rows = [self._pending_row(hospital) for hospital in hospitals]
        record = self._snapshot(
            batch_id, BatchStatus.PROCESSING, rows, started_at
        )
        self._repository.save(record)
        logger.info(
            "Batch processing started batch_id=%s records=%d",
            batch_id,
            len(hospitals),
        )

        self._apply_outcomes(rows, await self._create_all(hospitals, batch_id))

        # Reconcile ambiguous creation outcomes via batch retrieval. The
        # retrieved state doubles as the activation-safety verification.
        state: Optional[UpstreamBatchState] = None
        if any(row.status is RowStatus.UNKNOWN for row in rows):
            state = await self._reconcile_unknowns(rows, batch_id)

        # Persist an intermediate state: even if the process dies before
        # activation, the repository shows which rows were created.
        record = self._snapshot(
            batch_id, BatchStatus.PROCESSING, rows, started_at
        )
        self._repository.save(record)

        counts = self._counts(rows)
        if counts.failed or counts.unknown:
            record = self._finish(
                batch_id,
                BatchStatus.PARTIALLY_FAILED,
                rows,
                started_at,
                error_summary=self._partial_failure_summary(rows),
            )
            self._repository.save(record)
            logger.warning(
                "Batch %s partially failed: %d failed, %d unknown of %d records.",
                batch_id,
                counts.failed,
                counts.unknown,
                len(hospitals),
            )
            return record

        # Every row is confirmed created: verify the upstream batch is safe
        # to activate before the single activation attempt.
        if state is None:
            state = await self._hospital_api.get_batch_hospitals(batch_id)
        problem = self._verify_upstream_batch(hospitals, state)
        if problem is not None:
            record = self._finish(
                batch_id,
                BatchStatus.REQUIRES_ATTENTION,
                rows,
                started_at,
                error_summary=problem,
            )
            self._repository.save(record)
            logger.warning("Batch %s requires attention: %s", batch_id, problem)
            return record

        try:
            activation = await self._hospital_api.activate_batch(batch_id)
            record = self._finish(
                batch_id,
                BatchStatus.COMPLETED,
                rows,
                started_at,
                error_summary=None,
                batch_activated=True,
                activated_count=activation.activated_count,
            )
        except HospitalAPIError as exc:
            if exc.ambiguous:
                status = BatchStatus.REQUIRES_ATTENTION
                detail = (
                    "Activation outcome is ambiguous ({0}). It may have "
                    "partially succeeded; inspect the batch before "
                    "retrying.".format(exc)
                )
            else:
                status = BatchStatus.CREATED_NOT_ACTIVATED
                detail = "Activation failed: {0}".format(exc)
            record = self._finish(
                batch_id, status, rows, started_at, error_summary=detail
            )

        self._repository.save(record)
        logger.info(
            "Batch %s finished status=%s succeeded=%d failed=%d unknown=%d",
            batch_id,
            record.status.value,
            record.successful_records,
            record.failed_records,
            record.unknown_records,
        )
        return record

    # -- creation phase ------------------------------------------------------

    async def _create_all(
        self, hospitals: Sequence[HospitalRow], batch_id: UUID
    ) -> List[_RowOutcome]:
        """Create every record concurrently, bounded by the configured limit.

        Every task is created up front and a single gather point collects all
        results with ``return_exceptions``: a failure in one task is recorded
        and can never cancel or delay the settlement of its siblings.
        """
        semaphore = asyncio.Semaphore(
            self._settings.hospital_api_max_concurrency
        )
        tasks = [
            asyncio.ensure_future(
                self._create_one(index, hospital, batch_id, semaphore)
            )
            for index, hospital in enumerate(hospitals)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        outcomes: List[_RowOutcome] = []
        for index, result in enumerate(results):
            if isinstance(result, _RowOutcome):
                outcomes.append(result)
            else:
                # A bug in our own task code, not one of the client's
                # normalized errors: record it as a failure without sinking
                # the rest of the batch.
                logger.exception(
                    "Unexpected error creating row %d batch_id=%s",
                    index,
                    batch_id,
                )
                outcomes.append(
                    _RowOutcome(
                        index=index,
                        status=RowStatus.FAILED,
                        upstream_hospital_id=None,
                        error_message="Unexpected internal error: {0}".format(result),
                    )
                )
        return outcomes

    async def _create_one(
        self,
        index: int,
        hospital: HospitalRow,
        batch_id: UUID,
        semaphore: asyncio.Semaphore,
    ) -> _RowOutcome:
        """Create one hospital, bounded by the shared concurrency semaphore."""
        async with semaphore:
            try:
                created = await self._hospital_api.create_hospital(
                    hospital.name,
                    hospital.address,
                    hospital.phone,
                    batch_id=batch_id,
                )
                return _RowOutcome(
                    index=index,
                    status=RowStatus.CREATED,
                    upstream_hospital_id=created.id,
                    error_message=None,
                )
            except HospitalAPIError as exc:
                if exc.ambiguous:
                    # The upstream may already hold the record; the
                    # reconciliation phase decides. Never retried here.
                    return _RowOutcome(
                        index=index,
                        status=RowStatus.UNKNOWN,
                        upstream_hospital_id=None,
                        error_message="Creation outcome ambiguous: {0}".format(exc),
                    )
                return _RowOutcome(
                    index=index,
                    status=RowStatus.FAILED,
                    upstream_hospital_id=None,
                    error_message=str(exc),
                )

    def _apply_outcomes(
        self, rows: List[RowResult], outcomes: Sequence[_RowOutcome]
    ) -> None:
        """Replace each row with its terminal creation outcome."""
        for outcome in outcomes:
            row = rows[outcome.index]
            rows[outcome.index] = row.model_copy(
                update={
                    "status": outcome.status,
                    "upstream_hospital_id": outcome.upstream_hospital_id,
                    "error_message": outcome.error_message,
                }
            )

    # -- reconciliation ------------------------------------------------------

    async def _reconcile_unknowns(
        self, rows: List[RowResult], batch_id: UUID
    ) -> Optional[UpstreamBatchState]:
        """Confirm ambiguous rows against the upstream batch, where possible.

        Each UNKNOWN row is matched to upstream hospitals by
        ``(name, address, phone)`` occurrence counts, so two submitted
        duplicates are both confirmed only when the upstream batch holds two
        matching records. Hospitals already confirmed by a definitive
        creation response are excluded from the matching pool, so ambiguous
        rows only claim records that are not already accounted for. A
        concrete upstream ID is attributed only when the match is
        unambiguous; interchangeable duplicates are confirmed but left
        without an ID.

        Args:
            rows: The working row list, updated in place.
            batch_id: The batch whose upstream state to retrieve.

        Returns:
            The retrieved upstream state, or ``None`` when retrieval failed
            (in which case no row could be confirmed).
        """
        try:
            state = await self._hospital_api.get_batch_hospitals(batch_id)
        except HospitalAPIError as exc:
            logger.warning(
                "Reconciliation retrieval failed batch_id=%s: %s", batch_id, exc
            )
            return None

        known_ids = {
            row.upstream_hospital_id
            for row in rows
            if row.upstream_hospital_id is not None
        }
        pool = [h for h in state.hospitals if h.id not in known_ids]
        pool_counts = Counter(_upstream_key(h) for h in pool)
        unknown_counts = Counter(
            _record_key(row.name, row.address, row.phone)
            for row in rows
            if row.status is RowStatus.UNKNOWN
        )

        confirmed = 0
        for index, row in enumerate(rows):
            if row.status is not RowStatus.UNKNOWN:
                continue
            key = _record_key(row.name, row.address, row.phone)
            if pool_counts.get(key, 0) < 1:
                rows[index] = row.model_copy(
                    update={
                        "error_message": (
                            "Creation could not be confirmed: the upstream "
                            "batch holds no unaccounted matching record for "
                            "this row. Reconcile manually."
                        )
                    }
                )
                continue

            pool_counts[key] -= 1
            upstream_id: Optional[Union[int, str]] = None
            if pool_counts[key] == 0 and unknown_counts.get(key, 0) == 1:
                matching = [
                    h
                    for h in pool
                    if _upstream_key(h) == key and h.id not in known_ids
                ]
                if len(matching) == 1:
                    upstream_id = matching[0].id
            rows[index] = row.model_copy(
                update={
                    "status": RowStatus.CREATED,
                    "upstream_hospital_id": upstream_id,
                    "error_message": None,
                }
            )
            confirmed += 1

        logger.info(
            "Reconciliation batch_id=%s confirmed=%d of %d unknown row(s).",
            batch_id,
            confirmed,
            sum(unknown_counts.values()),
        )
        return state

    # -- verification --------------------------------------------------------

    @staticmethod
    def _verify_upstream_batch(
        hospitals: Sequence[HospitalRow], state: UpstreamBatchState
    ) -> Optional[str]:
        """Verify the upstream batch is safe to activate.

        Checks, in order: exact record count, every expected record
        accounted for, and an explicit ``active == False`` flag on every
        hospital. The active-state check fails closed: a hospital whose
        ``active`` field is missing, null, or not a boolean cannot be
        proven inactive, so the batch must not be activated.

        Args:
            hospitals: The submitted records.
            state: The upstream batch as retrieved.

        Returns:
            ``None`` when activation is safe, otherwise the reason it is not.
        """
        if len(state.hospitals) != len(hospitals):
            return (
                "Upstream batch count mismatch: {0} record(s) present, "
                "{1} submitted. Do not add more hospitals to this batch; "
                "reconcile manually.".format(len(state.hospitals), len(hospitals))
            )

        expected = Counter(
            _record_key(h.name, h.address, h.phone) for h in hospitals
        )
        upstream = Counter(_upstream_key(h) for h in state.hospitals)
        for key, need in expected.items():
            if upstream.get(key, 0) < need:
                return (
                    "Expected record not accounted for in the upstream batch: "
                    "name={0!r} address={1!r} phone={2!r} "
                    "(need {3}, found {4}).".format(
                        key[0], key[1], key[2], need, upstream.get(key, 0)
                    )
                )

        # Fail closed on the active flag itself. Activation requires every
        # hospital to be explicitly, verifiably inactive: a hospital whose
        # upstream ``active`` field is a true boolean is checked first, then
        # any hospital whose field is missing, null, or not a boolean is
        # treated as unverifiable and blocks activation.
        already_active = [h for h in state.hospitals if h.raw.get("active") is True]
        if already_active:
            return (
                "Upstream batch already contains {0} active hospital(s); a "
                "mixed batch cannot be activated. Inspect and resolve "
                "manually.".format(len(already_active))
            )

        unverifiable = [
            h for h in state.hospitals if not isinstance(h.raw.get("active"), bool)
        ]
        if unverifiable:
            return (
                "Active-state verification failed: {0} hospital(s) lack an "
                "explicit boolean 'active' flag (the value is missing, "
                "null, or not a boolean), so the batch cannot be proven "
                "inactive. It will not be activated; inspect the upstream "
                "records manually.".format(len(unverifiable))
            )
        return None

    # -- record assembly ------------------------------------------------------

    @staticmethod
    def _pending_row(hospital: HospitalRow) -> RowResult:
        """The initial row state for one submitted record."""
        return RowResult(
            row_number=hospital.row,
            name=hospital.name,
            address=hospital.address,
            phone=hospital.phone,
            status=RowStatus.PENDING,
        )

    @staticmethod
    def _snapshot(
        batch_id: UUID,
        status: BatchStatus,
        rows: Sequence[RowResult],
        started_at: datetime,
        **extra: object,
    ) -> BatchRecord:
        """Build a fresh, immutable record reflecting the current rows.

        Row-count aggregates are recomputed each time, so a snapshot is
        self-consistent. ``extra`` carries terminal-state fields
        (``batch_activated``, ``activated_count``, ``error_summary``, ...).
        """
        counts = BulkProcessingService._counts(rows)
        return BatchRecord(
            batch_id=batch_id,
            status=status,
            total_records=len(rows),
            processed_records=counts.processed,
            successful_records=counts.created,
            failed_records=counts.failed,
            unknown_records=counts.unknown,
            started_at=started_at,
            rows=list(rows),
            **extra,
        )

    @staticmethod
    def _counts(rows: Sequence[RowResult]) -> "_RowCounts":
        """Partition the rows into their terminal-state buckets."""
        created = sum(1 for r in rows if r.status is RowStatus.CREATED)
        failed = sum(1 for r in rows if r.status is RowStatus.FAILED)
        unknown = sum(1 for r in rows if r.status is RowStatus.UNKNOWN)
        return _RowCounts(
            created=created, failed=failed, unknown=unknown, processed=created + failed + unknown
        )

    def _finish(
        self,
        batch_id: UUID,
        status: BatchStatus,
        rows: Sequence[RowResult],
        started_at: datetime,
        *,
        error_summary: Optional[str] = None,
        batch_activated: bool = False,
        activated_count: Optional[int] = None,
    ) -> BatchRecord:
        """Attach the terminal state and timing to a record snapshot."""
        record = self._snapshot(
            batch_id,
            status,
            rows,
            started_at,
            error_summary=error_summary,
            batch_activated=batch_activated,
            activated_count=activated_count,
        )
        completed_at = datetime.now(timezone.utc)
        return record.model_copy(
            update={
                "completed_at": completed_at,
                "processing_time": (completed_at - record.started_at).total_seconds(),
            }
        )

    @staticmethod
    def _partial_failure_summary(rows: Sequence[RowResult]) -> str:
        """One-line summary of a batch that did not fully succeed."""
        counts = BulkProcessingService._counts(rows)
        parts: List[str] = []
        if counts.failed:
            parts.append(
                "{0} record(s) failed to create".format(counts.failed)
            )
        if counts.unknown:
            parts.append(
                "{0} record(s) could not be confirmed".format(counts.unknown)
            )
        if not parts:
            parts.append("no records could be created")
        parts.append("of {0}".format(len(rows)))
        return " ".join(parts)


@dataclass(frozen=True)
class _RowCounts:
    """The partition of processed rows into terminal buckets."""

    created: int
    failed: int
    unknown: int
    processed: int
