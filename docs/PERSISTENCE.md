# Worker persistence boundary

The Python Trading Worker uses a bounded in-memory queue followed by a
PostgreSQL transactional outbox. An event is only described as durable after
the outbox insert succeeds. The dispatcher replays pending events into the
orders, fills, positions, and portfolio snapshot tables using idempotent
upserts.

## Modes

`PERSISTENCE_MODE` accepts:

- `DISABLED`: no database connection is attempted; writes are reported as
  disabled and are not durable.
- `OPTIONAL`: Paper and Testnet may continue when PostgreSQL is unavailable,
  but readiness is `DEGRADED` and the worker must not claim durable history.
- `REQUIRED`: startup/readiness fails closed when PostgreSQL or the outbox is
  unavailable. This is the required mode for any future Small Live mode.

Configure the database with `DATABASE_URL`, or with all `POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`
environment variables. There are no credential defaults. Logs expose only the
database host and port.

Instrument metadata is accepted only from exchange `exchangeInfo` discovery.
The persistence layer does not invent tick size, step size, minimum notional,
or leverage rules. Futures positions are keyed by `(venue, symbol,
position_side)` so `LONG`, `SHORT`, and `BOTH` remain separate in Hedge Mode.
