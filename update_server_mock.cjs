const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

// Insert new properties into quantEngineState
const insertPoint = `  risk_rules: {`;
const insertData = `  strategy_intents: [
    {
      id: 'INT-GRID-BTC-01',
      engineId: 'structural_grid',
      strategyName: 'Structural Mean-Reversion Grid',
      symbol: 'BTCUSDT',
      direction: 'LONG',
      rawTargetDelta: 0.10,
      opportunityScore: 90.0,
      confidence: 0.90,
      urgency: 'LOW',
      timeHorizon: '12h - 48h',
      hypothesis: 'Price reclaiming prior 24h low; accumulate passive tranches at swing base.',
      regimeFit: 'R0_STRONG_MEAN_REVERSION',
      proposedMaxNotionalUsd: 15000,
    },
    {
      id: 'INT-TREND-BTC-01',
      engineId: 'trend_breakout',
      strategyName: 'Trend / Breakout Following',
      symbol: 'BTCUSDT',
      direction: 'SHORT',
      rawTargetDelta: 0,
      opportunityScore: 0,
      confidence: 0,
      urgency: 'LOW',
      timeHorizon: '4h - 12h',
      hypothesis: 'Awaiting R3/R4 structural confirmation. No current displacement.',
      regimeFit: 'R1_RANGE',
      proposedMaxNotionalUsd: 0,
    },
    {
      id: 'INT-SHOCK-ETH-01',
      engineId: 'shock_momentum',
      strategyName: 'Shock Momentum',
      symbol: 'ETHUSDT',
      direction: 'LONG',
      rawTargetDelta: 0.30,
      opportunityScore: 90.0,
      confidence: 0.60,
      urgency: 'HIGH',
      timeHorizon: '5m - 30m',
      hypothesis: 'Fast liquidity sweep below support reclaimed; targeting mean reversion.',
      regimeFit: 'R5_VOLATILITY_SHOCK',
      proposedMaxNotionalUsd: 6500,
    }
  ],
  meta_allocations: [
    {
      engineId: 'structural_grid',
      strategyName: 'Structural Mean-Reversion Grid',
      baseWeightPct: 35,
      expectedEdgeBps: 45,
      confidence: 0.90,
      regimeFitFactor: 1.15,
      executionQualityFactor: 1.0,
      cryptoBetaDiscount: 0.88,
      finalBudgetFactor: 1.15,
      virtualNotionalCapUsd: 25000,
      status: 'ACTIVE',
    },
    {
      engineId: 'trend_breakout',
      strategyName: 'Trend / Breakout Following',
      baseWeightPct: 25,
      expectedEdgeBps: 0,
      confidence: 0.0,
      regimeFitFactor: 0.0,
      executionQualityFactor: 0.95,
      cryptoBetaDiscount: 0.82,
      finalBudgetFactor: 0.0,
      virtualNotionalCapUsd: 15000,
      status: 'PAUSED',
    },
    {
      engineId: 'shock_momentum',
      strategyName: 'Shock Momentum',
      baseWeightPct: 15,
      expectedEdgeBps: 80,
      confidence: 0.60,
      regimeFitFactor: 1.50,
      executionQualityFactor: 0.90,
      cryptoBetaDiscount: 0.95,
      finalBudgetFactor: 1.45,
      virtualNotionalCapUsd: 8500,
      status: 'ACTIVE',
    }
  ],
`;

code = code.replace(insertPoint, insertData + insertPoint);

const returnInsertPoint = `    risk_rules: allRules,`;
const returnInsertData = `    strategy_intents: quantEngineState.strategy_intents,
    meta_allocations: quantEngineState.meta_allocations,`;

code = code.replace(returnInsertPoint, returnInsertPoint + '\n' + returnInsertData);

// Update instrument R1 to R0 to match grid intent
code = code.replace(`market_regime: 'R1_RANGE'`, `market_regime: 'R0_STRONG_MEAN_REVERSION'`);
code = code.replace(`regime_confidence: 0.74`, `regime_confidence: 0.92`);
code = code.replace(`grid_safety_score: 0.88`, `grid_safety_score: 90.0`);
code = code.replace(`atr_1h: 420.5`, `atr_1h: 420.5,\n      displacement_velocity: 0.8,\n      liquidity_swept: true,\n      range_expansion: 0.35`);


fs.writeFileSync('server.ts', code);
