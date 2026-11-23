"""FastAPI application factory and ASGI entry point.

Run locally with::

    uvicorn app.main:app --reload

The module exposes two things:

* :func:`create_app` — a factory so tests can build isolated instances with
  overridden dependencies.
* ``app`` — the module-level instance ASGI servers import.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import csv as csv_routes
from app.api.routes import health as health_routes
from app.api.routes import bulk as bulk_routes
from app.clients.hospital_api_client import HospitalAPIClient
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.repositories.batch_repository import InMemoryBatchRepository

logger = get_logger(__name__)

API_DESCRIPTION = """
A bulk processing service layered on top of the Hospital Directory API.

The upstream API handles individual hospital records and exposes batch
activation semantics. This service adds CSV ingestion and validation on top of
it.

**Scope**

* CSV upload validation — `POST /hospitals/validate`
* Bulk hospital creation — `POST /hospitals/bulk` (validates, creates,
  reconciles, and activates a batch)
"""


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build a configured FastAPI application.

    Args:
        settings: Settings to use. Defaults to the process-wide singleton;
            tests pass an explicit instance to control limits.

    Returns:
        A fully configured application instance.
    """
    resolved = settings or get_settings()
    configure_logging(resolved)

    application = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version,
        description=API_DESCRIPTION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Hospital Bulk Processing"},
        license_info={"name": "MIT"},
        lifespan=_application_lifespan,
    )

    application.state.settings = resolved

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(health_routes.router)
    application.include_router(csv_routes.router)
    application.include_router(bulk_routes.router)

    _install_exception_handlers(application)

    logger.info(
        "Application configured name=%s version=%s environment=%s",
        resolved.app_name,
        resolved.app_version,
        resolved.environment,
    )
    return application


@asynccontextmanager
async def _application_lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Manage the lifecycle of long-lived resources.

    The application-scoped collaborators the routes depend on are created
    here and attached to ``application.state``:

    * a single shared :class:`HospitalAPIClient` (and its ``httpx.AsyncClient``
      connection pool), closed on shutdown;
    * an in-memory :class:`InMemoryBatchRepository` for batch records.

    The bulk processing service is stateless apart from these two, so routes
    build it per request from the shared collaborators rather than caching a
    bound instance. No request ever re-creates the HTTP client.

    Args:
        application: The application instance for which to manage resources.

    Yields:
        Control for the duration of the application's lifetime.
    """
    settings: Settings = application.state.settings
    client = HospitalAPIClient(settings)
    application.state.hospital_api = client
    application.state.batch_repository = InMemoryBatchRepository()
    logger.info("Hospital API client started base_url=%s",
                settings.hospital_api_base_url)
    try:
        yield
    finally:
        await client.aclose()
        del application.state.hospital_api
        del application.state.batch_repository
        logger.info("Hospital API client closed")


def _install_exception_handlers(application: FastAPI) -> None:
    """Register handlers that keep error responses consistent.

    Args:
        application: Application to attach handlers to.
    """
    # FastAPI's own 422 for malformed multipart bodies is left untouched, but
    # unexpected server errors must not leak stack traces to clients.
    @application.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Convert an unhandled exception into a generic 500 payload."""
        logger.exception(
            "Unhandled error method=%s path=%s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal error occurred."},
        )


app = create_app()
