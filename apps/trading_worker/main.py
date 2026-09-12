import asyncio
import logging
import signal
import sys
import uvicorn
from typing import List, Optional
from decimal import Decimal

from domain.models import MarketEvent, MarketType, RiskSnapshot, utc_now
from domain.enums import RiskState
from apps.trading_worker.engines.price_action import PriceActionEngine
from apps.trading_worker.engines.market_state import MarketStateClassifier
from apps.trading_worker.engines.grid_strategy import GridStrategyEngine
from apps.trading_worker.engines.trend_strategy import TrendStrategyEngine
from apps.trading_worker.engines.shock_strategy import ShockStrategyEngine
from apps.trading_worker.engines.exposure_recovery import ExposureRecoveryEngine
from apps.trading_worker.engines.funding_carry import FundingCarryEngine
from apps.trading_worker.engines.market_scanner import MarketScannerEngine
from apps.trading_worker.engines.meta_allocator import MetaAllocator
from apps.trading_worker.engines.risk_governor import RiskGovernor

from venues.binance.public_ws import BinancePublicWebSocket
import apps.trading_worker.api as api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("blessing.worker")

class TradingWorkerApp:
    def __init__(self, symbols: List[str] = ["BTCUSDT", "ETHUSDT"]):
        self.symbols = symbols
        self.is_running = True
        
        # Engines
        self.pa_engine = PriceActionEngine()
        self.market_state_engine = MarketStateClassifier()
        self.grid_engine = GridStrategyEngine()
        self.trend_engine = TrendStrategyEngine()
        self.shock_engine = ShockStrategyEngine()
        self.carry_engine = FundingCarryEngine()
        self.recovery_engine = ExposureRecoveryEngine()
        self.meta_allocator = MetaAllocator()
        self.risk_governor = RiskGovernor()
        self.scanner = MarketScannerEngine(top_n=8)
        self.scan_task = None
        
        self.ws_client = None
        
        # Runtime State
        self.execution_mode = api.WorkerExecutionMode.PAPER
        self.engine_state = api.WorkerEngineState.DISARMED
        self.connection_state = "DISCONNECTED"
        self.market_data_healthy = False
        self.private_stream_healthy = False
        self.authenticated = False
        self.reconciliation_status = "UNKNOWN"
        self.kill_switch_active = False
        self.pause_new_risk = False
        self.recovery_only = False
        self.active_configuration = None
        self.updated_at = utc_now()

    def get_state(self) -> api.WorkerRuntimeState:
        return api.WorkerRuntimeState(
            execution_mode=self.execution_mode,
            engine_state=self.engine_state,
            connection_state=self.connection_state,
            market_data_healthy=self.market_data_healthy,
            private_stream_healthy=self.private_stream_healthy,
            authenticated=self.authenticated,
            reconciliation_status=self.reconciliation_status,
            kill_switch_active=self.kill_switch_active,
            pause_new_risk=self.pause_new_risk,
            recovery_only=self.recovery_only,
            active_configuration=self.active_configuration,
            updated_at=utc_now()
        )
        
    def get_capabilities(self) -> dict:
        return {
            "paper": True,
            "testnetConfigured": True, # TODO actual env check
            "testnetAuthenticated": self.authenticated,
            "testnetExecutionReady": self.engine_state == api.WorkerEngineState.ARMED,
            "liveConfigured": False,
            "liveExecutionReady": False,
            "spotSupported": False,
            "usdmFuturesSupported": True,
            "hedgeModeSupported": False
        }

def get_preflight(self, execution_mode: str) -> dict:
        can_arm = True
        checks = []
        if execution_mode == "TESTNET":
            if not self.authenticated:
                can_arm = False
                checks.append({"id": "CHK-AUTH", "name": "Authentication", "required": True, "status": "FAIL", "message": "Not authenticated"})
            if self.reconciliation_status != "IN_SYNC":
                can_arm = False
                checks.append({"id": "CHK-SYNC", "name": "Reconciliation", "required": True, "status": "FAIL", "message": "Not in sync"})
            if not self.private_stream_healthy:
                can_arm = False
                checks.append({"id": "CHK-STREAM", "name": "Private Stream", "required": True, "status": "FAIL", "message": "Stream offline"})
        return {
            "executionMode": execution_mode,
            "canArm": can_arm,
            "checks": checks
        }

    async def set_pause_new_risk(self, active: bool):
        self.pause_new_risk = active
        if active:
            self.engine_state = api.WorkerEngineState.PAUSED_NEW_RISK
        else:
            self.engine_state = api.WorkerEngineState.ARMED if self.active_configuration else api.WorkerEngineState.DISARMED

    async def set_recovery_only(self, active: bool):
        self.recovery_only = active
        if active:
            self.engine_state = api.WorkerEngineState.RECOVERY_ONLY
        else:
            self.engine_state = api.WorkerEngineState.ARMED if self.active_configuration else api.WorkerEngineState.DISARMED

    async def set_kill_switch(self, active: bool):
        self.kill_switch_active = active
        if active:
            self.engine_state = api.WorkerEngineState.EMERGENCY
        else:
            self.engine_state = api.WorkerEngineState.DISARMED
            
    async def trigger_reconciliation(self) -> str:
        # Mocking reconciliation
        self.reconciliation_status = "IN_SYNC"
        self.connection_state = "READY"
        self.private_stream_healthy = True
        self.authenticated = True
        return self.reconciliation_status

    async def arm(self, config: dict):
        self.active_configuration = config
        mode = config.get("executionMode", "PAPER")
        if mode == "TESTNET":
            self.execution_mode = api.WorkerExecutionMode.TESTNET
        else:
            self.execution_mode = api.WorkerExecutionMode.PAPER
            
        self.engine_state = api.WorkerEngineState.ARMED
        logger.info(f"Worker ARMED in {self.execution_mode} mode")
        return True, ""

    async def disarm(self):
        self.engine_state = api.WorkerEngineState.DISARMED
        logger.info("Worker DISARMED")

    async def handle_market_event(self, event: MarketEvent):
        if not self.market_data_healthy:
            self.market_data_healthy = True
            
        pa_state = self.pa_engine.process_event(event)
        if not pa_state:
            return
            
        market_state = self.market_state_engine.classify(pa_state)
        
        grid_intent = self.grid_engine.evaluate(pa_state, market_state)
        trend_intent = self.trend_engine.evaluate(pa_state, market_state)
        shock_intent = self.shock_engine.evaluate(pa_state, market_state)
        carry_intent = self.carry_engine.evaluate(event, market_state)
        
        intents = [i for i in [grid_intent, trend_intent, shock_intent, carry_intent] if i]
        
        mock_risk = RiskSnapshot(
            portfolio_equity=Decimal("100000.0"),
            unrealized_pnl=Decimal("0.0"),
            realized_pnl_24h=Decimal("0.0"),
            margin_utilization_pct=Decimal("5.0"),
            effective_leverage=Decimal("0.5"),
            current_drawdown_pct=Decimal("1.2"),
            liquidation_distance_pct=Decimal("45.0"),
            risk_state=RiskState.NORMAL
        )
        
        raw_target_exposure = self.meta_allocator.allocate(intents, event.symbol)
        
        current_position_qty = Decimal("1.2") # [RESEARCH] Mock
        
        target_exposure = self.recovery_engine.process(
            target=raw_target_exposure,
            risk=mock_risk,
            current_position_qty=current_position_qty
        )
        
        decision = self.risk_governor.evaluate(target_exposure, mock_risk, current_position_qty=Decimal("0.0"))
        
        if decision.action != "NOOP":
            if self.engine_state == api.WorkerEngineState.ARMED:
                logger.info(f"[{self.execution_mode.value}][SIMULATED] EXECUTION DECISION: {decision.symbol} | Action: {decision.action}")

    async def start(self):
        logger.info("Initializing Blessing AI Trading Worker v0.2...")
        self.symbols = await self.scanner.scan_active_symbols()
        
        logger.info("Connecting to Binance WS for: %s", self.symbols)
        self.ws_client = BinancePublicWebSocket(
            symbols=self.symbols,
            event_callback=self.handle_market_event,
        )
        self.scan_task = asyncio.create_task(self._periodic_scanner())
        
        # We don't sleep forever here, we just start tasks.
        
    async def _periodic_scanner(self):
        while self.is_running:
            await asyncio.sleep(self.scanner.refresh_interval_sec)
            try:
                new_symbols = await self.scanner.scan_active_symbols()
                if set(new_symbols) != set(self.symbols):
                    self.symbols = new_symbols
            except Exception as e:
                logger.error("Periodic scanner failed: %s", e)

    def stop(self):
        logger.info("Gracefully stopping Trading Worker...")
        self.is_running = False

def serve_api(app_instance):
    api.WORKER_ENGINE = app_instance
    config = uvicorn.Config(api.app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)
    return server.serve()

async def main():
    app_instance = TradingWorkerApp()
    await app_instance.start()
    
    # Run API server
    await serve_api(app_instance)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
