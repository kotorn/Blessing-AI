const fs = require('fs');
let code = fs.readFileSync('apps/trading_worker/engines/grid_strategy.py', 'utf-8');

code = code.replace('from apps.trading_worker.engines.ml_scorer import GridSafetyScorerML, Dict', 'from apps.trading_worker.engines.ml_scorer import GridSafetyScorerML\nfrom typing import Dict');

fs.writeFileSync('apps/trading_worker/engines/grid_strategy.py', code);
