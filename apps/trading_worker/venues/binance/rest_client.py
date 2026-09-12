import hashlib
import hmac
import asyncio
import time
import aiohttp
import logging
from typing import Dict, Any, Optional
from urllib.parse import urlencode

from .config import BinanceEnvironment, get_rest_url
from .clock import BinanceClock
from .models import (
    BinanceAuthenticationError,
    BinanceDefinitiveRejection,
    BinanceRateLimitError,
    BinanceTimestampError,
    BinanceTransportAmbiguity,
)

logger = logging.getLogger("blessing.binance.rest_client")

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
    def __init__(self, api_key: str, api_secret: str, env: BinanceEnvironment):
        if env != BinanceEnvironment.TESTNET:
            raise ValueError("Binance REST execution client is restricted to Testnet")
        self.api_key = api_key
        self.api_secret = api_secret
        self.env = env
        self.base_url = get_rest_url(env)
        self.clock = BinanceClock(self.base_url)
        self.session: Optional[aiohttp.ClientSession] = None
        self._throttle_until = 0.0

    async def _respect_rate_limit(self) -> None:
        delay = self._throttle_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def _record_rate_limit(self, headers: Dict[str, str]) -> None:
        raw_retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if raw_retry_after is None:
            return
        try:
            retry_after_seconds = float(raw_retry_after)
        except ValueError:
            return
        if retry_after_seconds > 0:
            self._throttle_until = max(
                self._throttle_until,
                time.monotonic() + retry_after_seconds,
            )

    async def init_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(headers={"X-MBX-APIKEY": self.api_key})
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
        if not self.session:
            await self.init_session()

        base_params = dict(kwargs.pop("params", {}) or {})
        method_upper = method.upper()
        # A timestamp retry is safe for read-only requests only. Mutable calls
        # intentionally receive the error after clock resynchronization so a
        # caller never blindly submits an order twice.
        timestamp_retry_used = False
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
                        logger.error("Binance REST Error: %s %s - %s", resp.status, path, data)

                        if resp.status in {408, 500, 502, 503, 504}:
                            raise BinanceTransportAmbiguity(
                                f"Binance returned an indeterminate HTTP {resp.status} for "
                                f"{method_upper} {path}: {message}"
                            )

                        if code == -1021:
                            logger.warning("Timestamp error. Resynchronizing clock.")
                            await self.clock.synchronize(self.session)
                            if method_upper in {"GET", "HEAD"} and not timestamp_retry_used:
                                timestamp_retry_used = True
                                continue
                            raise BinanceTimestampError(code, message)
                        if code in (-2014, -2015):
                            raise BinanceAuthenticationError(code, message)
                        if code in (-1003, -1015) or resp.status in {418, 429}:
                            self._record_rate_limit(headers)
                            raise BinanceRateLimitError(code or resp.status, message, headers=headers)
                        raise BinanceDefinitiveRejection(code or resp.status, message)
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
