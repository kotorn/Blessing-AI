const fs = require('fs');
let code = fs.readFileSync('apps/trading_worker/main.py', 'utf-8');

// 1. Add new imports
code = code.replace(
  'from apps.trading_worker.engines.shock_strategy import ShockStrategyEngine',
  'from apps.trading_worker.engines.shock_strategy import ShockStrategyEngine\nfrom apps.trading_worker.engines.exposure_recovery import ExposureRecoveryEngine\nfrom apps.trading_worker.engines.funding_carry import FundingCarryEngine'
);

// 2. Initialize new engines
code = code.replace(
  'self.shock_engine = ShockStrategyEngine()',
  'self.shock_engine = ShockStrategyEngine()\n        self.carry_engine = FundingCarryEngine()\n        self.recovery_engine = ExposureRecoveryEngine()'
);

// 3. Add to intent generation
code = code.replace(
  'shock_intent = self.shock_engine.evaluate(pa_state, market_state)',
  'shock_intent = self.shock_engine.evaluate(pa_state, market_state)\n        carry_intent = self.carry_engine.evaluate(event, market_state)'
);

code = code.replace(
  'intents = [i for i in [grid_intent, trend_intent, shock_intent] if i]',
  'intents = [i for i in [grid_intent, trend_intent, shock_intent, carry_intent] if i]'
);

// 4. Inject Exposure Recovery in the execution pipeline
const oldTarget = 'target_exposure = self.meta_allocator.allocate(intents, event.symbol)';
const newTarget = `raw_target_exposure = self.meta_allocator.allocate(intents, event.symbol)
        
        # 4b. Exposure Recovery & Grid Brake (Phase 3)
        current_position_qty = Decimal("1.2") # Mock existing long position inventory
        
        target_exposure = self.recovery_engine.process(
            target=raw_target_exposure,
            risk=mock_risk,
            current_position_qty=current_position_qty
        )`;

code = code.replace(oldTarget, newTarget);

fs.writeFileSync('apps/trading_worker/main.py', code);
