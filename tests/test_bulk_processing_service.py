"""Tests for the bulk processing engine.

The upstream API client is replaced with a programmable fake
(:class:`FakeHospitalAPI`): no test makes a network call. The fake mimics the
client's surface — including its normalized error classes and their
``ambiguous`` flag — so the service's reconciliation and activation-safety
logic runs against realistic outcomes.
"""

import asyncio
from typing import Callable, Dict, List, Optional, Sequence, Union
from uuid import UUID

import pytest

from app.clients.hospital_api_client import (
    ActivationResult,
    BatchActivationError,
    BatchRetrievalError,
    HospitalAPIError,
    HospitalCreationError,
    UpstreamBatchState,
    UpstreamErrorKind,
    UpstreamHospital,
)
from app.core.config import Settings
from app.repositories.batch_repository import (
    InMemoryBatchRepository,
)
from app.schemas.batch import BatchRecord, BatchStatus, RowStatus
from app.schemas.csv import HospitalRow
from app.services.bulk_processing_service import BulkProcessingService


#: Sentinel meaning "do not include the ``active`` key in the raw payload".
ACTIVE_MISSING = object()


def make_hospital(
    id,
    name: str,
    address: str,
    phone: Optional[str] = None,
    active: Union[bool, None, str, object] = False,
) -> UpstreamHospital:
    """An upstream hospital record with a realistic raw payload.

    ``active`` mirrors the upstream field exactly: pass a boolean or
    ``None`` to include the key with that value, or ``ACTIVE_MISSING`` to
    omit the key entirely.
    """
    raw = {
        "id": id,
        "name": name,
        "address": address,
        "phone": phone,
        "creation_batch_id": None,
    }
    if active is not ACTIVE_MISSING:
        raw["active"] = active
    return UpstreamHospital(id=id, name=name, address=address, phone=phone, raw=raw)


def make_state(
    batch_id: Union[UUID, str], hospitals: Sequence[UpstreamHospital]
) -> UpstreamBatchState:
    """An upstream batch state wrapping hospital records."""
    records = list(hospitals)
    return UpstreamBatchState(
        batch_id=str(batch_id), hospitals=records, raw=records
    )


def make_rows(*rows: tuple, start_line: int = 2) -> List[HospitalRow]:
    """Build validated input records from ``(name, address, phone)`` tuples.

    Args:
        rows: One tuple per record; duplicates are preserved as separate rows.
        start_line: The CSV line of the first record (the header is line 1).
    """
    return [
        HospitalRow(row=start_line + index, name=name, address=address, phone=phone)
        for index, (name, address, phone) in enumerate(rows)
    ]


def make_failure(
    status_code: int, ambiguous: bool, operation: str
) -> HospitalAPIError:
    """A normalized client error for a given status, with a flag set."""
    kind = {
        400: UpstreamErrorKind.BAD_REQUEST,
        404: UpstreamErrorKind.NOT_FOUND,
        422: UpstreamErrorKind.VALIDATION_ERROR,
        500: UpstreamErrorKind.SERVER_ERROR,
        504: UpstreamErrorKind.SERVER_ERROR,
    }[status_code]
    message = "HTTP {0}".format(status_code)
    if operation == "create":
        return HospitalCreationError(
            kind, message, ambiguous=ambiguous, status_code=status_code
        )
    if operation == "activate":
        return BatchActivationError(
            kind, message, ambiguous=ambiguous, status_code=status_code
        )
    return BatchRetrievalError(
        kind, message, ambiguous=ambiguous, status_code=status_code
    )


def ok_activate(count: int, batch_id: UUID) -> ActivationResult:
    payload = {"activated_count": count, "message": "activated"}
    return ActivationResult(
        batch_id=str(batch_id),
        activated_count=count,
        message="activated",
        raw=payload,
    )


class FakeHospitalAPI:
    """A programmable stand-in for :class:`HospitalAPIClient`.

    The test configures three hooks:

    * ``create_results`` maps ``(name, address, phone)`` to either an
      :class:`UpstreamHospital` to return or an exception to raise. Ambiguous
      creation failures are seeded with a
      :class:`HospitalCreationError` of ``ambiguous=True``.
    * ``get_batch_hospitals_fn`` receives the runtime batch id and returns a
      :class:`UpstreamBatchState` (or raises) — used for both the
      reconciliation retrieval and the activation-safety verification.
    * ``activate_batch_fn`` receives the runtime batch id and returns an
      :class:`ActivationResult` (or raises).

    ``max_in_flight`` tracks the peak number of concurrent create calls, to
    assert the semaphore bound.
    """

    def __init__(self) -> None:
        self.create_results: Dict[tuple, Union[UpstreamHospital, Exception]] = {}
        self.get_batch_hospitals_fn: Optional[
            Callable[[UUID], Union[UpstreamBatchState, "UpstreamStateFn"]]
        ] = None
        self.activate_batch_fn: Optional[
            Callable[[UUID], Union[ActivationResult, "ActivationFn"]]
        ] = None
        self.get_error: Optional[Exception] = None
        self.activate_error: Optional[Exception] = None

        self.create_calls: List[tuple] = []
        self.get_calls: List[UUID] = []
        self.activate_calls: List[UUID] = []

        self.in_flight = 0
        self.max_in_flight = 0

    @staticmethod
    def _as_uuid(batch_id: Union[str, UUID]) -> UUID:
        """Normalize a batch id to a UUID without double-wrapping."""
        return batch_id if isinstance(batch_id, UUID) else UUID(batch_id)

    # -- HospitalAPIClient surface ------------------------------------------

    async def create_hospital(
        self,
        name: str,
        address: str,
        phone: Optional[str] = None,
        *,
        batch_id: Union[str, UUID],
    ) -> UpstreamHospital:
        key = (name, address, phone)
        self.create_calls.append((key, str(batch_id)))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
        finally:
            self.in_flight -= 1
        result = self.create_results.get(key)
        if result is None:
            raise AssertionError(
                "create_hospital called unexpectedly for {0!r}".format(key)
            )
        if isinstance(result, Exception):
            raise result
        return result

    async def get_batch_hospitals(
        self, batch_id: Union[str, UUID]
    ) -> UpstreamBatchState:
        normalized = self._as_uuid(batch_id)
        self.get_calls.append(normalized)
        if self.get_error is not None:
            raise self.get_error
        if self.get_batch_hospitals_fn is None:
            raise AssertionError(
                "get_batch_hospitals called unexpectedly for {0!r}".format(batch_id)
            )
        return self.get_batch_hospitals_fn(normalized)

    async def activate_batch(self, batch_id: Union[str, UUID]) -> ActivationResult:
        normalized = self._as_uuid(batch_id)
        self.activate_calls.append(normalized)
        if self.activate_error is not None:
            raise self.activate_error
        if self.activate_batch_fn is None:
            raise AssertionError(
                "activate_batch called unexpectedly for {0!r}".format(batch_id)
            )
        return self.activate_batch_fn(normalized)


# Type aliases for the callable hooks (kept as forward strings in the fake).
UpstreamStateFn = Callable[[UUID], UpstreamBatchState]
ActivationFn = Callable[[UUID], ActivationResult]


class RecordingRepository(InMemoryBatchRepository):
    """A repository that records the status of every save, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.save_statuses: List[BatchStatus] = []

    def save(self, batch: BatchRecord) -> None:
        self.save_statuses.append(batch.status)
        super().save(batch)


# -- fixtures ----------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    return Settings(max_hospitals_per_batch=20, hospital_api_max_concurrency=5)


@pytest.fixture()
def fake_api() -> FakeHospitalAPI:
    return FakeHospitalAPI()


@pytest.fixture()
def repository() -> RecordingRepository:
    return RecordingRepository()


@pytest.fixture()
def service(
    settings: Settings, fake_api: FakeHospitalAPI, repository: RecordingRepository
) -> BulkProcessingService:
    return BulkProcessingService(settings, fake_api, repository)


def _wire_state(fake_api: FakeHospitalAPI, hospitals: Sequence[UpstreamHospital]) -> None:
    """Point the fake's batch retrieval at a fixed set of upstream records."""
    fake_api.get_batch_hospitals_fn = lambda batch_id: make_state(
        batch_id, list(hospitals)
    )


def _wire_activation(fake_api: FakeHospitalAPI, count: int) -> None:
    fake_api.activate_batch_fn = lambda batch_id: ok_activate(count, batch_id)


# -- the golden path ---------------------------------------------------------


async def test_all_created_and_batch_activated(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(
        ("St. Mary", "1 Hill St", "555-0101"),
        ("Grace", "2 Lake Rd", None),
        ("Riverside", "3 River Ave", "555-0103"),
    )
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", "555-0101"): make_hospital(
            1, "St. Mary", "1 Hill St", "555-0101"
        ),
        ("Grace", "2 Lake Rd", None): make_hospital(2, "Grace", "2 Lake Rd"),
        ("Riverside", "3 River Ave", "555-0103"): make_hospital(
            3, "Riverside", "3 River Ave", "555-0103"
        ),
    }
    _wire_state(
        fake_api,
        [
            make_hospital(1, "St. Mary", "1 Hill St", "555-0101"),
            make_hospital(2, "Grace", "2 Lake Rd"),
            make_hospital(3, "Riverside", "3 River Ave", "555-0103"),
        ],
    )
    _wire_activation(fake_api, 3)

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.COMPLETED
    assert record.batch_activated is True
    assert record.activated_count == 3
    assert record.total_records == 3
    assert record.processed_records == 3
    assert record.successful_records == 3
    assert record.failed_records == 0
    assert record.unknown_records == 0
    assert record.error_summary is None
    assert record.completed_at is not None
    assert record.processing_time is not None and record.processing_time >= 0
    assert [row.upstream_hospital_id for row in record.rows] == [1, 2, 3]
    assert all(row.status is RowStatus.CREATED for row in record.rows)

    # Every creation request carried the same generated batch UUID, and the
    # batch was activated exactly once.
    batch_ids = {batch for _, batch in fake_api.create_calls}
    assert batch_ids == {str(record.batch_id)}
    assert len(fake_api.activate_calls) == 1
    assert fake_api.activate_calls[0] == record.batch_id


# -- partial and total failure ------------------------------------------------


async def test_partial_creation_failure(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None), ("Broken", "4 Nowhere Rd", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
        ("Broken", "4 Nowhere Rd", None): make_failure(400, False, "create"),
    }
    _wire_state(fake_api, [make_hospital(1, "St. Mary", "1 Hill St")])

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.PARTIALLY_FAILED
    assert record.successful_records == 1
    assert record.failed_records == 1
    assert record.unknown_records == 0
    assert record.batch_activated is False
    assert fake_api.activate_calls == []
    assert record.error_summary is not None and "failed" in record.error_summary


async def test_all_creation_failures(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None), ("Grace", "2 Lake Rd", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_failure(422, False, "create"),
        ("Grace", "2 Lake Rd", None): make_failure(500, False, "create"),
    }
    _wire_state(fake_api, [])

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.PARTIALLY_FAILED
    assert record.successful_records == 0
    assert record.failed_records == 2
    assert record.batch_activated is False
    assert fake_api.activate_calls == []


async def test_independent_task_failures_do_not_sink_the_batch(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(
        ("Fail A", "1 A St", None),
        ("Good B", "2 B St", None),
        ("Fail C", "3 C St", None),
    )
    fake_api.create_results = {
        ("Fail A", "1 A St", None): make_failure(400, False, "create"),
        ("Good B", "2 B St", None): make_hospital(1, "Good B", "2 B St"),
        ("Fail C", "3 C St", None): make_failure(400, False, "create"),
    }
    _wire_state(fake_api, [make_hospital(1, "Good B", "2 B St")])

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.PARTIALLY_FAILED
    assert record.successful_records == 1
    assert record.failed_records == 2
    assert [row.status for row in record.rows] == [
        RowStatus.FAILED,
        RowStatus.CREATED,
        RowStatus.FAILED,
    ]
    assert record.rows[1].upstream_hospital_id == 1


# -- duplicate records ---------------------------------------------------------


async def test_duplicate_records_are_preserved(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(
        ("Same", "1 Place", None),
        ("Same", "1 Place", None),
        ("Other", "2 Place", None),
    )
    fake_api.create_results = {
        ("Same", "1 Place", None): make_hospital(1, "Same", "1 Place"),
        ("Other", "2 Place", None): make_hospital(2, "Other", "2 Place"),
    }
    _wire_state(
        fake_api,
        [
            make_hospital(1, "Same", "1 Place"),
            make_hospital(3, "Same", "1 Place"),
            make_hospital(2, "Other", "2 Place"),
        ],
    )
    _wire_activation(fake_api, 3)

    record = await service.process_batch(hospitals)

    # Duplicates are distinct rows, never deduplicated: three in, three
    # submitted, a batch of three activated.
    assert record.total_records == 3
    assert record.successful_records == 3
    assert record.status is BatchStatus.COMPLETED
    assert record.activated_count == 3
    assert len(fake_api.create_calls) == 3
    duplicates = [c for c in fake_api.create_calls if c[0] == ("Same", "1 Place", None)]
    assert len(duplicates) == 2


# -- ambiguous creation and reconciliation -------------------------------------


async def test_ambiguous_creation_reconciled(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None), ("Grace", "2 Lake Rd", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
        ("Grace", "2 Lake Rd", None): make_failure(504, True, "create"),
    }
    _wire_state(
        fake_api,
        [
            make_hospital(1, "St. Mary", "1 Hill St"),
            make_hospital(2, "Grace", "2 Lake Rd"),
        ],
    )
    _wire_activation(fake_api, 2)

    record = await service.process_batch(hospitals)

    grace = record.rows[1]
    assert record.status is BatchStatus.COMPLETED
    assert record.successful_records == 2
    assert record.unknown_records == 0
    # The ambiguous row was confirmed by reconciliation and, being the only
    # ambiguous occurrence of its key, got a concrete upstream ID.
    assert grace.status is RowStatus.CREATED
    assert grace.upstream_hospital_id == 2
    assert grace.error_message is None


async def test_ambiguous_creation_cannot_be_reconciled(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None), ("Grace", "2 Lake Rd", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
        ("Grace", "2 Lake Rd", None): make_failure(504, True, "create"),
    }
    # The upstream holds only the definitively-created record: the ambiguous
    # row cannot be confirmed.
    _wire_state(fake_api, [make_hospital(1, "St. Mary", "1 Hill St")])

    record = await service.process_batch(hospitals)

    grace = record.rows[1]
    assert record.status is BatchStatus.PARTIALLY_FAILED
    assert record.successful_records == 1
    assert record.unknown_records == 1
    assert grace.status is RowStatus.UNKNOWN
    assert grace.error_message is not None and "confirmed" in grace.error_message
    assert fake_api.activate_calls == []


async def test_ambiguous_creation_when_retrieval_fails_stays_unknown(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None), ("Grace", "2 Lake Rd", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
        ("Grace", "2 Lake Rd", None): make_failure(504, True, "create"),
    }
    fake_api.get_error = make_failure(500, False, "get")

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.PARTIALLY_FAILED
    assert record.rows[1].status is RowStatus.UNKNOWN
    assert record.unknown_records == 1
    assert fake_api.activate_calls == []


async def test_ambiguous_duplicate_handles_occurrence_counts(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("Same", "1 Place", None), ("Same", "1 Place", None))
    fake_api.create_results = {
        ("Same", "1 Place", None): make_failure(504, True, "create"),
    }
    # The upstream holds a single record: of two ambiguous duplicates, at
    # most one can be confidently confirmed.
    _wire_state(fake_api, [make_hospital(1, "Same", "1 Place")])

    record = await service.process_batch(hospitals)

    assert [row.status for row in record.rows] == [
        RowStatus.CREATED,
        RowStatus.UNKNOWN,
    ]
    assert record.successful_records == 1
    assert record.unknown_records == 1
    # Interchangeable duplicates get no concrete upstream ID.
    assert record.rows[0].upstream_hospital_id is None
    assert record.status is BatchStatus.PARTIALLY_FAILED


# -- activation gates ----------------------------------------------------------


async def test_incorrect_upstream_count_blocks_activation(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(
        ("St. Mary", "1 Hill St", None),
        ("Grace", "2 Lake Rd", None),
        ("Riverside", "3 River Ave", None),
    )
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
        ("Grace", "2 Lake Rd", None): make_hospital(2, "Grace", "2 Lake Rd"),
        ("Riverside", "3 River Ave", None): make_hospital(3, "Riverside", "3 River Ave"),
    }
    # All three rows created, but the upstream batch holds an extra record.
    _wire_state(
        fake_api,
        [
            make_hospital(1, "St. Mary", "1 Hill St"),
            make_hospital(2, "Grace", "2 Lake Rd"),
            make_hospital(3, "Riverside", "3 River Ave"),
            make_hospital(9, "Unknown", "9 Elsewhere"),
        ],
    )

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.REQUIRES_ATTENTION
    assert record.batch_activated is False
    assert fake_api.activate_calls == []
    assert "count mismatch" in record.error_summary


async def test_missing_expected_record_blocks_activation(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None), ("Grace", "2 Lake Rd", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
        ("Grace", "2 Lake Rd", None): make_hospital(2, "Grace", "2 Lake Rd"),
    }
    # Counts match (two each) but one record is the wrong one.
    _wire_state(
        fake_api,
        [
            make_hospital(1, "St. Mary", "1 Hill St"),
            make_hospital(7, "Wrong", "7 Wrong Way"),
        ],
    )

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.REQUIRES_ATTENTION
    assert record.batch_activated is False
    assert fake_api.activate_calls == []


async def test_mixed_active_inactive_batch_blocked(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None), ("Grace", "2 Lake Rd", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
        ("Grace", "2 Lake Rd", None): make_hospital(2, "Grace", "2 Lake Rd"),
    }
    _wire_state(
        fake_api,
        [
            make_hospital(1, "St. Mary", "1 Hill St", active=True),
            make_hospital(2, "Grace", "2 Lake Rd", active=False),
        ],
    )

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.REQUIRES_ATTENTION
    assert record.batch_activated is False
    assert fake_api.activate_calls == []
    assert "active" in record.error_summary


async def test_already_active_batch_blocked(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
    }
    _wire_state(fake_api, [make_hospital(1, "St. Mary", "1 Hill St", active=True)])

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.REQUIRES_ATTENTION
    assert record.batch_activated is False
    assert fake_api.activate_calls == []


def _wire_all_false_state(fake_api: FakeHospitalAPI) -> None:
    """A single valid record with an explicit ``active: false`` flag."""
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St")
    }
    _wire_state(
        fake_api, [make_hospital(1, "St. Mary", "1 Hill St", active=False)]
    )
    _wire_activation(fake_api, 1)


async def test_explicit_all_inactive_allows_activation(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    """Every hospital explicitly ``active: false`` -> activation proceeds."""
    _wire_all_false_state(fake_api)
    record = await service.process_batch(
        make_rows(("St. Mary", "1 Hill St", None))
    )

    assert record.status is BatchStatus.COMPLETED
    assert record.batch_activated is True
    assert len(fake_api.activate_calls) == 1


async def test_active_flag_missing_blocks_activation(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    _wire_all_false_state(fake_api)
    # The key is absent from the upstream payload: the state is unverifiable.
    fake_api.get_batch_hospitals_fn = lambda batch_id: make_state(
        batch_id, [make_hospital(1, "St. Mary", "1 Hill St", active=ACTIVE_MISSING)]
    )

    record = await service.process_batch(make_rows(("St. Mary", "1 Hill St", None)))

    assert record.status is BatchStatus.REQUIRES_ATTENTION
    assert record.batch_activated is False
    assert fake_api.activate_calls == []
    assert "active" in record.error_summary


async def test_active_flag_null_blocks_activation(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    _wire_all_false_state(fake_api)
    fake_api.get_batch_hospitals_fn = lambda batch_id: make_state(
        batch_id, [make_hospital(1, "St. Mary", "1 Hill St", active=None)]
    )

    record = await service.process_batch(make_rows(("St. Mary", "1 Hill St", None)))

    assert record.status is BatchStatus.REQUIRES_ATTENTION
    assert record.batch_activated is False
    assert fake_api.activate_calls == []


async def test_active_flag_non_boolean_blocks_activation(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    _wire_all_false_state(fake_api)
    fake_api.get_batch_hospitals_fn = lambda batch_id: make_state(
        batch_id, [make_hospital(1, "St. Mary", "1 Hill St", active="false")]
    )

    record = await service.process_batch(make_rows(("St. Mary", "1 Hill St", None)))

    assert record.status is BatchStatus.REQUIRES_ATTENTION
    assert record.batch_activated is False
    assert fake_api.activate_calls == []


async def test_mixed_valid_and_invalid_active_flags_block_activation(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None), ("Grace", "2 Lake Rd", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
        ("Grace", "2 Lake Rd", None): make_hospital(2, "Grace", "2 Lake Rd"),
    }
    _wire_state(
        fake_api,
        [
            make_hospital(1, "St. Mary", "1 Hill St", active=False),
            make_hospital(2, "Grace", "2 Lake Rd", active=ACTIVE_MISSING),
        ],
    )

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.REQUIRES_ATTENTION
    assert record.batch_activated is False
    assert fake_api.activate_calls == []


async def test_activation_failure_preserves_records(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
    }
    _wire_state(fake_api, [make_hospital(1, "St. Mary", "1 Hill St")])
    fake_api.activate_error = make_failure(500, False, "activate")

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.CREATED_NOT_ACTIVATED
    assert record.successful_records == 1
    assert record.batch_activated is False
    assert len(fake_api.activate_calls) == 1
    assert "Activation failed" in record.error_summary


async def test_ambiguous_activation_never_retried(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
    }
    _wire_state(fake_api, [make_hospital(1, "St. Mary", "1 Hill St")])
    # A timeout during activation: it may have partially succeeded.
    fake_api.activate_error = BatchActivationError(
        UpstreamErrorKind.TIMEOUT,
        "Upstream activate batch timed out",
        ambiguous=True,
    )

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.REQUIRES_ATTENTION
    assert record.batch_activated is False
    assert len(fake_api.activate_calls) == 1  # exactly one attempt
    assert "ambiguous" in record.error_summary


# -- concurrency ----------------------------------------------------------------


async def test_concurrency_limit_is_enforced(
    settings: Settings,
    fake_api: FakeHospitalAPI,
    repository: RecordingRepository,
) -> None:
    limited = Settings(max_hospitals_per_batch=20, hospital_api_max_concurrency=3)
    service = BulkProcessingService(limited, fake_api, repository)
    hospitals = make_rows(*[("H {0}".format(i), "{0} St".format(i), None) for i in range(10)])
    fake_api.create_results = {
        ("H {0}".format(i), "{0} St".format(i), None): make_hospital(
            i + 1, "H {0}".format(i), "{0} St".format(i)
        )
        for i in range(10)
    }
    _wire_state(
        fake_api,
        [
            make_hospital(i + 1, "H {0}".format(i), "{0} St".format(i))
            for i in range(10)
        ],
    )
    _wire_activation(fake_api, 10)

    record = await service.process_batch(hospitals)

    assert record.status is BatchStatus.COMPLETED
    # Ten records under a cap of three: the semaphore never let more than
    # three creations run at once, yet all ten were still submitted.
    assert fake_api.max_in_flight == 3
    assert len(fake_api.create_calls) == 10


# -- input invariants -----------------------------------------------------------


async def test_csv_row_numbers_are_preserved(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    # Non-contiguous row numbers, as they arise when blank lines exist.
    hospitals = [
        HospitalRow(row=2, name="A", address="1 A St"),
        HospitalRow(row=4, name="B", address="2 B St", phone="555"),
        HospitalRow(row=7, name="C", address="3 C St"),
    ]
    fake_api.create_results = {
        ("A", "1 A St", None): make_hospital(1, "A", "1 A St"),
        ("B", "2 B St", "555"): make_hospital(2, "B", "2 B St", "555"),
        ("C", "3 C St", None): make_hospital(3, "C", "3 C St"),
    }
    _wire_state(
        fake_api,
        [
            make_hospital(1, "A", "1 A St"),
            make_hospital(2, "B", "2 B St", "555"),
            make_hospital(3, "C", "3 C St"),
        ],
    )
    _wire_activation(fake_api, 3)

    record = await service.process_batch(hospitals)

    assert [row.row_number for row in record.rows] == [2, 4, 7]


async def test_empty_input_rejected(
    service: BulkProcessingService, fake_api: FakeHospitalAPI
) -> None:
    with pytest.raises(ValueError, match="at least one"):
        await service.process_batch([])
    assert fake_api.create_calls == []


async def test_batch_size_limit_enforced(
    settings: Settings,
    fake_api: FakeHospitalAPI,
    repository: RecordingRepository,
) -> None:
    service = BulkProcessingService(settings, fake_api, repository)
    too_many = make_rows(*[("H", "X", None)] * 21)
    with pytest.raises(ValueError, match="exceeds the maximum"):
        await service.process_batch(too_many)
    assert fake_api.create_calls == []
    assert len(repository) == 0


# -- repository state ------------------------------------------------------------


async def test_repository_tracks_full_lifecycle(
    service: BulkProcessingService,
    fake_api: FakeHospitalAPI,
    repository: RecordingRepository,
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
    }
    _wire_state(fake_api, [make_hospital(1, "St. Mary", "1 Hill St")])
    _wire_activation(fake_api, 1)

    record = await service.process_batch(hospitals)

    # Initial, post-creation, and final snapshots were all persisted.
    assert repository.save_statuses == [
        BatchStatus.PROCESSING,
        BatchStatus.PROCESSING,
        BatchStatus.COMPLETED,
    ]
    stored = repository.get(record.batch_id)
    assert stored == record
    assert stored is not record  # defensive copy, not the live object


async def test_repository_returns_isolated_copies(
    service: BulkProcessingService,
    fake_api: FakeHospitalAPI,
    repository: RecordingRepository,
) -> None:
    hospitals = make_rows(("St. Mary", "1 Hill St", None))
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_failure(400, False, "create"),
    }
    _wire_state(fake_api, [])

    record = await service.process_batch(hospitals)

    first = repository.get(record.batch_id)
    second = repository.get(record.batch_id)
    # Mutating one copy must not touch the stored state.
    first.rows.append(record.rows[0])
    assert len(second.rows) == 1
    assert len(repository.list()) == 1
    assert repository.list()[0].rows[0] == second.rows[0]


async def test_failed_batch_persisted_with_created_rows(
    service: BulkProcessingService,
    fake_api: FakeHospitalAPI,
    repository: RecordingRepository,
) -> None:
    hospitals = make_rows(("Good", "1 G St", None), ("Bad", "2 B St", None))
    fake_api.create_results = {
        ("Good", "1 G St", None): make_hospital(1, "Good", "1 G St"),
        ("Bad", "2 B St", None): make_failure(404, False, "create"),
    }
    _wire_state(fake_api, [make_hospital(1, "Good", "1 G St")])

    record = await service.process_batch(hospitals)
    stored = repository.get(record.batch_id)

    # Successful records survive the partial failure, with their IDs.
    assert stored.status is BatchStatus.PARTIALLY_FAILED
    assert stored.successful_records == 1
    assert [r.upstream_hospital_id for r in stored.rows] == [1, None]
