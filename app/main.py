"""FastAPI application factory and ASGI entry point.

Run locally with::

    uvicorn app.main:app --reload

The module exposes two things:

* :func:`create_app` — a factory so tests can build isolated instances with
  overridden dependencies.
* ``app`` — the module-level instance ASGI servers import.
"""

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import csv as csv_routes
from app.api.routes import health as health_routes
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

API_DESCRIPTION = """
A bulk processing service layered on top of the Hospital Directory API.

The upstream API handles individual hospital records and exposes batch
activation semantics. This service adds CSV ingestion and validation on top of
it.

**Current scope**

* CSV upload validation — `POST /hospitals/validate`

Bulk creation is not implemented yet.
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

    _install_exception_handlers(application)

    logger.info(
        "Application configured name=%s version=%s environment=%s",
        resolved.app_name,
        resolved.app_version,
        resolved.environment,
    )
    return application


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
