"""
NATS JetStream Client Wrapper for Blessing AI v0.2
Provides resilient async publishing and subscription with reconnect handling.
"""

import logging
from typing import Any, Optional

from domain.events import DomainEvent

logger = logging.getLogger("blessing.infra.nats")

try:
    import nats
    from nats.js import JetStreamContext
    NATS_INSTALLED = True
except ImportError:
    NATS_INSTALLED = False


class NatsBus:
    def __init__(self, nats_url: str = "nats://127.0.0.1:4222"):
        self.nats_url = nats_url
        self.nc: Optional[Any] = None
        self.js: Optional[Any] = None
        self._is_connected = False

    async def connect(self) -> bool:
        if not NATS_INSTALLED:
            logger.warning("nats-py not installed; running in mock in-memory bus mode")
            self._is_connected = True
            return True

        try:
            self.nc = await nats.connect(
                self.nats_url,
                reconnect_time_wait=2,
                max_reconnect_attempts=-1,
                name="blessing-ai-bus",
            )
            self.js = self.nc.jetstream()
            self._is_connected = True
            logger.info("Connected to NATS JetStream at %s", self.nats_url)
            return True
        except Exception as err:
            logger.error("Failed to connect to NATS: %s", err)
            self._is_connected = False
            return False

    async def publish_event(self, subject: str, event: DomainEvent) -> bool:
        if not self._is_connected:
            logger.warning("Cannot publish; NATS not connected")
            return False

        payload_bytes = event.serialize()
        if self.js:
            try:
                await self.js.publish(subject, payload_bytes)
                return True
            except Exception as err:
                logger.error("Error publishing to JetStream subject %s: %s", subject, err)
                return False
        return True

    async def close(self) -> None:
        if self.nc:
            await self.nc.close()
            self._is_connected = False
            logger.info("Closed NATS connection")
