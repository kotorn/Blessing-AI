const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

// Add ZECUSDT to the instruments mock to reflect the scanner
const insertPoint = `instruments: {`;
const insertData = `    ZECUSDT: {
      symbol: 'ZECUSDT',
      spot_price: 42.15,
      perp_price: 42.20,
      basis: 0.05,
      basis_pct: 0.11,
      basis_zscore: 1.2,
      funding_rate: 0.0002,
      funding_annualized_pct: 21.9,
      atr_1h: 1.2,
      realized_vol_24h_pct: 88.5,
      open_interest_usd: 120000000,
      open_interest_delta_24h_pct: 12.5,
      regime: 'R4_BREAKOUT',
      regime_probabilities: {
        R0_STRONG_MEAN_REVERSION: 0.05,
        R1_RANGE: 0.10,
        R2_WEAK_TREND: 0.15,
        R3_STRONG_TREND: 0.20,
        R4_BREAKOUT: 0.45,
        R5_VOLATILITY_SHOCK: 0.05,
        R6_CRISIS: 0.0,
      },
      grid_safety_score: 18.5,
      grid_status: 'PAUSED_NEW_RISK',
      expected_recovery_time_hrs: 48,
      expected_mae_pct: 12.0,
      prob_basket_profit: 0.22,
    },`;

if (!code.includes('ZECUSDT')) {
  code = code.replace(insertPoint, insertPoint + '\n' + insertData);
}

fs.writeFileSync('server.ts', code);
