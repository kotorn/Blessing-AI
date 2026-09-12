import asyncio
import logging
import json
import random
try:
    import websockets
except ImportError:
    websockets = None
from .rest_client import BinanceRestClient
from .config import BinanceEnvironment, get_ws_url

logger = logging.getLogger("blessing.binance.user_stream")

class BinanceUserStream:
    BACKOFF_STEPS = [1.0, 2.0, 4.0, 8.0, 15.0, 30.0]

    def __init__(self, rest_client: BinanceRestClient, env: BinanceEnvironment, on_disconnect=None, on_reconnected=None):
        self.rest_client = rest_client
        self.env = env
        self.base_ws_url = get_ws_url(env)
        self.listen_key = None
        self.ws = None
        self.keepalive_task: asyncio.Task | None = None
        self.listen_task: asyncio.Task | None = None
        self.reconnect_task: asyncio.Task | None = None
        self.is_connected = False
        self.on_event = None
        self.on_disconnect = on_disconnect
        self.on_reconnected = on_reconnected
        self.running = False

    async def start(self, event_callback) -> bool:
        self.on_event = event_callback
        self.running = True
        return await self._connect()

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
            self.is_connected = True
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

    async def _get_listen_key(self):
        try:
            data = await self.rest_client.request("POST", "/fapi/v1/listenKey")
            self.listen_key = data.get("listenKey")
            logger.info("Acquired new listenKey.")
        except Exception as e:
            logger.error("Failed to acquire listenKey: %s", e)
            self.listen_key = None

    async def _keepalive_loop(self):
        while self.is_connected and self.running:
            await asyncio.sleep(1800) # 30 mins
            try:
                await self.rest_client.request("PUT", "/fapi/v1/listenKey")
                logger.info("listenKey keepalive successful.")
            except Exception as e:
                logger.error("listenKey keepalive failed: %s", e)
                self.is_connected = False
                if self.ws:
                    await self.ws.close()
                self._trigger_reconnect()
                break

    async def _listen_loop(self):
        try:
            async for message in self.ws:
                try:
                    event = json.loads(message)
                    if self.on_event:
                        await self.on_event(event)
                except Exception as parse_err:
                    logger.warning("Error handling WS message: %s", parse_err)
        except Exception as e:
            logger.warning("User stream disconnected: %s", e)
        finally:
            self.is_connected = False
            if self.running:
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
            await self.rest_client.request("DELETE", "/fapi/v1/listenKey")
        except Exception:
            pass
