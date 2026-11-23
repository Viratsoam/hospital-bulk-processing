"""HTTP-level integration tests for ``POST /hospitals/bulk``.

The upstream API client is replaced with the programmable fake from
``test_bulk_processing_service`` (imported, not duplicated); everything else
— the route, validation service, bulk service, repository, and lifespan —
runs for real through the FastAPI application. No test makes a network call.
"""

from typing import Any, Dict, Iterator, List, Optional
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import provide_hospital_api_client
from app.core.config import Settings
from app.main import create_app

from tests.conftest import csv_bytes
from tests.test_bulk_processing_service import (
    ACTIVE_MISSING,
    FakeHospitalAPI,
    _wire_activation,
    _wire_state,
    make_failure,
    make_hospital,
    make_state,
)


def _csv(payload: str) -> bytes:
    return payload.encode("utf-8")


VALID_CSV = "name,address,phone\nSt. Mary,1 Hill St,555-0101\nGrace,2 Lake Rd,\n"


@pytest.fixture()
def settings() -> Settings:
    return Settings(max_hospitals_per_batch=20, max_csv_size_bytes=5_000)


@pytest.fixture()
def fake_api() -> FakeHospitalAPI:
    return FakeHospitalAPI()


@pytest.fixture()
def client(
    settings: Settings, fake_api: FakeHospitalAPI
) -> Iterator[TestClient]:
    """An application whose shared API client is the programmable fake."""
    application = create_app(settings)
    application.dependency_overrides[provide_hospital_api_client] = lambda: fake_api
    with TestClient(application) as test_client:
        yield test_client


def post_bulk(
    client: TestClient,
    payload: bytes,
    filename: str = "hospitals.csv",
    content_type: str = "text/csv",
) -> Any:
    return client.post(
        "/hospitals/bulk",
        files={"file": (filename, payload, content_type)},
    )


def wire_all_created(
    fake_api: FakeHospitalAPI,
    hospitals: List[Dict[str, Any]],
    activate: bool = True,
) -> None:
    """Seed the fake so every listed record is created and the batch state matches.

    ``hospitals`` is a list of dicts with ``id``, ``name``, ``address``,
    ``phone`` keys; ``activate`` also wires successful activation.
    """
    records = [
        make_hospital(
            h["id"], h["name"], h["address"], h.get("phone"), active=h.get("active", False)
        )
        for h in hospitals
    ]
    fake_api.create_results = {
        (h["name"], h["address"], h.get("phone")): make_hospital(
            h["id"], h["name"], h["address"], h.get("phone")
        )
        for h in hospitals
    }
    _wire_state(fake_api, records)
    if activate:
        _wire_activation(fake_api, len(hospitals))


# -- valid CSV handling -------------------------------------------------------


async def test_valid_csv_creates_and_activates(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    wire_all_created(
        fake_api,
        [
            {"id": 1, "name": "St. Mary", "address": "1 Hill St", "phone": "555-0101"},
            {"id": 2, "name": "Grace", "address": "2 Lake Rd", "phone": None},
        ],
    )
    response = post_bulk(client, _csv(VALID_CSV))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["batch_activated"] is True
    assert body["total_records"] == 2
    assert body["processed_records"] == 2
    assert body["successful_records"] == 2
    assert body["failed_records"] == 0
    assert body["unknown_records"] == 0
    assert body["activated_count"] == 2
    assert body["error_summary"] is None
    assert body["processing_time"] is not None
    assert UUID(body["batch_id"])  # a valid UUID
    assert [row["row_number"] for row in body["results"]] == [2, 3]
    assert [row["upstream_hospital_id"] for row in body["results"]] == [1, 2]
    assert all(row["status"] == "created" for row in body["results"])
    # The optional-phone row round-trips through the response with null.
    assert body["results"][1]["phone"] is None


async def test_successful_activation_records_count(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    wire_all_created(
        fake_api, [{"id": 7, "name": "Solo", "address": "9 Main St"}]
    )
    response = post_bulk(client, _csv("name,address\nSolo,9 Main St\n"))

    assert response.status_code == 200
    assert response.json()["batch_activated"] is True
    assert response.json()["activated_count"] == 1
    assert fake_api.activate_calls and len(fake_api.activate_calls) == 1


async def test_duplicate_rows_are_submitted_separately(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    wire_all_created(
        fake_api,
        [
            {"id": 1, "name": "Same", "address": "1 Place"},
            {"id": 2, "name": "Same", "address": "1 Place"},
        ],
    )
    response = post_bulk(
        client, _csv("name,address\nSame,1 Place\nSame,1 Place\n")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_records"] == 2
    assert body["successful_records"] == 2
    assert body["status"] == "completed"
    duplicate_calls = [
        c for c in fake_api.create_calls if c[0] == ("Same", "1 Place", None)
    ]
    assert len(duplicate_calls) == 2
    # The two rows keep distinct original line numbers despite equal values.
    assert [row["row_number"] for row in body["results"]] == [2, 3]


# -- partial failures ---------------------------------------------------------


async def test_partial_creation_failure_returns_200(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    hospitals = [
        {"id": 1, "name": "Good", "address": "1 G St"},
        {"id": 2, "name": "Bad", "address": "2 B St"},
    ]
    wire_all_created(fake_api, hospitals, activate=False)
    fake_api.create_results = {
        ("Good", "1 G St", None): make_hospital(1, "Good", "1 G St"),
        ("Bad", "2 B St", None): make_failure(400, False, "create"),
    }
    _wire_state(fake_api, [make_hospital(1, "Good", "1 G St")])

    response = post_bulk(client, _csv("name,address\nGood,1 G St\nBad,2 B St\n"))

    # A partial failure is a business outcome, not a server error.
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partially_failed"
    assert body["batch_activated"] is False
    assert body["successful_records"] == 1
    assert body["failed_records"] == 1
    assert body["error_summary"] is not None
    failed = body["results"][1]
    assert failed["status"] == "failed"
    assert failed["error_message"] is not None
    assert fake_api.activate_calls == []


async def test_all_creation_failures_return_200(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    fake_api.create_results = {
        ("A", "1 A", None): make_failure(422, False, "create"),
        ("B", "2 B", None): make_failure(500, False, "create"),
    }
    _wire_state(fake_api, [])

    response = post_bulk(client, _csv("name,address\nA,1 A\nB,2 B\n"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partially_failed"
    assert body["successful_records"] == 0
    assert body["failed_records"] == 2
    assert all(row["status"] == "failed" for row in body["results"])


# -- reconciliation and activation outcomes -----------------------------------


async def test_ambiguous_creation_reconciled_to_success(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    fake_api.create_results = {
        ("St. Mary", "1 Hill St", None): make_hospital(1, "St. Mary", "1 Hill St"),
        ("Grace", "2 Lake Rd", None): make_failure(504, True, "create"),
    }
    _wire_state(
        fake_api,
        [
            make_hospital(1, "St. Mary", "1 Hill St", active=False),
            make_hospital(2, "Grace", "2 Lake Rd", active=False),
        ],
    )
    _wire_activation(fake_api, 2)

    response = post_bulk(
        client, _csv("name,address\nSt. Mary,1 Hill St\nGrace,2 Lake Rd\n")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["unknown_records"] == 0
    assert body["results"][1]["status"] == "created"
    assert body["results"][1]["upstream_hospital_id"] == 2


async def test_activation_failure_is_200_with_status(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    wire_all_created(
        fake_api, [{"id": 1, "name": "St. Mary", "address": "1 Hill St"}]
    )
    fake_api.activate_error = make_failure(500, False, "activate")

    response = post_bulk(client, _csv("name,address\nSt. Mary,1 Hill St\n"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "created_not_activated"
    assert body["batch_activated"] is False
    assert body["error_summary"] is not None
    assert len(fake_api.activate_calls) == 1


# -- invalid CSV: rejected before any upstream call ---------------------------


def _assert_no_upstream_calls(fake_api: FakeHospitalAPI) -> None:
    assert fake_api.create_calls == []
    assert fake_api.get_calls == []
    assert fake_api.activate_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        "name,phone\nA,555",  # missing required address column
        "city,address\nX,1 B",  # missing required name column
        "name,address\n,1 B",  # missing name value
        "name,address\nSt. Mary,",  # missing address value
        "name,address,city\nA,1 B,C",  # unknown column
        "name,address",  # header only, no records
        "",  # empty file
    ],
)
async def test_invalid_csv_rejected_without_upstream_calls(
    client: TestClient, fake_api: FakeHospitalAPI, payload: str
) -> None:
    response = post_bulk(client, _csv(payload))

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["valid"] is False
    assert detail["errors"]
    _assert_no_upstream_calls(fake_api)


async def test_too_many_records_rejected(client: TestClient, fake_api: FakeHospitalAPI) -> None:
    lines = ["name,address"] + [
        "Hospital {0}, {0} Street".format(i) for i in range(21)
    ]
    response = post_bulk(client, _csv("\n".join(lines)))

    assert response.status_code == 400
    assert response.json()["detail"]["total_records"] == 21
    _assert_no_upstream_calls(fake_api)


async def test_invalid_encoding_rejected(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    response = post_bulk(
        client, b"name,address\nSt. M\xe4ry, 1 B"
    )

    assert response.status_code == 400
    _assert_no_upstream_calls(fake_api)


async def test_malformed_csv_rejected(client: TestClient, fake_api: FakeHospitalAPI) -> None:
    # An unterminated quoted cell is not parseable under strict dialects.
    response = post_bulk(client, b'name,address\n"A, 1 B')

    assert response.status_code == 400
    _assert_no_upstream_calls(fake_api)


async def test_oversized_file_rejected(
    client: TestClient, fake_api: FakeHospitalAPI, settings: Settings
) -> None:
    oversized = b"0" * (settings.max_csv_size_bytes + 1)
    response = post_bulk(client, oversized)

    assert response.status_code == 413
    _assert_no_upstream_calls(fake_api)


async def test_missing_file_field_returns_422(client: TestClient) -> None:
    response = client.post("/hospitals/bulk")
    assert response.status_code == 422


@pytest.mark.parametrize(
    "filename, content_type",
    [
        ("data.json", "text/csv"),  # wrong extension, no CSV extension to fall back on
        ("hospitals.txt", "text/plain"),
    ],
)
async def test_unsupported_file_type_returns_415(
    client: TestClient,
    fake_api: FakeHospitalAPI,
    filename: str,
    content_type: str,
) -> None:
    response = post_bulk(client, _csv(VALID_CSV), filename=filename, content_type=content_type)

    assert response.status_code == 415
    _assert_no_upstream_calls(fake_api)


async def test_extensionless_csv_media_type_accepted(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    wire_all_created(fake_api, [{"id": 1, "name": "St. Mary", "address": "1 Hill St"}])
    response = post_bulk(
        client, _csv("name,address\nSt. Mary,1 Hill St\n"), content_type="text/csv"
    )

    assert response.status_code == 200


# -- response schema shape -----------------------------------------------------


async def test_response_shape_matches_spec(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    wire_all_created(
        fake_api, [{"id": 1, "name": "St. Mary", "address": "1 Hill St"}]
    )
    body = post_bulk(client, _csv("name,address\nSt. Mary,1 Hill St\n")).json()

    expected_top_level = {
        "batch_id",
        "status",
        "total_records",
        "processed_records",
        "successful_records",
        "failed_records",
        "unknown_records",
        "processing_time",
        "batch_activated",
        "error_summary",
        "activated_count",
        "results",
    }
    assert expected_top_level <= set(body)

    row = body["results"][0]
    expected_row_fields = {
        "row_number",
        "name",
        "address",
        "phone",
        "status",
        "upstream_hospital_id",
        "error_message",
    }
    assert expected_row_fields <= set(row)


# -- existing endpoints still work ---------------------------------------------


async def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_validation_endpoint_unchanged(
    client: TestClient, fake_api: FakeHospitalAPI
) -> None:
    response = client.post(
        "/hospitals/validate",
        files={"file": ("hospitals.csv", _csv(VALID_CSV), "text/csv")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["total_records"] == 2
    _assert_no_upstream_calls(fake_api)
