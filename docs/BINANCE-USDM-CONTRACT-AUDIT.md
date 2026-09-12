# Binance USDⓈ-M Futures Contract Audit

## Official Documentation
Source: https://developers.binance.com/docs/derivatives/usds-margined-futures
Checked Date: 2026-09-11

## Endpoint Verification

### Base URLs
* **Mainnet REST**: `https://fapi.binance.com`
* **Testnet REST**: `https://testnet.binancefuture.com`
* **Mainnet WS**: `wss://fstream.binance.com/ws` or `wss://fstream.binance.com/stream`
* **Testnet WS**: `wss://stream.binancefuture.com/ws` or `wss://stream.binancefuture.com/stream`

### 1. Server Time
* **Purpose**: Fetch server time for clock synchronization.
* **Endpoint**: `GET /fapi/v1/time`
* **Implementation**: `apps/trading_worker/venues/binance/clock.py`

### 2. Exchange Information
* **Purpose**: Retrieve step sizes, tick sizes, minimum notionals.
* **Endpoint**: `GET /fapi/v1/exchangeInfo`
* **Implementation**: `apps/trading_worker/venues/binance/symbol_rules.py`

### 3. Position Mode & Account
* **Purpose**: Check if account is in Hedge Mode or One-Way.
* **Endpoint**: `GET /fapi/v1/positionSide/dual` and `GET /fapi/v2/account`
* **Implementation**: `apps/trading_worker/venues/binance/capabilities.py`

### 4. User Data Stream
* **Purpose**: Receive account updates, position updates, and execution reports (fills, cancels).
* **Endpoints**: 
  - `POST /fapi/v1/listenKey` (Start user stream)
  - `PUT /fapi/v1/listenKey` (Keepalive, send every 30m)
  - `DELETE /fapi/v1/listenKey` (Close stream)
* **Auth**: Valid API Key via X-MBX-APIKEY.
* **Implementation**: `apps/trading_worker/venues/binance/user_stream.py`

### 5. Order Placement
* **Purpose**: Create limits/markets.
* **Endpoint**: `POST /fapi/v1/order`
* **Auth**: HMAC SHA256 signature required.
* **Hedge Mode Semantics**: Requires `positionSide` (LONG, SHORT) if Hedge Mode enabled. `reduceOnly` can be set to true for closing positions in ONE_WAY.
* **Implementation**: `apps/trading_worker/venues/binance/rest_client.py`

### 6. Cancel/Replace (Modification)
* **Purpose**: Modify an order.
* **Endpoint**: `PUT /fapi/v1/order` (Binance officially supports PUT for modification).
* **Implementation Note**: Modifying an order returns the new order details. We must map deterministic IDs cleanly.

### 7. Reconciliation
* **Purpose**: Open orders and positions recovery.
* **Endpoint**: `GET /fapi/v1/openOrders` and `GET /fapi/v2/positionRisk`
* **Implementation**: `apps/trading_worker/venues/binance/reconciliation.py`
