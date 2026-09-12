import hashlib
import hmac
import aiohttp
import logging
from typing import Dict, Any, Optional
from urllib.parse import urlencode

from .config import BinanceEnvironment, get_rest_url
from .clock import BinanceClock

logger = logging.getLogger("blessing.binance.rest_client")

class BinanceRestClient:
    def __init__(self, api_key: str, api_secret: str, env: BinanceEnvironment):
        self.api_key = api_key
        self.api_secret = api_secret
        self.env = env
        self.base_url = get_rest_url(env)
        self.clock = BinanceClock(self.base_url)
        self.session: Optional[aiohttp.ClientSession] = None

    async def init_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(headers={"X-MBX-APIKEY": self.api_key})
        await self.clock.synchronize(self.session)

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

    async def request(self, method: str, path: str, signed: bool = False, **kwargs) -> Any:
        if not self.session:
            await self.init_session()
            
        params = kwargs.get('params', {})
        
        if signed:
            params['timestamp'] = self.clock.get_signed_timestamp()
            params['recvWindow'] = self.clock.recv_window
            params['signature'] = self._sign_payload(params)
            
        kwargs['params'] = params
        
        url = f"{self.base_url}{path}"
        async with self.session.request(method, url, **kwargs) as resp:
            data = await resp.json()
            if resp.status >= 400:
                logger.error("Binance REST Error: %s %s - %s", resp.status, path, data)
                # Catch timestamp errors to resync clock
                if data.get("code") == -1021:
                    logger.warning("Timestamp error. Resyncing clock.")
                    await self.clock.synchronize(self.session)
                raise ValueError(f"Binance API Error {resp.status}: {data}")
            return data
