import asyncio
import time

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.models import (
    BinanceAuthenticationError,
    BinanceDefinitiveRejection,
    BinanceRateLimitError,
    BinanceTimestampError,
    BinanceTransportAmbiguity,
)
from apps.trading_worker.venues.binance.rest_client import BinanceRestClient


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self.payload = payload if payload is not None else {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def make_client(responses):
    client = BinanceRestClient("key", "secret", BinanceEnvironment.TESTNET)
    session = FakeSession(responses)
    client.session = session
    sync_calls = []

    async def synchronize(_session):
        sync_calls.append(True)
        return True

    client.clock.synchronize = synchronize
    return client, session, sync_calls


@pytest.mark.asyncio
async def test_transport_rejects_unreviewed_endpoint_before_network_call():
    client, session, _ = make_client([])

    with pytest.raises(ValueError, match="endpoint allowlist"):
        await client.request("GET", "/fapi/v1/leverageBracket")

    assert session.calls == []


@pytest.mark.asyncio
async def test_mutable_timestamp_error_resyncs_without_resubmitting():
    client, session, sync_calls = make_client(
        [FakeResponse(400, {"code": -1021, "msg": "timestamp outside recvWindow"})]
    )

    with pytest.raises(BinanceTimestampError):
        await client.request("POST", "/fapi/v1/order", signed=True)

    assert len(session.calls) == 1
    assert len(sync_calls) == 1


@pytest.mark.asyncio
async def test_readonly_timestamp_error_retries_once_after_resync():
    client, session, sync_calls = make_client(
        [
            FakeResponse(400, {"code": "-1021", "msg": "timestamp"}),
            FakeResponse(200, {"serverTime": 123}),
        ]
    )

    result = await client.request("GET", "/fapi/v1/time", signed=True)

    assert result == {"serverTime": 123}
    assert len(session.calls) == 2
    assert len(sync_calls) == 1


@pytest.mark.asyncio
async def test_timestamp_resync_failure_becomes_transport_ambiguity():
    client, session, _ = make_client(
        [FakeResponse(400, {"code": -1021, "msg": "timestamp"})]
    )

    async def failed_sync(_session):
        return False

    client.clock.synchronize = failed_sync
    with pytest.raises(BinanceTransportAmbiguity):
        await client.request("POST", "/fapi/v1/order", signed=True)
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload",
    [
        (401, {"code": -2015, "msg": "invalid api-key"}),
        (403, {"msg": "forbidden"}),
    ],
)
async def test_authentication_failures_are_not_retried(status, payload):
    client, session, _ = make_client([FakeResponse(status, payload)])

    with pytest.raises(BinanceAuthenticationError):
        await client.request("GET", "/fapi/v2/account", signed=True)
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_rate_limit_records_retry_after_without_resubmission():
    client, session, _ = make_client(
        [
            FakeResponse(
                429,
                {"code": -1003, "msg": "too many requests"},
                headers={"Retry-After": "2"},
            )
        ]
    )

    with pytest.raises(BinanceRateLimitError):
        await client.request("POST", "/fapi/v1/order", signed=True)
    assert len(session.calls) == 1
    assert client._throttle_until >= time.monotonic() + 1.0


@pytest.mark.asyncio
async def test_transport_timeout_is_ambiguous_and_not_retried():
    client, session, _ = make_client([asyncio.TimeoutError()])

    with pytest.raises(BinanceTransportAmbiguity):
        await client.request("DELETE", "/fapi/v1/order", signed=True)
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [-1006, -1007])
async def test_unknown_binance_error_is_ambiguous_for_mutable_requests(code):
    client, session, _ = make_client(
        [FakeResponse(400, {"code": code, "msg": "indeterminate exchange error"})]
    )

    with pytest.raises(BinanceTransportAmbiguity):
        await client.request("POST", "/fapi/v1/order", signed=True)

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_known_order_rejection_is_definitive():
    client, session, _ = make_client(
        [FakeResponse(400, {"code": -2010, "msg": "new order rejected"})]
    )

    with pytest.raises(BinanceDefinitiveRejection) as exc_info:
        await client.request("POST", "/fapi/v1/order", signed=True)

    assert exc_info.value.code == -2010
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_no_such_order_on_new_order_is_ambiguous_not_a_rejection_record():
    client, session, _ = make_client(
        [FakeResponse(400, {"code": -2013, "msg": "order does not exist"})]
    )

    with pytest.raises(BinanceTransportAmbiguity):
        await client.request("POST", "/fapi/v1/order", signed=True)

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_rate_limit_code_without_header_uses_bounded_backoff():
    client, session, _ = make_client(
        [FakeResponse(418, {"code": -1008, "msg": "server busy"})]
    )

    with pytest.raises(BinanceRateLimitError):
        await client.request("POST", "/fapi/v1/order", signed=True)

    assert len(session.calls) == 1
    assert client._throttle_until > time.monotonic()
