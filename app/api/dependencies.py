"""FastAPI dependency providers.

Central place where concrete implementations are constructed and injected.
Route handlers declare what they need; this module decides how it is built, so
tests can override any provider with ``app.dependency_overrides``.
"""

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.validation_service import CsvValidationService

logger = get_logger(__name__)


def provide_settings() -> Settings:
    """Return the cached application settings.

    Returns:
        The process-wide settings singleton.
    """
    return get_settings()


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
