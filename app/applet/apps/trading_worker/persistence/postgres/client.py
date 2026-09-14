import asyncio
import logging
from typing import Optional, Dict, Any, List
import asyncpg
from decimal import Decimal
import os

logger = logging.getLogger(__name__)

class PostgresClient:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10):
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        if not self.pool:
            logger.info("Connecting to PostgreSQL at %s", self.dsn.split("@")[-1])
            try:
                self.pool = await asyncpg.create_pool(
                    dsn=self.dsn,
                    min_size=self.min_size,
                    max_size=self.max_size,
                )
                logger.info("PostgreSQL connection pool established.")
            except Exception as e:
                logger.error("Failed to connect to PostgreSQL: %s", e)
                raise

    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            logger.info("PostgreSQL connection pool closed.")

    async def execute(self, query: str, *args) -> str:
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> List[asyncpg.Record]:
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

def get_postgres_client() -> PostgresClient:
    user = os.getenv("POSTGRES_USER", "blessing_user")
    password = os.getenv("POSTGRES_PASSWORD", "blessing_secret_2026")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "blessing_ai")
    
    dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"
    return PostgresClient(dsn=dsn)
