# Binance USDⓈ-M Futures Contract Audit

## Official Documentation
Source: https://developers.binance.com/docs/derivatives/usds-margined-futures
Checked Date: 2026-09-11

## Endpoint Verification

### 1. User Data Stream
* **Purpose**: Receive account updates, position updates, and execution reports (fills, cancels).
* **Endpoints**: 
  - `POST /fapi/v1/listenKey` (Start user stream)
  - `PUT /fapi/v1/listenKey` (Keepalive, send every 60m, recommended every 30m)
  - `DELETE /fapi/v1/listenKey` (Close stream)
* **Auth**: Valid API Key (HMAC not required for listenKey creation, but required for trade endpoints).
* **Testnet Equivalent**: Same endpoints on `https://testnet.binancefuture.com`
* **Implementation Note**: Python worker must ping every 30m. Disconnect requires full reconciliation.

### 2. Order Placement
* **Purpose**: Create limits/markets.
* **Endpoint**: `POST /fapi/v1/order`
* **Testnet Equivalent**: Yes.
* **Auth**: HMAC SHA256 signature required.
* **Hedge Mode Semantics**: Requires `positionSide` (LONG, SHORT) if Hedge Mode enabled. `reduceOnly` can be set to true for closing positions.

### 3. Cancel/Replace
* **Purpose**: Modify an order.
* **Endpoint**: `POST /fapi/v1/order/cancelReplace`
* **Status**: Supported for USDM Futures. However, the system currently assumes atomic cancel/replace.
* **Implementation Note**: Explicit fallback or test coverage for failure modes during replacement is required.

### 4. Exchange Information
* **Purpose**: Retrieve step sizes, tick sizes, minimum notionals.
* **Endpoint**: `GET /fapi/v1/exchangeInfo`
* **Testnet Equivalent**: Yes.
* **Implementation Note**: The Python execution adapter must pull these limits before executing `round(qty, 3)` to ensure valid precision per instrument.
