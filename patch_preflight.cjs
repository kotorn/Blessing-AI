const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

const oldPreflightRegex = /app\.get\('\/api\/system\/preflight', \(req, res\) => \{[\s\S]*?res\.json\(\{[\s\S]*?executionMode,[\s\S]*?canArm,[\s\S]*?checks[\s\S]*?\}\);\n\}\);/;
const oldArmRegex = /app\.post\('\/api\/system\/arm', \(req, res\) => \{[\s\S]*?res\.json\(tradingSystemState\);\n\}\);/;
const oldDisarmRegex = /app\.post\('\/api\/system\/disarm', \(req, res\) => \{[\s\S]*?res\.json\(tradingSystemState\);\n\}\);/;

const newPreflightCode = `app.get('/api/system/preflight', (req, res) => {
  const requestedConfig = {
    executionMode: req.query.executionMode || 'PAPER'
  };
  const preflight = evaluatePreflight(tradingSystemState, requestedConfig);
  res.json(preflight);
});`;

const newArmCode = `app.post('/api/system/arm', (req, res) => {
  const { executionMode, riskProfile, instruments, strategies } = req.body;
  
  const requestedConfig = {
    executionMode: executionMode || 'PAPER',
    instruments: instruments || [],
    strategies: strategies || { grid: false, trend: false, shock: false, carry: false },
    riskProfile: riskProfile || 'BALANCED'
  };

  const preflight = evaluatePreflight(tradingSystemState, requestedConfig);

  if (!preflight.canArm) {
    return res.status(409).json({
      error: 'PRECHECK_FAILED',
      preflight
    });
  }

  if (!validateStateTransition(tradingSystemState.engineState, 'ARMED')) {
    return res.status(409).json({ error: 'INVALID_STATE_TRANSITION', currentState: tradingSystemState.engineState, requestedState: 'ARMED' });
  }

  tradingSystemState.executionMode = requestedConfig.executionMode as any;
  tradingSystemState.engineState = 'ARMED';
  
  // Pick risk profile
  const profileKey = requestedConfig.riskProfile as keyof typeof RISK_PROFILES;
  riskConfiguration = RISK_PROFILES[profileKey] || RISK_PROFILES.BALANCED;

  tradingSystemState.activeConfiguration = {
    executionMode: requestedConfig.executionMode as any,
    instruments: requestedConfig.instruments,
    strategies: requestedConfig.strategies,
    riskProfile: requestedConfig.riskProfile as any,
    riskConfiguration,
    configVersion: tradingSystemState.configVersion,
    armedAt: new Date().toISOString()
  };

  tradingSystemState.updatedAt = new Date().toISOString();
  
  (quantEngineState.account as any).source = tradingSystemState.executionMode === 'PAPER' ? 'SIMULATED' : (tradingSystemState.executionMode === 'TESTNET' ? 'BINANCE_TESTNET' : 'BINANCE_MAINNET');

  res.json(tradingSystemState);
});`;

const newDisarmCode = `app.post('/api/system/disarm', (req, res) => {
  if (!validateStateTransition(tradingSystemState.engineState, 'DISARMED')) {
    return res.status(409).json({ error: 'INVALID_STATE_TRANSITION', currentState: tradingSystemState.engineState, requestedState: 'DISARMED' });
  }
  tradingSystemState.engineState = 'DISARMED';
  tradingSystemState.killSwitchActive = false;
  tradingSystemState.updatedAt = new Date().toISOString();
  res.json(tradingSystemState);
});`;

code = code.replace(oldPreflightRegex, newPreflightCode);
code = code.replace(oldArmRegex, newArmCode);
code = code.replace(oldDisarmRegex, newDisarmCode);

fs.writeFileSync('server.ts', code);
