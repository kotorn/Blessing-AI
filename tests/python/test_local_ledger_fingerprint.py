"""In-memory ledger rows and a PostgreSQL reload (NUMERIC(28,10) -> Decimal with
trailing zeros) must fingerprint identically, or the durable-ledger match that
guards every Local Mainnet risk context can never succeed."""

from decimal import Decimal
from types import SimpleNamespace

from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter as A


def _order(qty, price):
    return SimpleNamespace(
        client_order_id="c1", basket_id="pilot-1", exchange_order_id=None, symbol="ETHUSDC",
        side="BUY", order_type="MARKET", quantity=qty, price=price, status="PENDING",
        position_side="BOTH",
    )


def test_order_fingerprint_ignores_numeric_scale():
    assert A._local_order_fingerprint(_order(Decimal("0.019"), Decimal("2610.59"))) == \
        A._local_order_fingerprint(_order(Decimal("0.0190000000"), Decimal("2610.5900000000")))


def test_order_fingerprint_still_detects_real_differences():
    assert A._local_order_fingerprint(_order(Decimal("0.019"), Decimal("2610.59"))) != \
        A._local_order_fingerprint(_order(Decimal("0.020"), Decimal("2610.59")))


def test_fill_and_position_fingerprints_ignore_numeric_scale():
    fill = lambda q, p, c: SimpleNamespace(  # noqa: E731
        exchange_trade_id="t", exchange_order_id="o", client_order_id="c", symbol="ETHUSDC",
        side="BUY", position_side="BOTH", quantity=q, price=p, commission=c, commission_asset="USDC")
    assert A._local_fill_fingerprint(fill(Decimal("0.019"), Decimal("2610.5"), Decimal("0.02"))) == \
        A._local_fill_fingerprint(fill(Decimal("0.0190000000"), Decimal("2610.5000000000"), Decimal("0.0200000000")))
    pos = lambda q, e, l: SimpleNamespace(  # noqa: E731
        symbol="ETHUSDC", position_side="BOTH", quantity=q, entry_price=e, mark_price=e,
        liquidation_price=Decimal("0"), unrealized_pnl=Decimal("0"), leverage=l, margin_type="CROSS")
    assert A._local_position_fingerprint(pos(Decimal("0"), Decimal("0"), Decimal("5"))) == \
        A._local_position_fingerprint(pos(Decimal("0E-10"), Decimal("0E-10"), Decimal("5.0000000000")))


def test_missing_values_stay_distinguishable_from_zero():
    assert A._local_order_fingerprint(_order(None, None)) != \
        A._local_order_fingerprint(_order(Decimal("0"), Decimal("0")))
