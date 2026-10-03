import hashlib
import json

import pytest

from apps.trading_worker.venues.binance.reconciliation import BinanceReconciliation, FillRecoveryError


@pytest.mark.parametrize("kind,id_field,client_field", [
    ("ALL_ORDERS", "orderId", "clientOrderId"),
    ("USER_TRADES", "id", "clientOrderId"),
    ("ALL_ALGO_ORDERS", "algoId", "clientAlgoId"),
])
def test_durable_payload_survives_without_new_exchange_rows(kind, id_field, client_field):
    payload = {"symbol": "ETHUSDC", id_field: 123, client_field: "owned", "status": "FILLED"}
    item = {"item_id": "123", "client_id": "owned", "payload": payload,
            "payload_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    assert BinanceReconciliation._durable_history_payloads([item], kind, "ETHUSDC") == [payload]
    corrupted = dict(item, payload=dict(payload, symbol="BTCUSDT"))
    with pytest.raises(FillRecoveryError):
        BinanceReconciliation._durable_history_payloads([corrupted], kind, "ETHUSDC")
    with pytest.raises(FillRecoveryError):
        BinanceReconciliation._durable_history_payloads([item, item], kind, "ETHUSDC")


def test_durable_history_rejects_missing_payload_instead_of_claiming_ownership():
    with pytest.raises(FillRecoveryError, match="no payload"):
        BinanceReconciliation._durable_history_payloads([{"item_id": "123"}], "ALL_ALGO_ORDERS", "ETHUSDC")
