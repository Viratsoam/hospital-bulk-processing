"""HTTP-level tests for the validation and health endpoints."""

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

from tests.conftest import csv_bytes


def test_health_check(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "Hospital Bulk Processing"


def test_validate_valid_csv(client: TestClient) -> None:
    payload = csv_bytes("name,address,phone", "St. Mary, 1 Hill St, 555-0101")
    response = client.post(
        "/hospitals/validate",
        files={"file": ("hospitals.csv", payload, "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["total_records"] == 1
    assert body["hospitals"][0]["name"] == "St. Mary"
    assert body["hospitals"][0]["phone"] == "555-0101"
    assert body["errors"] == []


def test_validate_invalid_csv_returns_200_with_errors(
    client: TestClient,
) -> None:
    # An invalid CSV is a business outcome, not a transport failure.
    payload = csv_bytes("name", "St. Mary")
    response = client.post(
        "/hospitals/validate",
        files={"file": ("hospitals.csv", payload, "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["hospitals"] == []
    assert body["errors"][0]["code"] == "missing_headers"


def test_validate_missing_file_part_returns_422(client: TestClient) -> None:
    response = client.post("/hospitals/validate")
    assert response.status_code == 422


def test_validate_oversized_upload_returns_413(settings: Settings) -> None:
    app = create_app(settings)
    limit = settings.max_csv_size_bytes
    oversized = b"0" * (limit + 1)
    with TestClient(app) as client:
        response = client.post(
            "/hospitals/validate",
            files={"file": ("big.csv", oversized, "text/csv")},
        )
    assert response.status_code == 413


def test_swagger_docs_available(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health" in paths
    assert "/hospitals/validate" in paths
