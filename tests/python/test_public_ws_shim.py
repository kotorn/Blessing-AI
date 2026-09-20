"""Compatibility tests for the Binance public websocket relocation.

The canonical implementation lives at
``apps/trading_worker/venues/binance/public_ws.py``.  The legacy path
``venues/binance/public_ws.py`` must remain an importable shim that
re-exports the exact same class, so existing importers keep working.
"""

from apps.trading_worker.venues.binance import public_ws as canonical_module
from venues.binance import public_ws as legacy_module


def test_legacy_path_imports() -> None:
    from venues.binance.public_ws import BinancePublicWebSocket

    assert BinancePublicWebSocket is not None


def test_canonical_path_imports() -> None:
    from apps.trading_worker.venues.binance.public_ws import BinancePublicWebSocket

    assert BinancePublicWebSocket is not None


def test_both_paths_resolve_to_same_class() -> None:
    assert legacy_module.BinancePublicWebSocket is canonical_module.BinancePublicWebSocket


def test_legacy_shim_declares_expected_exports() -> None:
    assert legacy_module.__all__ == ["BinancePublicWebSocket"]


def test_reexported_class_is_functional() -> None:
    client = legacy_module.BinancePublicWebSocket(["BTCUSDT"])

    assert client.symbols == ["btcusdt"]
    assert client.venue == "BINANCE_TESTNET"
    assert client.is_running is False
