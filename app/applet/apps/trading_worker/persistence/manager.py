import logging
import asyncio
from typing import Optional
from .postgres.client import PostgresClient, get_postgres_client
from .postgres.repositories import OrderRepository, FillRepository, PositionRepository, RiskSnapshotRepository
from domain.models import ExecutionOrder, ExchangeFill, ExchangePosition, RiskSnapshot

logger = logging.getLogger(__name__)

class PersistenceManager:
    """Coordinates writes to the database without blocking the execution path."""
    def __init__(self, db: Optional[PostgresClient] = None):
        self.db = db or get_postgres_client()
        self.orders: Optional[OrderRepository] = None
        self.fills: Optional[FillRepository] = None
        self.positions: Optional[PositionRepository] = None
        self.risk: Optional[RiskSnapshotRepository] = None
        
        self.is_connected = False
        self._write_queue = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None

    async def start(self):
        try:
            await self.db.connect()
            self.orders = OrderRepository(self.db)
            self.fills = FillRepository(self.db)
            self.positions = PositionRepository(self.db)
            self.risk = RiskSnapshotRepository(self.db)
            self.is_connected = True
            
            self._writer_task = asyncio.create_task(self._background_writer())
            logger.info("Persistence manager started with background writer.")
        except Exception as e:
            logger.error("Failed to start PersistenceManager: %s", e)

    async def stop(self):
        self.is_connected = False
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        await self.db.disconnect()

    async def _background_writer(self):
        while self.is_connected:
            try:
                entity, entity_type = await self._write_queue.get()
                
                if entity_type == "ORDER" and self.orders:
                    await self.orders.save_order(entity)
                elif entity_type == "FILL" and self.fills:
                    await self.fills.save_fill(entity)
                elif entity_type == "POSITION" and self.positions:
                    await self.positions.save_position(entity)
                elif entity_type == "RISK_SNAPSHOT" and self.risk:
                    await self.risk.save_snapshot(entity)
                
                self._write_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in background persistence writer: %s", e)
                # Note: real implementation might need retry logic here

    def enqueue_order(self, order: ExecutionOrder):
        if self.is_connected:
            self._write_queue.put_nowait((order, "ORDER"))

    def enqueue_fill(self, fill: ExchangeFill):
        if self.is_connected:
            self._write_queue.put_nowait((fill, "FILL"))

    def enqueue_position(self, position: ExchangePosition):
        if self.is_connected:
            self._write_queue.put_nowait((position, "POSITION"))

    def enqueue_risk_snapshot(self, snapshot: RiskSnapshot):
        if self.is_connected:
            self._write_queue.put_nowait((snapshot, "RISK_SNAPSHOT"))
