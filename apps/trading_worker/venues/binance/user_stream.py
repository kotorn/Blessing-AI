import asyncio
import json
import logging
import math
import os
import random
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    websockets = None

from .config import BinanceEnvironment, get_ws_url, PAPI_WS_URL
from .models import BinanceAuthenticationError
from .rest_client import BinanceRestClient

logger = logging.getLogger("blessing.binance.user_stream")


class BinanceUserStream:
    BACKOFF_STEPS = [1.0, 2.0, 4.0, 8.0, 15.0, 30.0]
    STREAM_HEARTBEAT_INTERVAL_SEC = 15.0
    STREAM_HEARTBEAT_TIMEOUT_SEC = 5.0

    def __init__(
        self,
        rest_client: BinanceRestClient,
        env: BinanceEnvironment,
        on_disconnect=None,
        on_reconnected=None,
        on_authentication_failed=None,
    ):
        if not isinstance(env, BinanceEnvironment):
            raise ValueError("Binance user streams require TESTNET or MAINNET")
        self.rest_client = rest_client
        self.env = env
        if getattr(self.rest_client, "portfolio_margin", False):
            self.base_ws_url = PAPI_WS_URL
        else:
            self.base_ws_url = get_ws_url(env)
        self.listen_key = None
        self.ws = None
        self.keepalive_task: asyncio.Task | None = None
        self.listen_task: asyncio.Task | None = None
        self.reconnect_task: asyncio.Task | None = None
        self.is_connected = False
        self.connected_at: datetime | None = None
        self.last_event_at: datetime | None = None
        self.last_transport_heartbeat_at: datetime | None = None
        self.last_keepalive_at: datetime | None = None
        self.on_event = None
        self.on_disconnect = on_disconnect
        self.on_reconnected = on_reconnected
        self.on_authentication_failed = on_authentication_failed
        self.running = False
        self.authentication_failed = False

    def is_healthy(self) -> bool:
        """Require a connected stream with a bounded transport/event heartbeat."""
        if not self.is_connected or not self.running:
            return False
        # Socket establishment alone is not proof that the connection remains
        # usable. A received private event or a successful WebSocket
        # ping/pong is required before the execution readiness gate passes.
        candidates = [
            ts
            for ts in (self.last_event_at, self.last_transport_heartbeat_at)
            if ts is not None and getattr(ts, "tzinfo", None) is not None
        ]
        if not candidates:
            return False
        timestamp = max(candidates)
        try:
            max_age = float(os.getenv("PRIVATE_STREAM_MAX_AGE_SEC", "60"))
        except (TypeError, ValueError):
            max_age = 60.0
        if not math.isfinite(max_age) or max_age <= 0:
            max_age = 60.0
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        return 0 <= age <= max_age

    async def start(self, event_callback) -> bool:
        self.on_event = event_callback
        self.running = True
        self.authentication_failed = False
        connected = await self._connect()
        if not connected and self.running and not self.authentication_failed:
            self._trigger_reconnect()
        return connected

    async def _connect(self) -> bool:
        await self._get_listen_key()
        if not self.listen_key:
            return False
            
        ws_url = f"{self.base_ws_url}/{self.listen_key}"
        if not websockets:
            logger.error("websockets package not installed.")
            return False
            
        try:
            self.ws = await websockets.connect(ws_url)
            self.last_event_at = None
            self.last_transport_heartbeat_at = None
            if not await self._transport_heartbeat():
                await self.ws.close()
                self.ws = None
                return False
            self.is_connected = True
            self.connected_at = datetime.now(timezone.utc)
            logger.info("User stream connected.")
            
            if self.keepalive_task and not self.keepalive_task.done():
                self.keepalive_task.cancel()
            self.keepalive_task = asyncio.create_task(self._keepalive_loop())
            
            if self.listen_task and not self.listen_task.done():
                self.listen_task.cancel()
            self.listen_task = asyncio.create_task(self._listen_loop())
            return True
        except Exception as e:
            logger.error("User stream connection failed: %s", e)
            self.is_connected = False
            return False

    async def _transport_heartbeat(self) -> bool:
        if self.ws is None:
            return False
        try:
            # websockets.ping() returns an awaitable pong waiter. Await both
            # the ping send and the actual pong; sending a ping alone is not
            # evidence that the private stream is still usable.
            deadline = (
                asyncio.get_running_loop().time()
                + self.STREAM_HEARTBEAT_TIMEOUT_SEC
            )
            pong_waiter = await asyncio.wait_for(
                self.ws.ping(), timeout=self.STREAM_HEARTBEAT_TIMEOUT_SEC
            )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("private stream pong deadline exceeded")
            await asyncio.wait_for(pong_waiter, timeout=remaining)
        except Exception as exc:
            logger.warning("Private stream ping/pong failed: %s", exc)
            return False
        self.last_transport_heartbeat_at = datetime.now(timezone.utc)
        return True

    @property
    def _listen_key_path(self) -> str:
        return (
            "/papi/v1/listenKey"
            if getattr(self.rest_client, "portfolio_margin", False)
            else "/fapi/v1/listenKey"
        )

    async def _get_listen_key(self):
        try:
            data = await self.rest_client.request("POST", self._listen_key_path)
            if not isinstance(data, dict) or not data.get("listenKey"):
                raise ValueError("Binance listenKey response is invalid")
            self.listen_key = data.get("listenKey")
            logger.info("Acquired new listenKey.")
        except BinanceAuthenticationError as exc:
            self.listen_key = None
            self.authentication_failed = True
            logger.error(
                "%s authentication failed while starting user stream: %s",
                self.env.value,
                exc,
            )
            self._notify_authentication_failure()
        except Exception as e:
            logger.error("Failed to acquire listenKey: %s", e)
            self.listen_key = None

    def _notify_authentication_failure(self) -> None:
        if not self.on_authentication_failed:
            return
        try:
            result = self.on_authentication_failed()
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception as exc:
            logger.error("Error in user-stream authentication failure handler: %s", exc)

    async def _keepalive_loop(self):
        keepalive_elapsed = 0.0
        while self.is_connected and self.running:
            try:
                await asyncio.sleep(self.STREAM_HEARTBEAT_INTERVAL_SEC)
                if not await self._transport_heartbeat():
                    logger.error("Private stream heartbeat failed.")
                    self.is_connected = False
                    if self.ws:
                        await self.ws.close()
                    self._trigger_reconnect()
                    break

                keepalive_elapsed += self.STREAM_HEARTBEAT_INTERVAL_SEC
                if keepalive_elapsed < 1800:
                    continue
                keepalive_elapsed = 0.0
                if await self.keepalive():
                    logger.info("listenKey keepalive successful.")
                    continue
                logger.error("listenKey keepalive failed.")
                self.is_connected = False
                if self.ws:
                    await self.ws.close()
                self._trigger_reconnect()
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Unexpected error in private stream keepalive loop: %s", exc)
                self.is_connected = False
                if self.ws:
                    await self.ws.close()
                self._trigger_reconnect()
                break

    async def keepalive(self) -> bool:
        """Refresh the active environment listen key and record verification time."""
        if not self.listen_key:
            return False
        try:
            await self.rest_client.request(
                "PUT", self._listen_key_path, params={"listenKey": self.listen_key}
            )
            self.last_keepalive_at = datetime.now(timezone.utc)
            return True
        except Exception as exc:
            if isinstance(exc, BinanceAuthenticationError):
                self._notify_authentication_failure()
            logger.error("listenKey keepalive failed: %s", exc)
            return False

    async def _listen_loop(self):
        try:
            async for message in self.ws:
                try:
                    event = json.loads(message)
                    self.last_event_at = datetime.now(timezone.utc)
                    if self.on_event:
                        await self.on_event(event)
                except Exception as parse_err:
                    logger.warning("Error handling WS message: %s", parse_err)
        except Exception as e:
            logger.warning("User stream disconnected: %s", e)
        finally:
            self.is_connected = False
            if self.running:
                logger.error(
                    "monitor_event=private_stream_disconnected environment=%s",
                    self.env.value,
                )
                logger.warning("User stream closed unexpectedly. Triggering bounded reconnect.")
                if self.on_disconnect:
                    try:
                        res = self.on_disconnect()
                        if asyncio.iscoroutine(res):
                            asyncio.create_task(res)
                    except Exception as disc_err:
                        logger.error("Error in on_disconnect handler: %s", disc_err)
                self._trigger_reconnect()

    def _trigger_reconnect(self):
        if not self.running:
            return
        if self.reconnect_task and not self.reconnect_task.done():
            logger.info("Reconnect task already running. Skipping duplicate spawn.")
            return
        self.reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        attempt = 0
        while self.running and not self.is_connected:
            delay = self.BACKOFF_STEPS[min(attempt, len(self.BACKOFF_STEPS) - 1)]
            jitter = random.uniform(0.1, 0.5)
            total_delay = delay + jitter
            logger.info("Attempting user stream reconnect in %.2fs (attempt %d)", total_delay, attempt + 1)
            await asyncio.sleep(total_delay)
            attempt += 1
            
            connected = await self._connect()
            if connected:
                logger.info("User stream successfully reconnected.")
                if self.on_reconnected:
                    try:
                        res = self.on_reconnected()
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as reconn_err:
                        logger.error("Error in on_reconnected handler: %s", reconn_err)
                break

    async def close(self):
        self.running = False
        self.is_connected = False
        if self.reconnect_task and not self.reconnect_task.done():
            self.reconnect_task.cancel()
            try:
                await self.reconnect_task
            except asyncio.CancelledError:
                pass
        if self.keepalive_task and not self.keepalive_task.done():
            self.keepalive_task.cancel()
            try:
                await self.keepalive_task
            except asyncio.CancelledError:
                pass
        if self.listen_task and not self.listen_task.done():
            self.listen_task.cancel()
            try:
                await self.listen_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
        try:
            if self.listen_key:
                await self.rest_client.request(
                    "DELETE", self._listen_key_path, params={"listenKey": self.listen_key}
                )
        except Exception:
            pass
        self.listen_key = None
