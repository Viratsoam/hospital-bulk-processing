"""Health-check routes."""

from fastapi import APIRouter, Depends

from app.api.dependencies import provide_settings
from app.core.config import Settings
from app.schemas.csv import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Reports that the service is running and returns its configured "
        "identity. Used by container orchestrators and uptime monitors."
    ),
)
def health_check(settings: Settings = Depends(provide_settings)) -> HealthResponse:
    """Return a static health payload.

    This endpoint performs no downstream calls. It answers "is the process
    up?", not "is the upstream Hospital Directory API reachable?" — those are
    deliberately different questions.

    Args:
        settings: Injected application settings.

    Returns:
        Service identity and status.
    """
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )
