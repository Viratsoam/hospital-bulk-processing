"""Asynchronous client for the Hospital Directory API.

This module is the application's only gateway to the upstream service. It is
deliberately ignorant of CSV and bulk-processing business logic: it knows how
to create one hospital, fetch one batch, and activate one batch, and how to
classify what went wrong when any of those calls fails.

Design notes
------------
* A single shared :class:`httpx.AsyncClient` is created when the FastAPI
  application starts (via its lifespan) and closed when it stops.
* Retries happen only where they cannot duplicate or corrupt state:

  =====================  ============  ============  ============
  operation              read timeouts connect errors  5xx
  =====================  ============  ============  ============
  create hospital        no (ambiguous)  yes         no (ambiguous)
  fetch batch            yes             yes         yes
  activate batch         no (ambiguous)  yes         no (ambiguous)
  =====================  ============  ============  ============

  The key distinction is *ambiguity*: a read-phase timeout on a state-changing
  call means the server may already have processed it, so the failure is
  reported as ``ambiguous`` and the caller (the future processing layer) must
  reconcile it via the batch GET endpoint instead of retrying blindly.
* Every failure is normalized into a :class:`HospitalAPIError` subclass
  carrying a machine-readable :class:`UpstreamErrorKind`, the upstream status
  when a response was received, and the original exception as ``cause``.
"""

import asyncio
import random
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Type, Union

import httpx

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Retries use full-jitter exponential backoff: the interval grows as
#: ``base * 2 ** (attempt - 1)`` capped at ``cap``, then a random fraction in
#: [1 - jitter, 1] of that is taken. Randomization desynchronizes concurrent
#: callers and keeps every sleep bounded.
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0
_BACKOFF_JITTER_FRACTION = 0.5

#: 5xx statuses treated as transient. Other 5xx codes (e.g. 501) are reported
#: as definitive server errors rather than retried.
_TRANSIENT_STATUSES: Tuple[int, ...] = (500, 502, 503, 504)


def _backoff_delay(
    attempt: int,
    base: float = _BACKOFF_BASE_SECONDS,
    cap: float = _BACKOFF_CAP_SECONDS,
) -> float:
    """Return the sleep duration before retry ``attempt`` (1-based).

    Args:
        attempt: The attempt number being postponed, starting at 1.
        base: Exponential backoff base in seconds.
        cap: Upper bound on any single delay in seconds.

    Returns:
        A delay in ``[ceiling * (1 - jitter), ceiling]`` where
        ``ceiling = min(cap, base * 2 ** (attempt - 1))``.
    """
    exponent = 2 ** (attempt - 1)
    ceiling = min(cap, base * exponent)
    floor = ceiling * (1.0 - _BACKOFF_JITTER_FRACTION)
    return random.uniform(floor, ceiling)


class UpstreamErrorKind(str, Enum):
    """Machine-readable categories of upstream failures.

    Callers branch on these values; the human-readable message on the
    exception may be reworded without breaking them. The category that
    matters most is ``ambiguous`` — not a kind but a flag on the exception —
    which tells the caller the request may have had an upstream effect and
    must be reconciled, not retried.
    """

    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    UNEXPECTED_STATUS = "unexpected_status"
    SERVER_ERROR = "server_error"
    CONNECTION_ERROR = "connection_error"
    TIMEOUT = "timeout"
    DECODE_ERROR = "decode_error"
    UNEXPECTED_RESPONSE = "unexpected_response"


class HospitalAPIError(Exception):
    """Base class for every failure talking to the upstream API.

    Attributes:
        kind: Machine-readable failure category.
        message: Human-readable description of the failure.
        ambiguous: True when the request may already have been processed
            upstream and therefore must not be blindly retried.
        status_code: The upstream HTTP status, when a response was received.
        cause: The underlying exception this error wraps, when any.
        request: The :class:`httpx.Request` that failed, when known.
    """

    def __init__(
        self,
        kind: UpstreamErrorKind,
        message: str,
        *,
        ambiguous: bool = False,
        status_code: Optional[int] = None,
        cause: Optional[Exception] = None,
        request: Optional[httpx.Request] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.ambiguous = ambiguous
        self.status_code = status_code
        self.cause = cause
        self.request = request

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "[{0}] {1}".format(self.kind.value, self.message)


class HospitalCreationError(HospitalAPIError):
    """Failure while creating a hospital via ``POST /hospitals/``."""


class BatchRetrievalError(HospitalAPIError):
    """Failure while fetching a batch's hospitals."""


class BatchActivationError(HospitalAPIError):
    """Failure while activating a batch.

    Note that a failure here with ``ambiguous=True`` means the activation
    may have partially succeeded; activation is not idempotent, so it must
    never be retried automatically.
    """


@dataclass(frozen=True)
class UpstreamHospital:
    """One hospital record as returned by the upstream API.

    ``raw`` preserves the full upstream payload so callers can use fields the
    client does not model explicitly; the client extracts only what it is
    confident about.
    """

    id: Optional[Union[int, str]]
    name: Optional[str]
    address: Optional[str]
    phone: Optional[str]
    raw: Mapping[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True)
class UpstreamBatchState:
    """Normalized view of ``GET /hospitals/batch/{batch_id}``."""

    batch_id: str
    hospitals: List[UpstreamHospital]
    raw: Any = field(compare=False, repr=False)


@dataclass(frozen=True)
class ActivationResult:
    """Outcome of a batch activation."""

    batch_id: str
    activated_count: int
    message: Optional[str]
    raw: Mapping[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True)
class _RetryPolicy:
    """Which failures a given operation retries automatically.

    Attributes:
        retry_connect_errors: Retry when no connection was established. The
            server did not receive the request, so retrying is always safe.
        retry_connect_timeouts: Retry connect-phase timeouts, for the same
            reason.
        retry_read_timeouts: Retry body-phase timeouts. Safe only for
            read-only operations: on a state-changing call the body may have
            reached the server.
        retry_server_errors: Retry transient 5xx responses. Safe only when
            the operation is idempotent.
        mutates_state: Whether a successful call changes upstream state.
            Makes final failures ambiguous (and thus unsafe to auto-retry).
    """

    retry_connect_errors: bool = True
    retry_connect_timeouts: bool = True
    retry_read_timeouts: bool = False
    retry_server_errors: bool = False
    mutates_state: bool = True


_CREATE_POLICY = _RetryPolicy(mutates_state=True)
_FETCH_POLICY = _RetryPolicy(
    retry_read_timeouts=True, retry_server_errors=True, mutates_state=False
)
_ACTIVATE_POLICY = _RetryPolicy(mutates_state=True)


class HospitalAPIClient:
    """A thin, resilient async client for the Hospital Directory API.

    The client owns its :class:`httpx.AsyncClient` and its connection pool
    for the lifetime of the application; close it with :meth:`aclose` (or use
    it as an async context manager). An external client or transport may be
    injected for testing.
    """

    def __init__(
        self,
        settings: Settings,
        client: Optional[httpx.AsyncClient] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        """Initialize the client.

        Args:
            settings: Supplies the base URL, timeouts, pool size, and retry
                budget.
            client: A fully managed :class:`httpx.AsyncClient` to wrap. When
                supplied, this instance will *not* close it on ``aclose``;
                the caller owns its lifecycle.
            transport: An :class:`httpx.AsyncBaseTransport` (e.g. a mock)
                used with an internally created client.
        """
        self._settings = settings
        self._injected = client is not None
        if client is not None:
            self._client = client
        else:
            # A generous read timeout absorbs Render cold starts, where the
            # first request after a sleep can take far longer than the
            # steady-state ~5.3s create latency.
            timeout = httpx.Timeout(
                settings.hospital_api_timeout_seconds,
                connect=settings.hospital_api_connect_timeout_seconds,
            )
            limits = httpx.Limits(
                max_connections=settings.hospital_api_max_concurrency,
                max_keepalive_connections=settings.hospital_api_max_concurrency,
            )
            self._client = httpx.AsyncClient(
                base_url=settings.hospital_api_base_url,
                timeout=timeout,
                limits=limits,
                transport=transport,
            )

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The underlying :class:`httpx.AsyncClient` (diagnostics/tests)."""
        return self._client

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying transport.

        A no-op when an external client was injected at construction time,
        since the caller is responsible for its lifecycle.
        """
        if not self._injected and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> "HospitalAPIClient":
        """Support ``async with`` for callers that manage the client directly."""
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """Close the client when the context exits."""
        await self.aclose()

    # -- public operations ---------------------------------------------------

    async def create_hospital(
        self,
        name: str,
        address: str,
        phone: Optional[str] = None,
        *,
        batch_id: Union[str, uuid.UUID],
    ) -> UpstreamHospital:
        """Create a single hospital in the upstream directory.

        The hospital is created as part of ``batch_id`` and therefore starts
        inactive; it becomes usable only after :meth:`activate_batch`.

        Args:
            name: Hospital name.
            address: Hospital address.
            phone: Optional phone number. Omitted from the payload when
                ``None``.
            batch_id: UUID of the creation batch. Required; a hospital
                created with a batch UUID is initially inactive.

        Returns:
            The created hospital as returned by the upstream API.

        Raises:
            ValueError: If ``name`` or ``address`` is empty, or ``batch_id``
                is not a valid UUID.
            HospitalCreationError: Any upstream or transport failure.
        """
        if not name or not name.strip():
            raise ValueError("name must be a non-empty string")
        if not address or not address.strip():
            raise ValueError("address must be a non-empty string")
        resolved_batch = self._require_uuid(batch_id)

        payload: Dict[str, Any] = {
            "name": name,
            "address": address,
            "creation_batch_id": resolved_batch,
        }
        if phone is not None:
            payload["phone"] = phone

        operation = "create hospital"
        logger.debug(
            "%s batch=%s name=%r address=%r phone=%s",
            operation,
            resolved_batch,
            name,
            address,
            phone,
        )
        response = await self._send(
            operation,
            "POST",
            "/hospitals/",
            json_body=payload,
            policy=_CREATE_POLICY,
            error_cls=HospitalCreationError,
            ambiguous_hint=(
                "The request may have been processed upstream; reconcile with "
                "get_batch_hospitals before creating the record again."
            ),
        )
        self._raise_for_status(
            response,
            operation=operation,
            error_cls=HospitalCreationError,
            policy=_CREATE_POLICY,
        )
        data = self._decode_json(response, operation, HospitalCreationError)
        return self._hospital_from_payload(data, HospitalCreationError, response)

    async def get_batch_hospitals(
        self, batch_id: Union[str, uuid.UUID]
    ) -> UpstreamBatchState:
        """Fetch every hospital that belongs to a creation batch.

        This is the read-only endpoint; it also serves as the reconciliation
        tool for ambiguous create failures.

        Args:
            batch_id: UUID of the batch.

        Returns:
            The batch's hospitals. A batch that exists but has no hospitals
            yields an empty list; a batch the upstream does not know about
            raises :class:`BatchRetrievalError` with kind ``not_found``.

        Raises:
            ValueError: If ``batch_id`` is not a valid UUID.
            BatchRetrievalError: Any upstream or transport failure.
        """
        resolved_batch = self._require_uuid(batch_id)
        operation = "fetch batch {0}".format(resolved_batch)
        response = await self._send(
            operation,
            "GET",
            "/hospitals/batch/{0}".format(resolved_batch),
            policy=_FETCH_POLICY,
            error_cls=BatchRetrievalError,
        )
        self._raise_for_status(
            response,
            operation=operation,
            error_cls=BatchRetrievalError,
            policy=_FETCH_POLICY,
        )
        data = self._decode_json(response, operation, BatchRetrievalError)
        hospitals = self._hospitals_from_payload(
            data, BatchRetrievalError, response
        )
        return UpstreamBatchState(
            batch_id=resolved_batch,
            hospitals=hospitals,
            raw=data,
        )

    async def activate_batch(
        self, batch_id: Union[str, uuid.UUID]
    ) -> ActivationResult:
        """Activate every hospital in a batch.

        Activation is **not idempotent** upstream and a batch with a mix of
        active and inactive hospitals cannot be activated, so this client
        never retries an activation that ended ambiguously (timeout or 5xx).
        It does retry connect-phase failures, where the request demonstrably
        never reached the server.

        Args:
            batch_id: UUID of the batch to activate.

        Returns:
            The activation outcome, including how many hospitals were
            activated.

        Raises:
            ValueError: If ``batch_id`` is not a valid UUID.
            BatchActivationError: Any upstream or transport failure. Inspect
                ``.ambiguous`` before deciding to retry.
        """
        resolved_batch = self._require_uuid(batch_id)
        operation = "activate batch {0}".format(resolved_batch)
        response = await self._send(
            operation,
            "PATCH",
            "/hospitals/batch/{0}/activate".format(resolved_batch),
            policy=_ACTIVATE_POLICY,
            error_cls=BatchActivationError,
            ambiguous_hint=(
                "The activation may have partially succeeded. A batch with a "
                "mix of active and inactive hospitals cannot be re-activated, "
                "so fetch the batch and inspect its state before retrying."
            ),
        )
        self._raise_for_status(
            response,
            operation=operation,
            error_cls=BatchActivationError,
            policy=_ACTIVATE_POLICY,
        )
        data = self._decode_json(response, operation, BatchActivationError)
        if not isinstance(data, Mapping):
            raise self._structure_error(
                BatchActivationError,
                "expected a JSON object, got {0}".format(_type_name(data)),
                response,
            )
        count = data.get("activated_count")
        if not isinstance(count, int) or isinstance(count, bool):
            raise self._structure_error(
                BatchActivationError,
                "expected an integer 'activated_count', got {0!r}".format(count),
                response,
            )
        message = data.get("message")
        return ActivationResult(
            batch_id=resolved_batch,
            activated_count=count,
            message=message,
            raw=dict(data),
        )

    # -- retry core ----------------------------------------------------------

    async def _send(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        policy: _RetryPolicy,
        error_cls: Type[HospitalAPIError],
        ambiguous_hint: str = "",
    ) -> httpx.Response:
        """Perform one upstream request applying the operation's retry policy.

        Returns the response as soon as it is definitive (success, a 4xx
        answer, or an exhausted retry budget). Raises ``error_cls`` for
        transport-level failures that survive the budget.

        Args:
            operation: Human-readable operation name for logs and errors.
            method: HTTP method.
            path: Path relative to the configured base URL.
            json_body: Request body, or ``None`` for a bodyless request.
            policy: Which failures to retry.
            error_cls: Exception class raised on final failure.
            ambiguous_hint: Advice appended to ambiguous failure messages.

        Returns:
            The upstream response.
        """
        max_attempts = self._settings.hospital_api_max_retries + 1

        for attempt in range(1, max_attempts + 1):
            request: Optional[httpx.Request] = None
            try:
                request = self._client.build_request(method, path, json=json_body)
                response = await self._client.send(request)
            except httpx.ConnectTimeout as exc:
                # The connection was never established, so the server did not
                # receive the request: retrying is safe for every operation.
                if policy.retry_connect_timeouts and attempt < max_attempts:
                    await self._sleep_for_retry(attempt, operation)
                    continue
                raise self._transport_failure(
                    error_cls,
                    operation,
                    exc,
                    request,
                    kind=UpstreamErrorKind.TIMEOUT,
                    ambiguous=False,
                    hint="The connection was not established, so the request "
                    "did not reach the upstream API.",
                )
            except httpx.TimeoutException as exc:
                # Read/write/pool timeouts: the request body may already have
                # reached the server. Only read-only operations may retry.
                if policy.retry_read_timeouts and attempt < max_attempts:
                    await self._sleep_for_retry(attempt, operation)
                    continue
                raise self._transport_failure(
                    error_cls,
                    operation,
                    exc,
                    request,
                    kind=UpstreamErrorKind.TIMEOUT,
                    ambiguous=policy.mutates_state,
                    hint=ambiguous_hint,
                )
            except httpx.ConnectError as exc:
                # No connection: the server did not receive the request.
                if policy.retry_connect_errors and attempt < max_attempts:
                    await self._sleep_for_retry(attempt, operation)
                    continue
                raise self._transport_failure(
                    error_cls,
                    operation,
                    exc,
                    request,
                    kind=UpstreamErrorKind.CONNECTION_ERROR,
                    ambiguous=False,
                    hint="The upstream API could not be reached.",
                )
            except httpx.HTTPError as exc:
                # Any other transport-level failure (protocol error, closed
                # connection, ...). Not retried: the outcome is unknown and a
                # second attempt risks duplicate or corrupted state.
                raise self._transport_failure(
                    error_cls,
                    operation,
                    exc,
                    request,
                    kind=UpstreamErrorKind.CONNECTION_ERROR,
                    ambiguous=policy.mutates_state,
                    hint=ambiguous_hint,
                )

            if (
                response.status_code in _TRANSIENT_STATUSES
                and policy.retry_server_errors
                and attempt < max_attempts
            ):
                logger.warning(
                    "%s got HTTP %d on attempt %d/%d; retrying",
                    operation,
                    response.status_code,
                    attempt,
                    max_attempts,
                )
                await self._sleep_for_retry(attempt, operation)
                continue

            return response

        # Unreachable: every loop iteration either returns or raises.
        raise RuntimeError("retry loop exited unexpectedly")

    async def _sleep_for_retry(self, attempt: int, operation: str) -> None:
        """Wait out the backoff before the next attempt."""
        delay = _backoff_delay(attempt)
        logger.info(
            "Retrying %s (attempt %d) in %.2fs", operation, attempt, delay
        )
        await asyncio.sleep(delay)

    def _transport_failure(
        self,
        error_cls: Type[HospitalAPIError],
        operation: str,
        cause: Exception,
        request: Optional[httpx.Request],
        *,
        kind: UpstreamErrorKind,
        ambiguous: bool,
        hint: str = "",
    ) -> HospitalAPIError:
        """Build the raised error for a transport-level failure."""
        message = "Upstream {0} failed: {1}".format(operation, cause)
        if hint:
            message += " {0}".format(hint)
        return error_cls(
            kind,
            message,
            ambiguous=ambiguous,
            cause=cause,
            request=request,
        )

    # -- response handling ---------------------------------------------------

    def _raise_for_status(
        self,
        response: httpx.Response,
        *,
        operation: str,
        error_cls: Type[HospitalAPIError],
        policy: _RetryPolicy,
    ) -> None:
        """Turn a non-2xx response into the operation's error type.

        4xx answers are definitive — the upstream answered the request, so
        they are never ambiguous. A 5xx that survives the retry budget is
        ambiguous for state-changing operations: the upstream may have
        processed the request despite the error.
        """
        status = response.status_code
        if status < 400:
            return

        if status == 400:
            kind = UpstreamErrorKind.BAD_REQUEST
            ambiguous = False
        elif status == 404:
            kind = UpstreamErrorKind.NOT_FOUND
            ambiguous = False
        elif status == 422:
            kind = UpstreamErrorKind.VALIDATION_ERROR
            ambiguous = False
        elif status >= 500:
            kind = UpstreamErrorKind.SERVER_ERROR
            ambiguous = policy.mutates_state
        else:
            kind = UpstreamErrorKind.UNEXPECTED_STATUS
            ambiguous = False

        message = "Upstream {0} returned HTTP {1}".format(operation, status)
        detail = self._extract_detail(response)
        if detail:
            message += " ({0})".format(detail)
        if ambiguous:
            message += (
                " The request may have been processed upstream; reconcile "
                "via batch retrieval before retrying."
            )
        raise error_cls(
            kind,
            message,
            ambiguous=ambiguous,
            status_code=status,
            request=response.request,
        )

    @staticmethod
    def _extract_detail(response: httpx.Response) -> Optional[str]:
        """Best-effort extraction of an upstream error detail.

        Never raises: an unreadable body simply yields ``None`` and the
        caller reports the bare status.
        """
        try:
            body = response.json()
        except (ValueError, httpx.DecodingError):
            text = response.text
            return text[:200] if text else None

        if isinstance(body, Mapping):
            for key in ("detail", "message", "error"):
                value = body.get(key)
                if isinstance(value, str):
                    return value[:200]
        return str(body)[:200]

    def _decode_json(
        self,
        response: httpx.Response,
        operation: str,
        error_cls: Type[HospitalAPIError],
    ) -> Any:
        """Parse a 2xx response body, normalizing decode failures.

        A 2xx with unparseable JSON is a definitive failure: the response
        arrived, so it is never retried and never ambiguous.
        """
        try:
            return response.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise error_cls(
                UpstreamErrorKind.DECODE_ERROR,
                "Upstream {0} returned a body that is not valid JSON: "
                "{1!r}".format(operation, response.text[:200]),
                ambiguous=False,
                status_code=response.status_code,
                cause=exc,
                request=response.request,
            )

    def _structure_error(
        self,
        error_cls: Type[HospitalAPIError],
        problem: str,
        response: httpx.Response,
    ) -> HospitalAPIError:
        """Build the raised error for a well-formed but unexpected response."""
        return error_cls(
            UpstreamErrorKind.UNEXPECTED_RESPONSE,
            "Upstream response is malformed: {0}".format(problem),
            ambiguous=False,
            status_code=response.status_code,
            request=response.request,
        )

    def _hospital_from_payload(
        self,
        data: Any,
        error_cls: Type[HospitalAPIError],
        response: httpx.Response,
    ) -> UpstreamHospital:
        """Validate and extract a single-hospital payload.

        A hospital object is accepted when it is a JSON object carrying an
        ``id``; extra fields are preserved in ``raw`` for forward
        compatibility.
        """
        if not isinstance(data, Mapping):
            raise self._structure_error(
                error_cls,
                "expected a hospital object, got {0}".format(_type_name(data)),
                response,
            )
        if "id" not in data:
            raise self._structure_error(
                error_cls, "expected a hospital object with an 'id'", response
            )
        return UpstreamHospital(
            id=data.get("id"),
            name=data.get("name"),
            address=data.get("address"),
            phone=data.get("phone"),
            raw=dict(data),
        )

    def _hospitals_from_payload(
        self,
        data: Any,
        error_cls: Type[HospitalAPIError],
        response: httpx.Response,
    ) -> List[UpstreamHospital]:
        """Validate and extract a batch payload.

        Accepts either a bare list of hospital objects or an object wrapping
        one under a ``hospitals``/``data`` key, since the exact envelope is
        not guaranteed.
        """
        if isinstance(data, Mapping):
            unwrapped: Any = None
            for key in ("hospitals", "data", "results"):
                if key in data:
                    unwrapped = data[key]
                    break
            if unwrapped is None:
                raise self._structure_error(
                    error_cls,
                    "expected an object containing a 'hospitals' list",
                    response,
                )
            data = unwrapped
        if isinstance(data, str) or not isinstance(data, Sequence):
            raise self._structure_error(
                error_cls,
                "expected a list of hospitals, got {0}".format(_type_name(data)),
                response,
            )
        hospitals: List[UpstreamHospital] = []
        for index, item in enumerate(data):
            if not isinstance(item, Mapping):
                raise self._structure_error(
                    error_cls,
                    "hospital #{0} in the batch is not an object".format(index),
                    response,
                )
            hospitals.append(
                UpstreamHospital(
                    id=item.get("id"),
                    name=item.get("name"),
                    address=item.get("address"),
                    phone=item.get("phone"),
                    raw=dict(item),
                )
            )
        return hospitals

    # -- small helpers -------------------------------------------------------

    @staticmethod
    def _require_uuid(value: Union[str, uuid.UUID]) -> str:
        """Validate and normalize a batch id to its canonical string form.

        Args:
            value: A UUID string or :class:`uuid.UUID`.

        Returns:
            The canonical 36-character UUID string.

        Raises:
            ValueError: If the value does not parse as a UUID.
        """
        if isinstance(value, uuid.UUID):
            return str(value)
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(
                "batch_id must be a valid UUID, got {0!r}".format(value)
            ) from exc


def _type_name(value: Any) -> str:
    """Human-readable type name for error messages."""
    return type(value).__name__
