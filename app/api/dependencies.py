"""FastAPI dependency providers.

Central place where concrete implementations are constructed and injected.
Route handlers declare what they need; this module decides how it is built, so
tests can override any provider with ``app.dependency_overrides``.
"""

from fastapi import Depends, Request

from app.clients.hospital_api_client import HospitalAPIClient
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.repositories.batch_repository import BatchRepository
from app.services.bulk_processing_service import BulkProcessingService
from app.services.validation_service import CsvValidationService

logger = get_logger(__name__)


def provide_settings(request: Request) -> Settings:
    """Return the settings in effect for this application instance.

    The application factory attaches the settings it was built with to
    ``request.app.state`` (see ``create_app``); those take precedence over the
    process-wide singleton so a request always sees the configuration of the
    app it is served by.

    Args:
        request: Incoming request, used to reach application state.

    Returns:
        The app-scoped settings, falling back to the singleton.
    """
    return getattr(request.app.state, "settings", None) or get_settings()


def provide_validation_service(
    request: Request,
    settings: Settings = Depends(provide_settings),
) -> CsvValidationService:
    """Return the CSV validation service.

    The service is stateless apart from configuration, but it is cached on the
    application state so repeated uploads reuse one instance.

    Args:
        request: Incoming request, used to reach application state.
        settings: Injected settings.

    Returns:
        A ready-to-use :class:`CsvValidationService`.
    """
    cached = getattr(request.app.state, "validation_service", None)
    if cached is None:
        cached = CsvValidationService(settings=settings)
        request.app.state.validation_service = cached
    return cached


def provide_hospital_api_client(request: Request) -> HospitalAPIClient:
    """Return the application-scoped shared upstream API client.

    Created once in the lifespan and closed on shutdown; never rebuilt per
    request.

    Args:
        request: Incoming request, used to reach application state.

    Returns:
        The shared :class:`HospitalAPIClient`.
    """
    return request.app.state.hospital_api


def provide_batch_repository(request: Request) -> BatchRepository:
    """Return the application-scoped batch repository.

    Args:
        request: Incoming request, used to reach application state.

    Returns:
        The shared :class:`BatchRepository`.
    """
    return request.app.state.batch_repository


def provide_bulk_processing_service(
    request: Request,
    hospital_api: HospitalAPIClient = Depends(provide_hospital_api_client),
    repository: BatchRepository = Depends(provide_batch_repository),
) -> BulkProcessingService:
    """Return a bulk processing service wired to the shared collaborators.

    The service is stateless apart from its collaborators, so it is built
    fresh per request from the injected (and overridable) collaborators. The
    collaborators themselves — the shared upstream client and the repository —
    are application-scoped, so no request ever re-creates the HTTP client.

    Args:
        request: Incoming request, used to reach application state.
        hospital_api: The shared upstream API client (overridable in tests).
        repository: The shared batch repository (overridable in tests).

    Returns:
        A ready-to-use :class:`BulkProcessingService`.
    """
    return BulkProcessingService(
        settings=getattr(request.app.state, "settings", None) or get_settings(),
        hospital_api=hospital_api,
        repository=repository,
    )
