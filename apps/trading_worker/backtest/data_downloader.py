"""Public, read-only Binance kline downloader for research datasets.

This module deliberately has no API-key support and no mutable exchange
operation. Its output is research evidence only; it is never an execution
readiness signal.
"""

import asyncio
import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Any, List, Optional

import aiohttp

try:
    import polars as pl
except ImportError:  # pragma: no cover - exercised in minimal environments
    pl = None


logger = logging.getLogger("blessing.backtest.downloader")

_INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}

_PUBLIC_REST_HOSTS = {
    "https://fapi.binance.com": "BINANCE_PUBLIC_MAINNET_READ_ONLY",
    "https://testnet.binancefuture.com": "BINANCE_PUBLIC_TESTNET_READ_ONLY",
}


def _finite_positive(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Kline field {field} is not numeric") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"Kline field {field} is not finite and positive")
    return parsed


def _finite_nonnegative(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Kline field {field} is not numeric") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"Kline field {field} is not finite and non-negative")
    return parsed


def _parse_kline_row(
    row: Any,
    *,
    symbol: str,
    interval: str,
    end_time_ms: int,
) -> Optional[dict[str, Any]]:
    """Validate one Binance kline array and drop an unfinished candle."""
    if not isinstance(row, (list, tuple)) or len(row) < 10:
        raise ValueError("Binance kline row is malformed")
    try:
        open_time_ms = int(row[0])
        close_time_ms = int(row[6])
        trade_count = int(row[8])
    except (TypeError, ValueError) as exc:
        raise ValueError("Binance kline timestamps/trade count are invalid") from exc
    if open_time_ms < 0 or close_time_ms < open_time_ms or trade_count < 0:
        raise ValueError("Binance kline timestamps/trade count are unusable")
    if close_time_ms >= end_time_ms:
        return None

    open_price = _finite_positive(row[1], "open")
    high_price = _finite_positive(row[2], "high")
    low_price = _finite_positive(row[3], "low")
    close_price = _finite_positive(row[4], "close")
    volume = _finite_nonnegative(row[5], "volume")
    if high_price < max(open_price, close_price) or low_price > min(open_price, close_price):
        raise ValueError("Binance kline OHLC relationship is invalid")

    return {
        "timestamp": datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": volume,
        "trade_count": trade_count,
        "symbol": symbol,
        "interval": interval,
        "data_source": "",
    }


class BinanceDataDownloader:
    """Download verified public Binance futures klines to compressed Parquet."""

    def __init__(
        self,
        data_dir: str = "./data/historical",
        *,
        base_url: Optional[str] = None,
        request_delay_sec: float = 0.2,
        session_factory: Any = aiohttp.ClientSession,
    ):
        self.data_dir = data_dir
        configured_url = base_url or os.getenv(
            "BINANCE_PUBLIC_RESEARCH_REST_URL", "https://fapi.binance.com"
        )
        self.base_url = configured_url.rstrip("/")
        if self.base_url not in _PUBLIC_REST_HOSTS:
            raise ValueError(
                "Research downloader only permits Binance public Mainnet or Testnet REST hosts"
            )
        try:
            delay = float(request_delay_sec)
        except (TypeError, ValueError):
            delay = 0.2
        self.request_delay_sec = delay if math.isfinite(delay) and delay >= 0 else 0.2
        self.session_factory = session_factory
        os.makedirs(self.data_dir, exist_ok=True)

    @property
    def data_source(self) -> str:
        return _PUBLIC_REST_HOSTS[self.base_url]

    @staticmethod
    def _validate_request(symbol: str, interval: str, days: int) -> tuple[str, str, int]:
        normalized_symbol = str(symbol).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,30}", normalized_symbol):
            raise ValueError("symbol must be an uppercase Binance symbol")
        normalized_interval = str(interval).strip()
        if normalized_interval not in _INTERVAL_MILLISECONDS:
            raise ValueError(f"unsupported Binance interval: {normalized_interval}")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValueError("days must be an integer between 1 and 365")
        return normalized_symbol, normalized_interval, days

    async def _fetch_page(self, session: Any, params: dict[str, Any]) -> Any:
        url = f"{self.base_url}/fapi/v1/klines"
        async with session.get(url, params=params) as response:
            response.raise_for_status()
            payload = await response.json()
        if not isinstance(payload, list):
            raise ValueError("Binance kline response is not a list")
        return payload

    async def download_klines_to_parquet(
        self,
        symbol: str,
        interval: str = "1m",
        days: int = 30,
        *,
        end_time_ms: Optional[int] = None,
    ) -> str:
        """Download closed candles and return the verified Parquet path."""
        if pl is None:
            raise RuntimeError("Polars is required to write verified Parquet research data")
        symbol, interval, days = self._validate_request(symbol, interval, days)
        now_ms = int(
            datetime.now(timezone.utc).timestamp() * 1000
            if end_time_ms is None
            else end_time_ms
        )
        if now_ms <= 0:
            raise ValueError("end_time_ms must be positive")
        start_ms = now_ms - days * 86_400_000
        step_ms = _INTERVAL_MILLISECONDS[interval]
        rows: List[dict[str, Any]] = []
        cursor = start_ms

        logger.info(
            "Downloading public read-only Binance klines for %s (%s) over %d days",
            symbol,
            interval,
            days,
        )
        async with self.session_factory() as session:
            while cursor < now_ms:
                payload = await self._fetch_page(
                    session,
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "startTime": cursor,
                        "endTime": now_ms,
                        "limit": 1500,
                    },
                )
                if not payload:
                    break
                page_rows = [
                    parsed
                    for raw_row in payload
                    for parsed in [
                        _parse_kline_row(
                            raw_row,
                            symbol=symbol,
                            interval=interval,
                            end_time_ms=now_ms,
                        )
                    ]
                    if parsed is not None
                ]
                rows.extend(page_rows)
                try:
                    last_open_ms = int(payload[-1][0])
                except (IndexError, TypeError, ValueError) as exc:
                    raise ValueError("Binance kline pagination cursor is invalid") from exc
                next_cursor = last_open_ms + step_ms
                if next_cursor <= cursor:
                    raise ValueError("Binance kline pagination did not advance")
                cursor = next_cursor
                if len(payload) < 1500:
                    break
                if self.request_delay_sec:
                    await asyncio.sleep(self.request_delay_sec)

        if not rows:
            raise ValueError("Binance returned no closed, valid kline rows")
        rows.sort(key=lambda row: row["timestamp"])
        deduplicated: list[dict[str, Any]] = []
        seen_timestamps: set[datetime] = set()
        for row in rows:
            timestamp = row["timestamp"]
            if timestamp in seen_timestamps:
                continue
            seen_timestamps.add(timestamp)
            row["data_source"] = self.data_source
            deduplicated.append(row)
        if not deduplicated:
            raise ValueError("No unique closed kline rows were downloaded")

        frame = pl.DataFrame(deduplicated)
        output_path = os.path.join(
            self.data_dir, f"{symbol}_{interval}_historical.parquet"
        )
        frame.write_parquet(output_path, compression="zstd")
        logger.info("Saved %d verified research rows to %s", frame.height, output_path)
        return output_path


if __name__ == "__main__":  # pragma: no cover - manual research utility
    logging.basicConfig(level=logging.INFO)
    downloader = BinanceDataDownloader()
    asyncio.run(downloader.download_klines_to_parquet("BTCUSDT", days=7))
