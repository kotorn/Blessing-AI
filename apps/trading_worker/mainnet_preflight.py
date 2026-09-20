"""Read-only Mainnet preflight observation internals.

Extracted verbatim from ``TradingWorkerApp.run_mainnet_read_only_preflight``
so the Worker control API can keep a thin delegator in
``apps.trading_worker.main``.  Behavior is preserved exactly: the check
order, check/log messages, and response shape are unchanged.

Dependency boundary of this module:

* It must never import ``apps.trading_worker.main`` (that would be a cycle),
  must never read the research strategy stack
  (``apps.trading_worker.strategies`` /
  ``apps.trading_worker.engines.portfolio_risk``), and must never reach a
  mutable global of the control API module (``WORKER_ENGINE``, its logger).
* Everything that used to be read from ``self`` is handed in explicitly via
  :class:`MainnetPreflightContext` — the worker instance (live reads for the
  before/after lifecycle signature and the preflight lock), the disposable
  adapter factory (resolved by the wrapper from its own module namespace at
  call time), the lifecycle helper callables, and the persistence object.
* ``BinanceExecutionAdapter`` is deliberately NOT imported here: it arrives
  as ``MainnetPreflightContext.adapter_factory`` so that monkeypatching
  ``apps.trading_worker.main.BinanceExecutionAdapter`` keeps intercepting
  disposable preflight adapter creation.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, List, Optional

from domain.models import utc_now

from apps.trading_worker.venues.binance.config import (
    BinanceEnvironment,
    environment_label,
    is_portfolio_margin_enabled,
)
from apps.trading_worker.venues.binance.models import ConnectionState

logger = logging.getLogger("blessing.worker.mainnet_preflight")


@dataclass(frozen=True)
class MainnetPreflightContext:
    """Explicit dependency bag for the read-only Mainnet preflight.

    ``worker`` is the live :class:`~apps.trading_worker.main.TradingWorkerApp`
    instance (typed loosely to avoid importing the control API module).  The
    preflight reads the following from it, live, exactly as the original
    method read them from ``self``:

    * ``_mainnet_preflight_lock`` — serializes preflight observations;
    * the lifecycle signature attributes read before and after the
      observation: ``execution_mode``, ``engine_state``, ``connection_state``,
      ``market_data_healthy``, ``private_stream_healthy``, ``authenticated``,
      ``reconciliation_status``, ``kill_switch_active``, ``pause_new_risk``,
      ``recovery_only``, ``symbols``, ``execution_adapter``,
      ``active_configuration``;
    * ``persistence`` — only ``.readiness()`` and
      ``.create_execution_ledger`` (when present) are called on it.
    """

    worker: Any
    # Disposable Mainnet adapter class/factory.  Resolved by the thin wrapper
    # in apps.trading_worker.main from its own module global at call time.
    adapter_factory: Callable[..., Any]
    # TradingWorkerApp._mainnet_configured, passed as a callable.
    is_mainnet_configured: Callable[[], bool]
    # TradingWorkerApp._is_mainnet_snapshot_risk_ready, passed as a callable.
    is_snapshot_risk_ready: Callable[..., bool]
    # TradingWorkerApp._env_flag, passed as a callable.
    env_flag: Callable[..., bool]


async def run_mainnet_read_only_preflight(context: MainnetPreflightContext) -> dict:
    """Collect signed Mainnet evidence without changing worker lifecycle.

    This path deliberately creates a disposable adapter that is allowed to
    perform only the read/stream/reconciliation lifecycle.  It never binds
    the adapter to this worker, never acquires an execution lease, never
    calls an order endpoint, and always closes the private stream before
    returning.  A successful observation is evidence for a later release
    gate; it is not an ARM operation.
    """

    worker = context.worker
    adapter_factory = context.adapter_factory
    async with worker._mainnet_preflight_lock:
        observed_at = utc_now()
        checks: List[dict[str, Any]] = []

        def add_check(
            check_id: str,
            name: str,
            passed: bool,
            message: str,
            *,
            required: bool = True,
        ) -> None:
            checks.append(
                {
                    "id": check_id,
                    "name": name,
                    "required": required,
                    "status": "PASS" if passed else "FAIL",
                    "message": message,
                }
            )

        before_signature = (
            worker.execution_mode,
            worker.engine_state,
            worker.connection_state,
            worker.market_data_healthy,
            worker.private_stream_healthy,
            worker.authenticated,
            worker.reconciliation_status,
            worker.kill_switch_active,
            worker.pause_new_risk,
            worker.recovery_only,
            tuple(worker.symbols),
            id(worker.execution_adapter),
            int(getattr(worker.execution_adapter, "order_submission_attempts", 0) or 0),
            repr(worker.active_configuration),
        )
        adapter: Optional[Any] = None
        order_submission_attempts = 0
        order_endpoint_attempts = 0
        credentials_configured = context.is_mainnet_configured()
        add_check(
            "CHK-PREFLIGHT-CREDENTIALS",
            "Mainnet Credentials",
            credentials_configured,
            "Secret-injected Mainnet credential pair is present"
            if credentials_configured
            else "Mainnet credentials are not injected into this revision",
        )

        connected = False
        market_fresh = False
        persistence_ready = False
        persistence_error = False
        durable_ledger = None
        durable_ledger_error = False
        try:
            try:
                persistence = worker.persistence.readiness()
                has_pending = (
                    isinstance(persistence, Mapping)
                    and "pending_outbox" in persistence
                )
                has_failed = (
                    isinstance(persistence, Mapping)
                    and "failed_writes" in persistence
                )
                pending_outbox = persistence.get("pending_outbox") if has_pending else None
                failed_writes = persistence.get("failed_writes") if has_failed else None

                def _is_explicit_zero_counter(val: Any) -> bool:
                    if val is None or isinstance(val, bool):
                        return False
                    if isinstance(val, (int, float, Decimal)):
                        return val == 0
                    return False

                persistence_ready = bool(
                    isinstance(persistence, Mapping)
                    and persistence.get("mode") == "REQUIRED"
                    and persistence.get("durable") is True
                    and has_pending
                    and _is_explicit_zero_counter(pending_outbox)
                    and has_failed
                    and _is_explicit_zero_counter(failed_writes)
                )
            except Exception:
                persistence_error = True
            add_check(
                "CHK-PREFLIGHT-PERSISTENCE",
                "Required SQL Persistence",
                persistence_ready and not persistence_error,
                "Required transactional outbox is durable"
                if persistence_ready and not persistence_error
                else "Required persistence is unavailable; preflight evidence is not durable",
            )
            ledger_factory = getattr(worker.persistence, "create_execution_ledger", None)
            if credentials_configured and callable(ledger_factory):
                try:
                    durable_ledger = await ledger_factory(
                        symbol="ETHUSDC",
                        venue=environment_label(BinanceEnvironment.MAINNET),
                    )
                except Exception as exc:
                    durable_ledger_error = True
                    logger.error(
                        "Mainnet durable ledger snapshot failed: %s",
                        type(exc).__name__,
                    )
                add_check(
                    "CHK-PREFLIGHT-DURABLE-LEDGER",
                    "Durable Mainnet Ledger Snapshot",
                    durable_ledger is not None and not durable_ledger_error,
                    "Cloud SQL Mainnet ledger scope was loaded before reconciliation"
                    if durable_ledger is not None and not durable_ledger_error
                    else "Cloud SQL Mainnet ledger scope could not be loaded safely",
                )
            add_check(
                "CHK-PREFLIGHT-KILL-SWITCH",
                "Kill Switch",
                not worker.kill_switch_active,
                "Kill switch is inactive"
                if not worker.kill_switch_active
                else "Kill switch is active",
            )

            if credentials_configured and not durable_ledger_error:
                adapter = adapter_factory(
                    api_key="".join(str(os.getenv("BINANCE_MAINNET_API_KEY", "")).split()),
                    api_secret="".join(str(os.getenv("BINANCE_MAINNET_API_SECRET", "")).split()),
                    env=BinanceEnvironment.MAINNET,
                    ledger=durable_ledger,
                    preflight_only=True,
                    portfolio_margin=is_portfolio_margin_enabled(),
                )
                connected = await adapter.connect()
                try:
                    market_fresh = await adapter.refresh_market_data(["ETHUSDC"])
                except Exception:
                    market_fresh = False

                capabilities = adapter.capabilities
                add_check(
                    "CHK-PREFLIGHT-CONNECTION",
                    "Mainnet Read-only Connection",
                    connected and adapter.connection_state == ConnectionState.READY,
                    "Fixed Mainnet adapter reached READY for observation"
                    if connected and adapter.connection_state == ConnectionState.READY
                    else "Mainnet read-only connection did not reach READY",
                )
                add_check(
                    "CHK-PREFLIGHT-AUTH",
                    "Signed Account Authentication",
                    bool(
                        capabilities.account_request_succeeded
                        and adapter.authenticated
                    ),
                    "Signed Mainnet account request succeeded"
                    if capabilities.account_request_succeeded and adapter.authenticated
                    else "Signed Mainnet account authentication is unverified",
                )
                add_check(
                    "CHK-PREFLIGHT-CAN-TRADE",
                    "Account Trade Permission",
                    bool(capabilities.trade_authorized),
                    "Binance account canTrade is true"
                    if capabilities.trade_authorized
                    else "Binance account canTrade is false or unverified",
                )
                add_check(
                    "CHK-PREFLIGHT-POSITION-MODE",
                    "Position Mode",
                    bool(capabilities.position_mode_known),
                    "Binance position mode was read successfully"
                    if capabilities.position_mode_known
                    else "Binance position mode is unknown",
                )
                rules_ready = adapter.is_symbol_ready_for_execution("ETHUSDC")
                add_check(
                    "CHK-PREFLIGHT-RULES",
                    "ETHUSDC Exchange Rules",
                    rules_ready,
                    "Runtime exchangeInfo proves a TRADING USDC perpetual with complete filters"
                    if rules_ready
                    else "ETHUSDC exchange-derived contract or filters are incomplete",
                )
                reconciliation_ready = (
                    adapter.reconciliation.last_status == "IN_SYNC"
                )
                diff_summary = ", ".join(
                    f"{d.code}:{d.symbol}:{d.local_value}->{d.exchange_value}"
                    for d in getattr(adapter.reconciliation, "last_diffs", [])
                )
                add_check(
                    "CHK-PREFLIGHT-RECONCILIATION",
                    "Account Reconciliation",
                    reconciliation_ready,
                    "Mainnet positions, open orders, fills, and account snapshot are in sync"
                    if reconciliation_ready
                    else f"Mainnet exchange state is not reconciled with the disposable preflight ledger: {diff_summary or 'NO_DIFFS_REPORTED'}",
                )
                add_check(
                    "CHK-PREFLIGHT-PRIVATE-STREAM",
                    "Private Stream",
                    bool(adapter.private_stream_healthy),
                    "Private stream transport heartbeat was verified"
                    if adapter.private_stream_healthy
                    else "Private stream is unavailable or stale",
                )
                snapshot = adapter.account_snapshot
                account_ready = context.is_snapshot_risk_ready(
                    snapshot,
                    adapter,
                    require_execution_lease=False,
                )
                add_check(
                    "CHK-PREFLIGHT-ACCOUNT-RISK",
                    "USDC Account Risk Snapshot",
                    account_ready,
                    "USDC collateral, balance, leverage, exposure, liquidation, and fee/funding-inclusive daily PnL are within locked limits"
                    if account_ready
                    else "USDC collateral, mode, leverage, exposure, liquidation, or complete daily PnL evidence is unsafe or unavailable",
                )
                market_timestamp = adapter.last_market_event_at.get("ETHUSDC")
                if market_timestamp is not None and market_timestamp.tzinfo is not None:
                    market_age = (utc_now() - market_timestamp).total_seconds()
                    market_fresh = bool(
                        market_fresh
                        and adapter.has_authoritative_market_sample("ETHUSDC")
                        and 0 <= market_age <= adapter._market_data_max_age()
                    )
                else:
                    market_fresh = False
                add_check(
                    "CHK-PREFLIGHT-MARKET",
                    "ETHUSDC Market Freshness",
                    market_fresh,
                    "Fresh Mainnet ETHUSDC book data was observed"
                    if market_fresh
                    else "Fresh Mainnet ETHUSDC market data is unavailable",
                )
            else:
                for check_id, name, message in (
                    (
                        "CHK-PREFLIGHT-CONNECTION",
                        "Mainnet Read-only Connection",
                        "Skipped because Mainnet credentials are not injected",
                    ),
                    (
                        "CHK-PREFLIGHT-AUTH",
                        "Signed Account Authentication",
                        "Skipped because Mainnet credentials are not injected",
                    ),
                    (
                        "CHK-PREFLIGHT-CAN-TRADE",
                        "Account Trade Permission",
                        "Skipped because Mainnet credentials are not injected",
                    ),
                    (
                        "CHK-PREFLIGHT-POSITION-MODE",
                        "Position Mode",
                        "Skipped because Mainnet credentials are not injected",
                    ),
                    (
                        "CHK-PREFLIGHT-RULES",
                        "ETHUSDC Exchange Rules",
                        "Skipped because Mainnet credentials are not injected",
                    ),
                    (
                        "CHK-PREFLIGHT-RECONCILIATION",
                        "Account Reconciliation",
                        "Skipped because Mainnet credentials are not injected",
                    ),
                    (
                        "CHK-PREFLIGHT-PRIVATE-STREAM",
                        "Private Stream",
                        "Skipped because Mainnet credentials are not injected",
                    ),
                    (
                        "CHK-PREFLIGHT-ACCOUNT-RISK",
                        "USDC Account Risk Snapshot",
                        "Skipped because Mainnet credentials are not injected",
                    ),
                    (
                        "CHK-PREFLIGHT-MARKET",
                        "ETHUSDC Market Freshness",
                        "Skipped because Mainnet credentials are not injected",
                    ),
                ):
                    add_check(check_id, name, False, message)
        except Exception as exc:
            logger.error(
                "Mainnet read-only preflight failed: %s",
                type(exc).__name__,
            )
            add_check(
                "CHK-PREFLIGHT-ERROR",
                "Preflight Lifecycle",
                False,
                "Read-only preflight could not complete; see sanitized server logs",
            )
        finally:
            if adapter is not None:
                order_submission_attempts = int(
                    getattr(adapter, "order_submission_attempts", 0) or 0
                )
                order_endpoint_attempts = int(
                    getattr(
                        getattr(adapter, "rest_client", None),
                        "order_endpoint_attempts",
                        0,
                    )
                    or 0
                )
                try:
                    await adapter.close()
                except Exception as exc:
                    logger.error(
                        "Mainnet read-only preflight cleanup failed: %s",
                        type(exc).__name__,
                    )

        after_signature = (
            worker.execution_mode,
            worker.engine_state,
            worker.connection_state,
            worker.market_data_healthy,
            worker.private_stream_healthy,
            worker.authenticated,
            worker.reconciliation_status,
            worker.kill_switch_active,
            worker.pause_new_risk,
            worker.recovery_only,
            tuple(worker.symbols),
            id(worker.execution_adapter),
            int(getattr(worker.execution_adapter, "order_submission_attempts", 0) or 0),
            repr(worker.active_configuration),
        )
        state_unchanged = before_signature == after_signature
        add_check(
            "CHK-PREFLIGHT-WORKER-STATE",
            "Worker Lifecycle Unchanged",
            state_unchanged,
            "Worker remained in its prior lifecycle state"
            if state_unchanged
            else "Worker lifecycle changed during read-only preflight",
        )
        add_check(
            "CHK-PREFLIGHT-NO-ORDER-ENDPOINT",
            "No Order Endpoint",
            order_endpoint_attempts == 0,
            "No Binance order endpoint was called"
            if order_endpoint_attempts == 0
            else "A Binance order endpoint was called during read-only preflight",
        )
        add_check(
            "CHK-PREFLIGHT-NO-ORDER-SUBMISSION",
            "No Order Submission",
            order_submission_attempts == 0,
            "No order submission was attempted"
            if order_submission_attempts == 0
            else "An order submission was attempted during read-only preflight",
        )
        operational_checks = [
            check for check in checks if check["required"]
        ]
        preflight_passed = bool(
            operational_checks
            and all(check["status"] == "PASS" for check in operational_checks)
        )
        approval = context.env_flag("MAINNET_LIVE_APPROVED", False)
        logger.info(
            "monitor_event=mainnet_read_only_preflight preflight_passed=%s order_submission_attempts=%d order_endpoint_attempts=%d",
            preflight_passed,
            order_submission_attempts,
            order_endpoint_attempts,
        )
        return {
            "executionMode": "LIVE",
            "preflightOnly": True,
            "preflightPassed": preflight_passed,
            # A read-only observation can never arm this Worker, even if a
            # deployment happens to carry a stale approval flag.
            "canArm": False,
            "mainnetLiveApproved": approval,
            "engineState": worker.engine_state.value,
            "orderSubmissionAttempts": order_submission_attempts,
            "order_submission_attempts": order_submission_attempts,
            "orderEndpointAttempts": order_endpoint_attempts,
            "checks": checks,
            "observedAt": observed_at.isoformat(),
        }
