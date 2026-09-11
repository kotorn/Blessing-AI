const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

// Update UI state to show all scanned pairs instead of just BTC/ETH
const insertPoint = `instruments: {`;
const insertData = `    SOLUSDT: {
      symbol: 'SOLUSDT',
      spot_price: 154.20,
      perp_price: 154.35,
      basis: 0.15,
      basis_pct: 0.09,
      basis_zscore: 0.8,
      funding_rate: 0.0001,
      funding_annualized_pct: 10.95,
      atr_1h: 4.5,
      realized_vol_24h_pct: 75.2,
      open_interest_usd: 850000000,
      open_interest_delta_24h_pct: 4.5,
      regime: 'R3_STRONG_TREND',
      regime_probabilities: {
        R0_STRONG_MEAN_REVERSION: 0.05,
        R1_RANGE: 0.15,
        R2_WEAK_TREND: 0.20,
        R3_STRONG_TREND: 0.50,
        R4_BREAKOUT: 0.10,
        R5_VOLATILITY_SHOCK: 0.0,
        R6_CRISIS: 0.0,
      },
      grid_safety_score: 25.0,
      grid_status: 'PAUSED_NEW_RISK',
      expected_recovery_time_hrs: 24,
      expected_mae_pct: 8.5,
      prob_basket_profit: 0.35,
    },
    BNBUSDT: {
      symbol: 'BNBUSDT',
      spot_price: 580.40,
      perp_price: 580.90,
      basis: 0.50,
      basis_pct: 0.08,
      basis_zscore: 0.2,
      funding_rate: 0.00005,
      funding_annualized_pct: 5.47,
      atr_1h: 8.2,
      realized_vol_24h_pct: 42.5,
      open_interest_usd: 620000000,
      open_interest_delta_24h_pct: -1.2,
      regime: 'R1_RANGE',
      regime_probabilities: {
        R0_STRONG_MEAN_REVERSION: 0.10,
        R1_RANGE: 0.60,
        R2_WEAK_TREND: 0.20,
        R3_STRONG_TREND: 0.05,
        R4_BREAKOUT: 0.05,
        R5_VOLATILITY_SHOCK: 0.0,
        R6_CRISIS: 0.0,
      },
      grid_safety_score: 82.5,
      grid_status: 'ALLOWED',
      expected_recovery_time_hrs: 4,
      expected_mae_pct: 1.2,
      prob_basket_profit: 0.88,
    },
`;

code = code.replace(insertPoint, insertPoint + '\n' + insertData);

fs.writeFileSync('server.ts', code);
