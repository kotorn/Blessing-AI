const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

const oldPnrRegex = /app\.post\('\/api\/system\/pause-new-risk', \(req, res\) => \{[\s\S]*?res\.json\(tradingSystemState\);\n\}\);/;
const newPnrCode = `app.post('/api/system/pause-new-risk', (req, res) => {
  const { active: pnrActive } = req.body;
  const targetState = pnrActive ? 'PAUSED_NEW_RISK' : 'ARMED';
  if (!validateStateTransition(tradingSystemState.engineState, targetState)) {
    return res.status(409).json({ error: 'INVALID_STATE_TRANSITION', currentState: tradingSystemState.engineState, requestedState: targetState });
  }
  tradingSystemState.pauseNewRisk = pnrActive;
  tradingSystemState.engineState = targetState as any;
  tradingSystemState.updatedAt = new Date().toISOString();
  res.json(tradingSystemState);
});`;
code = code.replace(oldPnrRegex, newPnrCode);

const oldRecRegex = /app\.post\('\/api\/system\/recovery-only', \(req, res\) => \{[\s\S]*?res\.json\(tradingSystemState\);\n\}\);/;
const newRecCode = `app.post('/api/system/recovery-only', (req, res) => {
  const { active: recActive } = req.body;
  const targetState = recActive ? 'RECOVERY_ONLY' : 'ARMED';
  if (!validateStateTransition(tradingSystemState.engineState, targetState)) {
    return res.status(409).json({ error: 'INVALID_STATE_TRANSITION', currentState: tradingSystemState.engineState, requestedState: targetState });
  }
  tradingSystemState.recoveryOnly = recActive;
  tradingSystemState.engineState = targetState as any;
  tradingSystemState.updatedAt = new Date().toISOString();
  res.json(tradingSystemState);
});`;
code = code.replace(oldRecRegex, newRecCode);

const oldKsRegex = /app\.post\('\/api\/quant\/risk\/kill-switch', \(req, res\) => \{[\s\S]*?res\.json\(\{ kill_switch_active: ksActive, risk_state: quantEngineState\.account\.risk_state \}\);\n\}\);/;
const newKsCode = `app.post('/api/quant/risk/kill-switch', (req: Request, res: Response) => {
  const { active: ksActive } = req.body;
  const targetState = ksActive ? 'EMERGENCY' : 'DISARMED';
  if (!validateStateTransition(tradingSystemState.engineState, targetState)) {
    return res.status(409).json({ error: 'INVALID_STATE_TRANSITION', currentState: tradingSystemState.engineState, requestedState: targetState });
  }
  tradingSystemState.killSwitchActive = ksActive;
  tradingSystemState.engineState = targetState as any;
  tradingSystemState.updatedAt = new Date().toISOString();
  
  quantEngineState.account.kill_switch_active = ksActive;
  quantEngineState.account.risk_state = ksActive ? 'EMERGENCY' : 'NORMAL';
  res.json({ kill_switch_active: ksActive, risk_state: quantEngineState.account.risk_state });
});`;
code = code.replace(oldKsRegex, newKsCode);

const oldKs2Regex = /app\.post\('\/api\/quant\/killswitch', \(req: Request, res: Response\) => \{[\s\S]*?res\.json\(\{ kill_switch_active: active, risk_state: quantEngineState\.account\.risk_state \}\);\n\}\);/;
const newKs2Code = `app.post('/api/quant/killswitch', (req: Request, res: Response) => {
  // Alias for backward compatibility
  const { active: ksActive } = req.body;
  const targetState = ksActive ? 'EMERGENCY' : 'DISARMED';
  if (!validateStateTransition(tradingSystemState.engineState, targetState)) {
    return res.status(409).json({ error: 'INVALID_STATE_TRANSITION', currentState: tradingSystemState.engineState, requestedState: targetState });
  }
  tradingSystemState.killSwitchActive = ksActive;
  tradingSystemState.engineState = targetState as any;
  tradingSystemState.updatedAt = new Date().toISOString();
  
  quantEngineState.account.kill_switch_active = ksActive;
  quantEngineState.account.risk_state = ksActive ? 'EMERGENCY' : 'NORMAL';
  res.json({ kill_switch_active: ksActive, risk_state: quantEngineState.account.risk_state });
});`;
code = code.replace(oldKs2Regex, newKs2Code);

fs.writeFileSync('server.ts', code);
