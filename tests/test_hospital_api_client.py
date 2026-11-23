"""Tests for the Hospital Directory API client.

HTTP-level behaviour (status codes, payloads, retries on 5xx) is mocked with
``respx`` against the client's real ``httpx.AsyncClient``, so the full retry
and error-normalization paths run end to end. Transport-level failures
(connection errors, timeouts) are injected through the client's documented
``transport`` seam with ``httpx.MockTransport``, since ``respx`` expresses
responses rather than socket-level exceptions.

No test touches the network: every route is mocked, and every injected
transport raises immediately.
"""

import json
import uuid
from typing import Iterator, List

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import app.clients.hospital_api_client as client_module
from app.clients.hospital_api_client import (
    ActivationResult,
    BatchActivationError,
    BatchRetrievalError,
    HospitalAPIClient,
    HospitalCreationError,
    UpstreamBatchState,
    UpstreamErrorKind,
    UpstreamHospital,
)
from app.core.config import Settings
from app.main import create_app

BATCH_ID = uuid.UUID("b4e9f7a3-7b3b-4f1f-9a3a-5f5b8a9c3f2a")
BASE_URL = "https://hospital-directory.onrender.com"
BATCH_PATH = "/hospitals/batch/{0}".format(BATCH_ID)
ACTIVATE_PATH = "/hospitals/batch/{0}/activate".format(BATCH_ID)

HOSPITAL_1 = {
    "id": 1,
    "name": "St. Mary",
    "address": "1 Hill St",
    "phone": "555-0101",
    "creation_batch_id": str(BATCH_ID),
    "active": False,
}
HOSPITAL_2 = {
    "id": 2,
    "name": "Grace Hospital",
    "address": "2 Lake Rd",
    "phone": None,
    "creation_batch_id": str(BATCH_ID),
    "active": False,
}


@pytest.fixture()
def client_settings() -> Settings:
    """Settings pinned to the mocked base URL and the default retry budget.

    The default budget is one retry after the initial attempt (two total
    attempts), matching the shipped ``Settings`` default.
    """
    return Settings(
        hospital_api_base_url=BASE_URL,
        hospital_api_max_retries=1,
    )


@pytest.fixture()
async def hospital_api(
    client_settings: Settings,
) -> Iterator[HospitalAPIClient]:
    """A client owning its real ``httpx.AsyncClient``; closed after the test."""
    client = HospitalAPIClient(client_settings)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture()
def recorded_sleeps(monkeypatch) -> List[float]:
    """Record backoff delays instead of actually sleeping, to keep the suite fast."""
    delays: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    return delays


def _last_payload(route: respx.Route) -> dict:
    """The JSON request body of the route's last recorded call."""
    return json.loads(route.calls.last.request.content)


# -- hospital creation -------------------------------------------------------


async def test_create_hospital_success(hospital_api: HospitalAPIClient) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/hospitals/").respond(200, json=HOSPITAL_1)
        hospital = await hospital_api.create_hospital(
            "St. Mary", "1 Hill St", "555-0101", batch_id=BATCH_ID
        )

    assert isinstance(hospital, UpstreamHospital)
    assert hospital.id == 1
    assert hospital.name == "St. Mary"
    assert hospital.address == "1 Hill St"
    assert hospital.phone == "555-0101"
    assert hospital.raw == HOSPITAL_1
    assert route.call_count == 1


async def test_create_hospital_sends_verified_payload(
    hospital_api: HospitalAPIClient,
) -> None:
    """The payload matches the live API contract: batch UUID always present."""
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/hospitals/").respond(200, json=HOSPITAL_1)
        await hospital_api.create_hospital(
            "St. Mary", "1 Hill St", "555-0101", batch_id=BATCH_ID
        )
        payload = _last_payload(route)
        request = route.calls.last.request

    assert payload == {
        "name": "St. Mary",
        "address": "1 Hill St",
        "phone": "555-0101",
        "creation_batch_id": str(BATCH_ID),
    }
    assert request.url == httpx.URL(BASE_URL + "/hospitals/")
    assert request.method == "POST"
    # A UUID input normalizes to its canonical string form in the payload.
    assert payload["creation_batch_id"] == str(uuid.UUID(payload["creation_batch_id"]))


async def test_create_hospital_accepts_uuid_string(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/hospitals/").respond(200, json=HOSPITAL_1)
        await hospital_api.create_hospital(
            "St. Mary", "1 Hill St", batch_id=str(BATCH_ID)
        )
        assert _last_payload(route)["creation_batch_id"] == str(BATCH_ID)


async def test_create_hospital_omits_optional_phone(
    hospital_api: HospitalAPIClient,
) -> None:
    """A ``None`` phone is omitted from the payload entirely, not sent as null."""
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/hospitals/").respond(200, json=HOSPITAL_2)
        hospital = await hospital_api.create_hospital(
            "Grace Hospital", "2 Lake Rd", batch_id=BATCH_ID
        )

    payload = _last_payload(route)
    assert "phone" not in payload
    assert hospital.phone is None


async def test_create_hospital_rejects_invalid_batch_id(
    hospital_api: HospitalAPIClient,
) -> None:
    with pytest.raises(ValueError, match="valid UUID"):
        await hospital_api.create_hospital(
            "St. Mary", "1 Hill St", batch_id="not-a-uuid"
        )


async def test_create_hospital_rejects_blank_name_and_address(
    hospital_api: HospitalAPIClient,
) -> None:
    with pytest.raises(ValueError):
        await hospital_api.create_hospital(
            "   ", "1 Hill St", batch_id=BATCH_ID
        )
    with pytest.raises(ValueError):
        await hospital_api.create_hospital(
            "St. Mary", "", batch_id=BATCH_ID
        )


# -- batch retrieval ----------------------------------------------------------


async def test_get_batch_hospitals_success(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(BATCH_PATH).respond(
            200, json=[HOSPITAL_1, HOSPITAL_2]
        )
        state = await hospital_api.get_batch_hospitals(BATCH_ID)
        request = route.calls.last.request

    assert isinstance(state, UpstreamBatchState)
    assert state.batch_id == str(BATCH_ID)
    assert [hospital.id for hospital in state.hospitals] == [1, 2]
    assert [hospital.name for hospital in state.hospitals] == [
        "St. Mary",
        "Grace Hospital",
    ]
    assert request.method == "GET"
    assert route.call_count == 1


async def test_get_batch_hospitals_empty_batch(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(BATCH_PATH).respond(200, json=[])
        state = await hospital_api.get_batch_hospitals(BATCH_ID)

    assert state.hospitals == []


async def test_get_batch_hospitals_accepts_wrapped_payloads(
    hospital_api: HospitalAPIClient,
) -> None:
    """The envelope is not guaranteed upstream: list or wrapped object."""
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(BATCH_PATH)
        route.mock(side_effect=[
            httpx.Response(200, json={"hospitals": [HOSPITAL_1, HOSPITAL_2]}),
            httpx.Response(200, json={"data": [HOSPITAL_1]}),
        ])
        wrapped = await hospital_api.get_batch_hospitals(BATCH_ID)
        direct = await hospital_api.get_batch_hospitals(BATCH_ID)

    assert [hospital.id for hospital in wrapped.hospitals] == [1, 2]
    assert [hospital.id for hospital in direct.hospitals] == [1]
    assert route.call_count == 2


async def test_get_batch_hospitals_rejects_invalid_batch_id(
    hospital_api: HospitalAPIClient,
) -> None:
    with pytest.raises(ValueError, match="valid UUID"):
        await hospital_api.get_batch_hospitals("not-a-uuid")


# -- batch activation ---------------------------------------------------------


async def test_activate_batch_success(hospital_api: HospitalAPIClient) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.patch(ACTIVATE_PATH).respond(
            200,
            json={"activated_count": 3, "message": "activated 3 hospitals"},
        )
        result = await hospital_api.activate_batch(BATCH_ID)
        request = route.calls.last.request

    assert isinstance(result, ActivationResult)
    assert result.batch_id == str(BATCH_ID)
    assert result.activated_count == 3
    assert result.message == "activated 3 hospitals"
    assert request.method == "PATCH"
    assert route.calls.last.request.content == b""  # no request body
    assert route.call_count == 1


async def test_activate_batch_rejects_invalid_batch_id(
    hospital_api: HospitalAPIClient,
) -> None:
    with pytest.raises(ValueError, match="valid UUID"):
        await hospital_api.activate_batch("not-a-uuid")


# -- HTTP error normalization --------------------------------------------------


async def test_create_hospital_bad_request_not_retried(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/hospitals/").respond(
            400, json={"detail": "name too long"}
        )
        with pytest.raises(HospitalCreationError) as exc_info:
            await hospital_api.create_hospital(
                "St. Mary", "1 Hill St", batch_id=BATCH_ID
            )

    error = exc_info.value
    assert error.kind is UpstreamErrorKind.BAD_REQUEST
    assert error.status_code == 400
    assert error.ambiguous is False
    assert "name too long" in error.message
    assert route.call_count == 1  # 4xx is definitive: never retried


async def test_get_batch_hospitals_not_found(hospital_api: HospitalAPIClient) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(BATCH_PATH).respond(
            404, json={"detail": "batch not found"}
        )
        with pytest.raises(BatchRetrievalError) as exc_info:
            await hospital_api.get_batch_hospitals(BATCH_ID)

    error = exc_info.value
    assert error.kind is UpstreamErrorKind.NOT_FOUND
    assert error.status_code == 404
    assert error.ambiguous is False
    assert route.call_count == 1


async def test_activate_batch_validation_error(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.patch(ACTIVATE_PATH).respond(
            422, json={"detail": "batch not in creatable state"}
        )
        with pytest.raises(BatchActivationError) as exc_info:
            await hospital_api.activate_batch(BATCH_ID)

    error = exc_info.value
    assert error.kind is UpstreamErrorKind.VALIDATION_ERROR
    assert error.status_code == 422
    assert error.ambiguous is False
    assert route.call_count == 1


async def test_create_hospital_server_error_is_ambiguous(
    hospital_api: HospitalAPIClient,
) -> None:
    """A 5xx on create must not be retried and must be flagged ambiguous."""
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post("/hospitals/").respond(500, json={"detail": "boom"})
        with pytest.raises(HospitalCreationError) as exc_info:
            await hospital_api.create_hospital(
                "St. Mary", "1 Hill St", batch_id=BATCH_ID
            )

    error = exc_info.value
    assert error.kind is UpstreamErrorKind.SERVER_ERROR
    assert error.status_code == 500
    assert error.ambiguous is True
    assert route.call_count == 1


async def test_activate_batch_server_error_is_ambiguous(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.patch(ACTIVATE_PATH).respond(
            500, json={"detail": "boom"}
        )
        with pytest.raises(BatchActivationError) as exc_info:
            await hospital_api.activate_batch(BATCH_ID)

    error = exc_info.value
    assert error.kind is UpstreamErrorKind.SERVER_ERROR
    assert error.ambiguous is True
    assert route.call_count == 1


async def test_fetch_retries_transient_5xx_then_succeeds(
    hospital_api: HospitalAPIClient,
    recorded_sleeps: List[float],
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(BATCH_PATH)
        route.mock(side_effect=[
            httpx.Response(503, json={"detail": "unavailable"}),
            httpx.Response(200, json=[HOSPITAL_1]),
        ])
        state = await hospital_api.get_batch_hospitals(BATCH_ID)

    assert [hospital.id for hospital in state.hospitals] == [1]
    # Default budget: one retry after the initial attempt -> two calls.
    assert route.call_count == 2
    assert len(recorded_sleeps) == 1


async def test_fetch_retries_exhausted_5xx(
    hospital_api: HospitalAPIClient,
    recorded_sleeps: List[float],
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(BATCH_PATH)
        route.mock(side_effect=[
            httpx.Response(500, json={"detail": "boom"}),
            httpx.Response(500, json={"detail": "boom"}),
        ])
        with pytest.raises(BatchRetrievalError) as exc_info:
            await hospital_api.get_batch_hospitals(BATCH_ID)

    error = exc_info.value
    assert error.kind is UpstreamErrorKind.SERVER_ERROR
    assert error.status_code == 500
    assert error.ambiguous is False  # read-only operation
    assert route.call_count == 2
    assert len(recorded_sleeps) == 1


async def test_fetch_does_not_retry_non_transient_5xx(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(BATCH_PATH).respond(
            501, json={"detail": "not implemented"}
        )
        with pytest.raises(BatchRetrievalError) as exc_info:
            await hospital_api.get_batch_hospitals(BATCH_ID)

    assert exc_info.value.kind is UpstreamErrorKind.SERVER_ERROR
    assert route.call_count == 1


# -- backoff shape -------------------------------------------------------------


async def test_backoff_is_bounded_exponential_with_jitter(
    client_settings: Settings,
    recorded_sleeps: List[float],
) -> None:
    # Two retries (three attempts) so both backoff steps are observed.
    wide_settings = client_settings.model_copy(update={"hospital_api_max_retries": 2})
    client = HospitalAPIClient(wide_settings)
    try:
        with respx.mock(base_url=BASE_URL) as router:
            route = router.get(BATCH_PATH)
            route.mock(side_effect=[
                httpx.Response(500, json={}),
                httpx.Response(500, json={}),
                httpx.Response(200, json=[]),
            ])
            await client.get_batch_hospitals(BATCH_ID)
    finally:
        await client.aclose()

    first, second = recorded_sleeps
    # base 0.5s: attempt 1 sleeps in [0.25, 0.5], attempt 2 in [0.5, 1.0].
    assert 0.25 <= first <= 0.5
    assert 0.5 <= second <= 1.0
    assert second >= first


# -- transport-level failures --------------------------------------------------


def _failing_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_connect_timeout_on_create_retried_and_not_ambiguous(
    client_settings: Settings,
    recorded_sleeps: List[float],
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectTimeout("simulated", request=request)

    client = HospitalAPIClient(
        client_settings, transport=_failing_transport(handler)
    )
    try:
        with pytest.raises(HospitalCreationError) as exc_info:
            await client.create_hospital("St. Mary", "1 Hill St", batch_id=BATCH_ID)
    finally:
        await client.aclose()

    error = exc_info.value
    # A connect timeout proves the request never reached the server: safe to
    # retry (default budget: 1 retry -> 2 attempts) and never ambiguous.
    assert error.kind is UpstreamErrorKind.TIMEOUT
    assert error.ambiguous is False
    assert len(calls) == 2
    assert len(recorded_sleeps) == 1


async def test_read_timeout_on_create_not_retried_and_ambiguous(
    client_settings: Settings,
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("simulated", request=request)

    client = HospitalAPIClient(
        client_settings, transport=_failing_transport(handler)
    )
    try:
        with pytest.raises(HospitalCreationError) as exc_info:
            await client.create_hospital("St. Mary", "1 Hill St", batch_id=BATCH_ID)
    finally:
        await client.aclose()

    error = exc_info.value
    # The body may already have reached the server: one attempt, flagged
    # ambiguous so the processing layer reconciles via batch retrieval.
    assert error.kind is UpstreamErrorKind.TIMEOUT
    assert error.ambiguous is True
    assert len(calls) == 1


async def test_connect_error_on_activate_retried(
    client_settings: Settings,
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("simulated", request=request)

    client = HospitalAPIClient(
        client_settings, transport=_failing_transport(handler)
    )
    try:
        with pytest.raises(BatchActivationError) as exc_info:
            await client.activate_batch(BATCH_ID)
    finally:
        await client.aclose()

    error = exc_info.value
    assert error.kind is UpstreamErrorKind.CONNECTION_ERROR
    assert error.ambiguous is False
    assert len(calls) == 2


async def test_read_timeout_on_activate_not_retried_and_ambiguous(
    client_settings: Settings,
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("simulated", request=request)

    client = HospitalAPIClient(
        client_settings, transport=_failing_transport(handler)
    )
    try:
        with pytest.raises(BatchActivationError) as exc_info:
            await client.activate_batch(BATCH_ID)
    finally:
        await client.aclose()

    # Activation is not idempotent: an ambiguous outcome is never retried.
    assert exc_info.value.kind is UpstreamErrorKind.TIMEOUT
    assert exc_info.value.ambiguous is True
    assert len(calls) == 1


async def test_generic_request_error_not_retried(
    client_settings: Settings,
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadError("simulated protocol failure", request=request)

    client = HospitalAPIClient(
        client_settings, transport=_failing_transport(handler)
    )
    try:
        with pytest.raises(HospitalCreationError) as exc_info:
            await client.create_hospital("St. Mary", "1 Hill St", batch_id=BATCH_ID)
    finally:
        await client.aclose()

    error = exc_info.value
    assert error.kind is UpstreamErrorKind.CONNECTION_ERROR
    # Outcome unknown on a state-changing call -> ambiguous.
    assert error.ambiguous is True
    assert len(calls) == 1
    assert isinstance(error.cause, httpx.ReadError)


async def test_connect_timeout_on_fetch_retried(
    client_settings: Settings,
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectTimeout("simulated", request=request)

    client = HospitalAPIClient(
        client_settings, transport=_failing_transport(handler)
    )
    try:
        with pytest.raises(BatchRetrievalError) as exc_info:
            await client.get_batch_hospitals(BATCH_ID)
    finally:
        await client.aclose()

    # Read-only: even read timeouts are retried; connect timeouts are not
    # ambiguous for any operation. Default budget: 1 retry -> 2 attempts.
    assert exc_info.value.kind is UpstreamErrorKind.TIMEOUT
    assert exc_info.value.ambiguous is False
    assert len(calls) == 2


# -- malformed bodies and unexpected structures ---------------------------------


async def test_create_hospital_malformed_json(hospital_api: HospitalAPIClient) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/hospitals/").respond(200, text="not json")
        with pytest.raises(HospitalCreationError) as exc_info:
            await hospital_api.create_hospital(
                "St. Mary", "1 Hill St", batch_id=BATCH_ID
            )

    assert exc_info.value.kind is UpstreamErrorKind.DECODE_ERROR
    assert exc_info.value.status_code == 200
    assert exc_info.value.ambiguous is False


async def test_create_hospital_rejects_list_payload(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/hospitals/").respond(200, json=[HOSPITAL_1])
        with pytest.raises(HospitalCreationError) as exc_info:
            await hospital_api.create_hospital(
                "St. Mary", "1 Hill St", batch_id=BATCH_ID
            )

    assert exc_info.value.kind is UpstreamErrorKind.UNEXPECTED_RESPONSE


async def test_create_hospital_rejects_payload_without_id(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post("/hospitals/").respond(
            200, json={"name": "St. Mary", "address": "1 Hill St"}
        )
        with pytest.raises(HospitalCreationError) as exc_info:
            await hospital_api.create_hospital(
                "St. Mary", "1 Hill St", batch_id=BATCH_ID
            )

    assert exc_info.value.kind is UpstreamErrorKind.UNEXPECTED_RESPONSE


async def test_get_batch_hospitals_rejects_non_sequence(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(BATCH_PATH).respond(200, json="nope")
        with pytest.raises(BatchRetrievalError) as exc_info:
            await hospital_api.get_batch_hospitals(BATCH_ID)

    assert exc_info.value.kind is UpstreamErrorKind.UNEXPECTED_RESPONSE


async def test_get_batch_hospitals_rejects_unknown_envelope(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get(BATCH_PATH).respond(
            200, json={"unexpected": True}
        )
        with pytest.raises(BatchRetrievalError) as exc_info:
            await hospital_api.get_batch_hospitals(BATCH_ID)

    assert exc_info.value.kind is UpstreamErrorKind.UNEXPECTED_RESPONSE


async def test_activate_batch_rejects_non_integer_count(
    hospital_api: HospitalAPIClient,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.patch(ACTIVATE_PATH).respond(
            200, json={"activated_count": "three", "message": "ok"}
        )
        with pytest.raises(BatchActivationError) as exc_info:
            await hospital_api.activate_batch(BATCH_ID)

    assert exc_info.value.kind is UpstreamErrorKind.UNEXPECTED_RESPONSE


async def test_activate_batch_rejects_bool_count(
    hospital_api: HospitalAPIClient,
) -> None:
    # ``True`` is an ``int`` in Python; the client explicitly refuses it.
    with respx.mock(base_url=BASE_URL) as router:
        router.patch(ACTIVATE_PATH).respond(
            200, json={"activated_count": True}
        )
        with pytest.raises(BatchActivationError) as exc_info:
            await hospital_api.activate_batch(BATCH_ID)

    assert exc_info.value.kind is UpstreamErrorKind.UNEXPECTED_RESPONSE


# -- configuration and lifecycle -------------------------------------------------


def test_http_client_uses_configured_url_timeouts_and_pool(
    client_settings: Settings,
    hospital_api: HospitalAPIClient,
) -> None:
    http_client = hospital_api.http_client
    assert http_client.base_url == httpx.URL(BASE_URL)
    assert http_client.timeout.read == client_settings.hospital_api_timeout_seconds
    assert http_client.timeout.connect == (
        client_settings.hospital_api_connect_timeout_seconds
    )
    # The pool is owned by the transport; httpcore exposes its limits only
    # through private attributes, so reach through deliberately here.
    pool = hospital_api.http_client._transport._pool
    assert pool._max_connections == client_settings.hospital_api_max_concurrency
    assert (
        pool._max_keepalive_connections
        == client_settings.hospital_api_max_concurrency
    )


def test_lifespan_creates_and_closes_shared_client(settings: Settings) -> None:
    application = create_app(settings)
    with TestClient(application) as test_client:
        shared = application.state.hospital_api
        assert isinstance(shared, HospitalAPIClient)
        assert not shared.http_client.is_closed
        assert test_client.get("/health").status_code == 200
    # After the lifespan ends the client is closed and detached.
    assert shared.http_client.is_closed
    assert not hasattr(application.state, "hospital_api")


async def test_injected_client_lifecycle_is_external(
    client_settings: Settings,
) -> None:
    external = httpx.AsyncClient(base_url=BASE_URL)
    client = HospitalAPIClient(client_settings, client=external)
    try:
        assert client.http_client is external
        await client.aclose()  # no-op: the caller owns the injected client
        assert not external.is_closed
    finally:
        await external.aclose()


def test_injected_transport_lifecycle_is_owned(
    client_settings: Settings,
) -> None:
    client = HospitalAPIClient(client_settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})
    ))
    # Synchronous close is not possible here; assert ownership instead: the
    # built-in client was created (not injected) and is currently open.
    assert not client.http_client.is_closed
