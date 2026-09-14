# Binance CLI research integration

Blessing AI uses [Binance's official `binance-cli` repository](https://github.com/binance/binance-cli)
as an optional, independently installed cross-check tool. It is not part of
the Python or npm dependency graph and it is not part of the Worker execution
path.

## Safety boundary

The Python Trading Worker remains the sole execution authority. The local
bridge in `apps/trading_worker/research/binance_cli.py`:

- accepts only an explicit allowlist of USDⓈ-M read-only checks;
- refuses `prod`, `demo`, Mainnet base URLs, profiles, custom requests, and
  all order/margin/leverage/position mutations;
- requires `BINANCE_API_ENV=testnet` and pins the USDⓈ-M route to
  `https://testnet.binancefuture.com`;
- requires an explicit absolute `BINANCE_CLI_PATH` whose filename is
  `binance-cli`/`binance-cli.exe` before any signed check can receive keys;
- launches the child with isolated home/config directories so an existing
  local profile cannot silently replace the explicit Testnet settings;
- passes credentials through child-process environment variables only, never
  command arguments, output records, or logs; and
- labels every result `verified=false` and
  `RESEARCH_OR_CONTRACT_CROSS_CHECK_ONLY`.

The official CLI supports many more commands, including mutable futures
commands. Their existence is why the repository wrapper uses a positive
allowlist instead of forwarding arbitrary CLI arguments. Do not use the
official CLI's profile creation/selection commands for Worker credentials.

## Allowed checks

Public checks do not require credentials:

```text
server_time
exchange_info
book_ticker
mark_price
```

Signed read-only checks require an explicit Testnet key pair:

```text
account
balance
account_configuration
position_mode
positions
open_orders
user_trades
all_orders
query_order
```

The command names are based on the official USDⓈ-M examples, for example
`futures-usds exchange-information`, `futures-usds position-information-v3`,
and `futures-usds symbol-order-book-ticker`.

## Local use

The binary is optional and is never committed to this repository. On Linux,
the pinned installer can place it in an external directory:

```sh
./binance-cli-installer.sh --install-dir "$HOME/.local/bin"
```

The installer verifies the pinned archive checksum and installs outside the
checkout. If the binary is not installed, the wrapper returns `NOT_RUN`; it
does not install software or invent contract evidence.

PowerShell example for a public research cross-check:

```powershell
$env:BINANCE_API_ENV = "testnet"
$env:BINANCE_FUTURES_USDS_BASE_PATH = "https://testnet.binancefuture.com"
$env:BINANCE_CLI_PATH = (Get-Command binance-cli).Source
binance-cli --version
python -m apps.trading_worker.research.binance_cli --check exchange_info
python -m apps.trading_worker.research.binance_cli --check book_ticker --symbol BTCUSDT
python -m apps.trading_worker.research.binance_cli --check mark_price --symbol BTCUSDT
```

For a signed read-only check, provide a dedicated Testnet credential pair in
the official CLI names (`BINANCE_API_KEY` and `BINANCE_SECRET_KEY`) or the
Worker's existing Testnet names (`BINANCE_TESTNET_API_KEY` and
`BINANCE_TESTNET_API_SECRET`). Presence is enough for local gating; values
must never be printed or committed.

Signed checks additionally require `BINANCE_CLI_PATH` to be an absolute path
to the official executable. This prevents an implicit PATH binary from ever
receiving credentials. Verify the installed version and provenance separately;
the wrapper still labels its response `verified=false`.

```powershell
python -m apps.trading_worker.research.binance_cli --check account
python -m apps.trading_worker.research.binance_cli --check positions --symbol BTCUSDT
python -m apps.trading_worker.research.binance_cli --check open_orders --symbol BTCUSDT
```

A successful CLI response is only a sanitized research/contract observation.
It does not make `testnetExecutionReady`, `IN_SYNC`, or any launch evidence
true. Private user-stream health, bootstrap verification, reconciliation,
risk gates, kill switch behavior, and all mutations remain owned and tested by
the Worker architecture.

## Research provenance

When CLI output is converted into research data, retain the underlying
Binance source classification (for example
`BINANCE_PUBLIC_TESTNET_READ_ONLY`) and record `binance-cli` as the collection
tool. The CLI is a transport/cross-check detail, not a new alpha or a second
execution authority. Net results must still include explicit fees, funding,
spread, slippage, and execution-cost assumptions and remain research-only
until the complete evidence ladder is satisfied.
