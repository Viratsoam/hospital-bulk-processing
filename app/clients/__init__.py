"""External API integration.

The Hospital Directory API client lives here and is the application's only
gateway to the upstream service. It deliberately knows nothing about CSV or
bulk-processing business logic, so that layer stays a pure caller.
"""

from app.clients.hospital_api_client import (
    ActivationResult,
    BatchActivationError,
    BatchRetrievalError,
    HospitalAPIClient,
    HospitalAPIError,
    HospitalCreationError,
    UpstreamBatchState,
    UpstreamErrorKind,
    UpstreamHospital,
)

__all__ = [
    "ActivationResult",
    "BatchActivationError",
    "BatchRetrievalError",
    "HospitalAPIClient",
    "HospitalAPIError",
    "HospitalCreationError",
    "UpstreamBatchState",
    "UpstreamErrorKind",
    "UpstreamHospital",
]
