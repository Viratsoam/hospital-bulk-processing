"""Shared fixtures for the test suite."""

from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.validation_service import CsvValidationService


@pytest.fixture()
def settings() -> Settings:
    """Settings with explicit limits so tests never depend on the environment."""
    return Settings(max_hospitals_per_batch=20, max_csv_size_bytes=1_000_000)


@pytest.fixture()
def service(settings: Settings) -> CsvValidationService:
    """A validation service wired to the test settings."""
    return CsvValidationService(settings=settings)


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    """An in-process ASGI client over a dedicated application instance."""
    application = create_app(settings)
    with TestClient(application) as test_client:
        yield test_client


def csv_bytes(*lines: str, bom: bool = False) -> bytes:
    """Build CSV bytes from lines, optionally prefixed with a UTF-8 BOM.

    Args:
        lines: CSV lines; joined with ``\\n``.
        bom: Prefix the payload with a UTF-8 byte order mark.

    Returns:
        The encoded payload.
    """
    text = "\n".join(lines)
    payload = text.encode("utf-8")
    if bom:
        payload = b"\xef\xbb\xbf" + payload
    return payload
