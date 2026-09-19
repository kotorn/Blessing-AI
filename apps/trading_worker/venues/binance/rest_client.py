import hashlib
import hmac
import asyncio
import time
import math
import os
import aiohttp
import logging
from typing import Dict, Any, Optional
from urllib.parse import urlencode

from .config import BinanceEnvironment, get_rest_url, PAPI_REST_URL, is_portfolio_margin_enabled
from .clock import BinanceClock
from .models import (
    BinanceAuthenticationError,
    BinanceDefinitiveRejection,
    BinanceRateLimitError,
    BinanceTimestampError,
    BinanceTransportAmbiguity,
)

logger = logging.getLogger("blessing.binance.rest_client")


# Binance only documents a finite set of errors as deterministic order
# rejections.  Any other error must remain ambiguous for a mutable request:
# the exchange may have accepted the request before returning the error.
_DEFINITIVE_REJECTION_CODES = {
    -1100,  # ILLEGAL_CHARS
    -1101,  # TOO_MANY_PARAMETERS
    -1102,  # MANDATORY_PARAM_EMPTY_OR_MALFORMED
    -1103,  # UNKNOWN_PARAM
    -1104,  # UNREAD_PARAMETERS
    -1105,  # PARAM_EMPTY
    -1106,  # PARAM_NOT_REQUIRED
    -1111,  # BAD_PRECISION
    -1112,  # NO_DEPTH
    -1114,  # TIF_NOT_REQUIRED
    -1115,  # INVALID_TIF
    -1116,  # INVALID_ORDER_TYPE
    -1117,  # INVALID_SIDE
    -1118,  # EMPTY_NEW_CL_ORD_ID
    -1119,  # EMPTY_ORG_CL_ORD_ID
    -1121,  # BAD_SYMBOL
    -1125,  # INVALID_LISTEN_KEY
    -1013,  # INVALID_MESSAGE / filter rejection
    -2010,  # NEW_ORDER_REJECTED
    -2011,  # CANCEL_REJECTED
    -2013,  # NO_SUCH_ORDER (query/cancel resolution can treat this as absent)
    -2019,  # MARGIN_NOT_SUFFICIENT
    -2020,  # UNABLE_TO_FILL
    -2021,  # ORDER_WOULD_IMMEDIATELY_TRIGGER
    -2022,  # REDUCE_ONLY_REJECT
    -4164,  # MIN_NOTIONAL
    -4165,  # TARGET_STRATEGY_INVALID
    -4003,  # QTY_LESS_THAN_ZERO
    -4004,  # QTY_LESS_THAN_MIN_QTY
    -4005,  # QTY_GREATER_THAN_MAX_QTY
    -4014,  # PRICE_NOT_INCREASED_BY_TICK_SIZE
    -4023,  # QTY_NOT_INCREASED_BY_STEP_SIZE
}
_RATE_LIMIT_CODES = {-1003, -1008, -1015}

# Keep the fixed-host transport from becoming a generic Binance request
# proxy.  Every path is reviewed for the worker's USDⓈ-M read, private-stream,
# reconciliation, and order lifecycle operations.
_ALLOWED_REQUEST_METHODS: dict[str, frozenset[str]] = {
    "/fapi/v1/time": frozenset({"GET"}),
    "/fapi/v1/exchangeInfo": frozenset({"GET"}),
    "/fapi/v1/positionSide/dual": frozenset({"GET"}),
    "/fapi/v2/account": frozenset({"GET"}),
    "/fapi/v2/positionRisk": frozenset({"GET"}),
    "/fapi/v1/openOrders": frozenset({"GET"}),
    "/fapi/v1/userTrades": frozenset({"GET"}),
    "/fapi/v1/income": frozenset({"GET"}),
    "/fapi/v1/premiumIndex": frozenset({"GET"}),
    "/fapi/v1/ticker/bookTicker": frozenset({"GET"}),
    "/fapi/v1/order": frozenset({"GET", "POST", "PUT", "DELETE"}),
    "/fapi/v1/listenKey": frozenset({"POST", "PUT", "DELETE"}),
    # Portfolio Margin (PAPI) endpoints
    "/papi/v1/account": frozenset({"GET"}),
    "/papi/v1/balance": frozenset({"GET"}),
    "/papi/v1/um/account": frozenset({"GET"}),
    "/papi/v1/um/positionSide/dual": frozenset({"GET"}),
    "/papi/v1/um/positionRisk": frozenset({"GET"}),
    "/papi/v1/um/openOrders": frozenset({"GET"}),
    "/papi/v1/um/userTrades": frozenset({"GET"}),
    "/papi/v1/um/income": frozenset({"GET"}),
    "/papi/v1/um/order": frozenset({"GET", "POST", "PUT", "DELETE"}),
    "/papi/v1/um/leverage": frozenset({"POST"}),
    "/papi/v1/um/leverageBracket": frozenset({"GET"}),
    "/papi/v1/listenKey": frozenset({"POST", "PUT", "DELETE"}),
}


def _numeric_error_code(value: Any, fallback: int) -> int:
    """Normalize Binance's numeric error code without trusting response types."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


class BinanceAPIError(Exception):
    def __init__(
        self,
        status: int,
        code: Optional[int],
        message: str,
        raw_data: Any,
        headers: Optional[Dict[str, str]] = None,
    ):
        super().__init__(f"Binance API Error {status} (code {code}): {message}")
        self.status = status
        self.code = code
        self.error_message = message
        self.raw_data = raw_data
        self.headers = headers or {}

class BinanceRestClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        env: BinanceEnvironment,
        *,
        read_only: bool = False,
        portfolio_margin: Optional[bool] = None,
    ):
        if not isinstance(env, BinanceEnvironment):
            raise ValueError("Binance REST execution client requires TESTNET or MAINNET")
        self.api_key = "".join(str(api_key or "").split())
        self.api_secret = "".join(str(api_secret or "").split())
        self.env = env
        if portfolio_margin is None:
            portfolio_margin = is_portfolio_margin_enabled()
        self.portfolio_margin = bool(portfolio_margin)
        # A preflight adapter gets a transport-level read-only boundary in
        # addition to the adapter method guards.  This prevents a future
        # preflight code path from reaching the order endpoint accidentally.
        self.read_only = bool(read_only)
        self.order_endpoint_attempts = 0
        self.base_url = get_rest_url(env)
        self.clock = BinanceClock(self.base_url)
        self.session: Optional[aiohttp.ClientSession] = None
        try:
            timeout_seconds = float(os.getenv("BINANCE_REQUEST_TIMEOUT_SEC", "10"))
        except (TypeError, ValueError):
            timeout_seconds = 10.0
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            timeout_seconds = 10.0
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._throttle_until = 0.0
        self._rate_limit_failures = 0

    async def _respect_rate_limit(self) -> None:
        delay = self._throttle_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def _record_rate_limit(self, headers: Dict[str, str]) -> None:
        raw_retry_after = headers.get("Retry-After") or headers.get("retry-after")
        retry_after_seconds: Optional[float] = None
        if raw_retry_after is not None:
            try:
                parsed = float(raw_retry_after)
                if parsed > 0:
                    retry_after_seconds = parsed
            except (TypeError, ValueError):
                pass

        self._rate_limit_failures = min(self._rate_limit_failures + 1, 6)
        # Retry-After is authoritative when provided.  Binance can omit it,
        # so use a bounded exponential delay rather than immediately sending
        # another request into a rate-limit storm.
        delay = retry_after_seconds or min(
            30.0, 2.0 ** (self._rate_limit_failures - 1)
        )
        self._throttle_until = max(self._throttle_until, time.monotonic() + delay)

    async def init_session(self):
        if not self.session:
            headers = {}
            if self.api_key:
                headers["X-MBX-APIKEY"] = self.api_key
            self.session = aiohttp.ClientSession(
                headers=headers,
                timeout=self.timeout,
            )
        if not await self.clock.synchronize(self.session):
            raise BinanceTransportAmbiguity("Unable to synchronize Binance server clock")

    async def close(self):
        if self.session:
            await self.session.close()

    def _sign_payload(self, payload: Dict[str, Any]) -> str:
        query_string = urlencode(payload, doseq=True)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return signature

    async def request(
        self,
        method: str,
        path: str,
        signed: bool = False,
        **kwargs,
    ) -> Any:
        method_upper = method.upper()
        allowed_methods = _ALLOWED_REQUEST_METHODS.get(path)
        if allowed_methods is None or method_upper not in allowed_methods:
            raise ValueError(
                "Binance request is outside the fixed USDⓈ-M endpoint allowlist: "
                f"{method_upper} {path}"
            )
        if path in ("/fapi/v1/order", "/papi/v1/um/order") and method_upper in ("POST", "PUT", "DELETE"):
            self.order_endpoint_attempts += 1
            if self.read_only:
                raise PermissionError(
                    "Read-only Binance client cannot call the order endpoint"
                )
        if not self.session:
            await self.init_session()

        base_params = dict(kwargs.pop("params", {}) or {})
        # A timestamp retry is safe for read-only requests only. Mutable calls
        # intentionally receive the error after clock resynchronization so a
        # caller never blindly submits an order twice.
        timestamp_retry_used = False
        if path.startswith("/papi/"):
            url = f"{PAPI_REST_URL}{path}"
        else:
            url = f"{self.base_url}{path}"

        while True:
            await self._respect_rate_limit()
            params = dict(base_params)
            if signed:
                params["timestamp"] = self.clock.get_signed_timestamp()
                params["recvWindow"] = self.clock.recv_window
                params["signature"] = self._sign_payload(params)

            request_kwargs = dict(kwargs)
            request_kwargs["params"] = params
            try:
                async with self.session.request(method_upper, url, **request_kwargs) as resp:
                    try:
                        data = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise BinanceTransportAmbiguity(
                            f"Invalid Binance response for {method_upper} {path}: {exc}"
                        ) from exc

                    if resp.status >= 400:
                        code = data.get("code") if isinstance(data, dict) else None
                        message = data.get("msg", str(data)) if isinstance(data, dict) else str(data)
                        headers = {str(k): str(v) for k, v in resp.headers.items()}
                        logger.error(
                            "Binance REST error status=%s path=%s code=%s",
                            resp.status,
                            path,
                            code,
                        )

                        if resp.status in {408, 500, 502, 503, 504}:
                            raise BinanceTransportAmbiguity(
                                f"Binance returned an indeterminate HTTP {resp.status} for "
                                f"{method_upper} {path}: {message}"
                            )

                        numeric_code = _numeric_error_code(code, resp.status)
                        if numeric_code == -1021:
                            logger.warning("Timestamp error. Resynchronizing clock.")
                            if not await self.clock.synchronize(self.session):
                                raise BinanceTransportAmbiguity(
                                    f"Unable to resynchronize Binance clock for {method_upper} {path}"
                                )
                            if method_upper in {"GET", "HEAD"} and not timestamp_retry_used:
                                timestamp_retry_used = True
                                continue
                            raise BinanceTimestampError(numeric_code, message)
                        if numeric_code in (-2014, -2015) or resp.status in {401, 403}:
                            raise BinanceAuthenticationError(numeric_code, message)
                        if numeric_code in _RATE_LIMIT_CODES or resp.status in {418, 429}:
                            self._record_rate_limit(headers)
                            raise BinanceRateLimitError(numeric_code, message, headers=headers)
                        if numeric_code in _DEFINITIVE_REJECTION_CODES and not (
                            numeric_code == -2013 and method_upper == "POST"
                        ):
                            raise BinanceDefinitiveRejection(numeric_code, message)
                        # Unknown Binance errors are not proof that a mutable
                        # request was rejected.  Preserve ambiguity so the
                        # adapter queries clientOrderId and reconciles instead
                        # of blindly resubmitting or recording REJECTED.
                        raise BinanceTransportAmbiguity(
                            f"Unknown Binance error {numeric_code} for {method_upper} {path}: {message}"
                        )
                    return data
            except (
                BinanceAPIError,
                BinanceAuthenticationError,
                BinanceDefinitiveRejection,
                BinanceRateLimitError,
                BinanceTimestampError,
                BinanceTransportAmbiguity,
            ):
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                # The exchange may have accepted a mutable request before the
                # transport failed. The adapter must reconcile/query, never
                # transparently retry it.
                raise BinanceTransportAmbiguity(
                    f"Transport ambiguity for {method_upper} {path}: {exc}"
                ) from exc
