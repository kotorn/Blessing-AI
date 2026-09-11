const fs = require('fs');
let code = fs.readFileSync('apps/trading_worker/main.py', 'utf-8');

code = code.replace(
  /from apps\.trading_worker\.engines\.shock_strategy import ShockStrategyEngine/,
  `from apps.trading_worker.engines.shock_strategy import ShockStrategyEngine\nfrom apps.trading_worker.engines.meta_allocator import MetaAllocator\nfrom apps.trading_worker.engines.risk_governor import RiskGovernor\nfrom domain.models import RiskSnapshot, utc_now\nfrom domain.enums import RiskState\nfrom decimal import Decimal`
);

code = code.replace(
  /self\.shock_engine = ShockStrategyEngine\(\)/,
  `self.shock_engine = ShockStrategyEngine()\n        self.meta_allocator = MetaAllocator()\n        self.risk_governor = RiskGovernor()`
);

const todoRegex = /# TODO: Route intent to Meta Allocator -> Risk Governor -> Target Exposure -> Adapter/;
const executionCode = `# 4. Meta Allocation
        target_exposure = self.meta_allocator.allocate(intents, event.symbol)
        
        # 5. Risk Governor Validation
        # In a real environment, RiskSnapshot is maintained continuously by an account sync task
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
        
        decision = self.risk_governor.evaluate(target_exposure, mock_risk, current_position_qty=Decimal("0.0"))
        
        if decision.action != "NOOP":
            logger.info("EXECUTION DECISION: %s | Action: %s | Qty: %s", 
                        decision.symbol, decision.action, decision.orders[0].quantity if decision.orders else 0)`;

code = code.replace(todoRegex, executionCode);

fs.writeFileSync('apps/trading_worker/main.py', code);
