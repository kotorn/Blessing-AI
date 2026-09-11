const fs = require('fs');
let code = fs.readFileSync('apps/trading_worker/engines/grid_strategy.py', 'utf-8');

code = code.replace(
  'from typing import Optional',
  'from typing import Optional\nfrom apps.trading_worker.engines.ml_scorer import GridSafetyScorerML'
);

code = code.replace(
  'self.strategy_id = strategy_id',
  'self.strategy_id = strategy_id\n        self.ml_scorer = GridSafetyScorerML()'
);

const oldScoreLogic = 'opportunity_score = Decimal("75.0")  # MVP deterministic mock';
const newScoreLogic = 'opportunity_score = self.ml_scorer.predict_safety_score(market_state, pa_state)';

code = code.replace(oldScoreLogic, newScoreLogic);

fs.writeFileSync('apps/trading_worker/engines/grid_strategy.py', code);
