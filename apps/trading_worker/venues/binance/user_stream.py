import asyncio
import logging
import json
try:
    import websockets
except ImportError:
    websockets = None
from .rest_client import BinanceRestClient
from .config import BinanceEnvironment, get_ws_url

logger = logging.getLogger("blessing.binance.user_stream")

class BinanceUserStream:
    def __init__(self, rest_client: BinanceRestClient, env: BinanceEnvironment, on_disconnect=None):
        self.rest_client = rest_client
        self.env = env
        self.base_ws_url = get_ws_url(env)
        self.listen_key = None
        self.ws = None
        self.keepalive_task = None
        self.listen_task = None
        self.is_connected = False
        self.on_event = None
        self.on_disconnect = on_disconnect
        self.running = False

    async def start(self, event_callback):
        self.on_event = event_callback
        self.running = True
        return await self._connect()

    async def _connect(self):
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
            
            if self.keepalive_task:
                self.keepalive_task.cancel()
            self.keepalive_task = asyncio.create_task(self._keepalive_loop())
            
            if self.listen_task:
                self.listen_task.cancel()
            self.listen_task = asyncio.create_task(self._listen_loop())
            return True
        except Exception as e:
            logger.error(f"User stream connection failed: {e}")
            return False

    async def _get_listen_key(self):
        try:
            data = await self.rest_client.request("POST", "/fapi/v1/listenKey")
            self.listen_key = data["listenKey"]
            logger.info("Acquired new listenKey.")
        except Exception as e:
            logger.error("Failed to acquire listenKey: %s", e)

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

    async def _listen_loop(self):
        try:
            async for message in self.ws:
                event = json.loads(message)
                if self.on_event:
                    await self.on_event(event)
        except Exception as e:
            logger.warning("User stream disconnected: %s", e)
        finally:
            self.is_connected = False
            if self.running:
                logger.warning("User stream closed unexpectedly. Reconnection and reconciliation required.")
                if self.on_disconnect:
                    asyncio.create_task(self.on_disconnect())

    async def close(self):
        self.running = False
        self.is_connected = False
        if self.keepalive_task:
            self.keepalive_task.cancel()
        if self.listen_task:
            self.listen_task.cancel()
        if self.ws:
            await self.ws.close()
        try:
            await self.rest_client.request("DELETE", "/fapi/v1/listenKey")
        except Exception:
            pass