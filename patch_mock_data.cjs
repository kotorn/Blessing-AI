const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

// Update Recovery State in quantEngineState
const insertPoint = `  meta_allocations: quantEngineState.meta_allocations,`;
const insertData = `
    exposure_recovery: {
      status: 'ACTIVE_GRID_BRAKE',
      current_drawdown_pct: 3.12,
      trigger_threshold_pct: 2.50,
      toxic_inventory_symbol: 'BTCUSDT',
      action_taken: 'Blocked +0.10 BTC Grid intent. Enforcing exposure reduction.',
      recommended_hedge_ratio: 0.15
    },`;

code = code.replace(insertPoint, insertPoint + insertData);

fs.writeFileSync('server.ts', code);
