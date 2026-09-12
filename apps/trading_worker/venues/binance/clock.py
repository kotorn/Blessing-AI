import time
import logging
import asyncio
import aiohttp
from typing import Optional

logger = logging.getLogger("blessing.binance.clock")

class BinanceClock:
    def __init__(self, rest_url: str):
        self.rest_url = rest_url
        self.server_time_offset_ms: int = 0
        self.recv_window = 5000

    async def synchronize(self, session: aiohttp.ClientSession) -> bool:
        try:
            local_before = int(time.time() * 1000)
            async with session.get(f"{self.rest_url}/fapi/v1/time") as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"Binance time endpoint returned HTTP {resp.status}")
                data = await resp.json()
                server_time = int(data["serverTime"])
            local_after = int(time.time() * 1000)
            rtt = local_after - local_before
            
            self.server_time_offset_ms = server_time - (local_before + rtt // 2)
            logger.info("Binance clock synchronized. Offset: %d ms, RTT: %d ms", self.server_time_offset_ms, rtt)
            return True
        except Exception as e:
            logger.error("Failed to synchronize Binance clock: %s", e)
            return False

    def get_signed_timestamp(self) -> int:
        return int(time.time() * 1000) + self.server_time_offset_ms
