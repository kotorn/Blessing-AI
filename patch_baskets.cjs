const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

const guards = {
  expand: `  if (!canExecuteAction(tradingSystemState.engineState, 'INCREASE_RISK')) {
    return res.status(403).json({ error: 'ACTION_BLOCKED_BY_SYSTEM_STATE', engineState: tradingSystemState.engineState, action: 'INCREASE_RISK' });
  }`,
  recovery: `  if (!canExecuteAction(tradingSystemState.engineState, 'RECOVERY')) {
    return res.status(403).json({ error: 'ACTION_BLOCKED_BY_SYSTEM_STATE', engineState: tradingSystemState.engineState, action: 'RECOVERY' });
  }`,
  close: `  if (!canExecuteAction(tradingSystemState.engineState, 'CLOSE')) {
    return res.status(403).json({ error: 'ACTION_BLOCKED_BY_SYSTEM_STATE', engineState: tradingSystemState.engineState, action: 'CLOSE' });
  }`,
  action: `  const { action } = req.body;
  let riskClass = 'NEW_RISK';
  if (action === 'CLOSE_ALL') riskClass = 'CLOSE';
  if (action === 'ENABLE_AUTO_RECOVERY') riskClass = 'RECOVERY';
  if (!canExecuteAction(tradingSystemState.engineState, riskClass as any)) {
    return res.status(403).json({ error: 'ACTION_BLOCKED_BY_SYSTEM_STATE', engineState: tradingSystemState.engineState, action: riskClass });
  }`
};

code = code.replace(
  /app\.post\('\/api\/quant\/basket\/expand', \(req: Request, res: Response\) => \{/,
  `app.post('/api/quant/basket/expand', (req: Request, res: Response) => {\n${guards.expand}`
);

code = code.replace(
  /app\.post\('\/api\/quant\/basket\/recovery', \(req: Request, res: Response\) => \{/,
  `app.post('/api/quant/basket/recovery', (req: Request, res: Response) => {\n${guards.recovery}`
);

code = code.replace(
  /app\.post\('\/api\/quant\/basket\/close', \(req: Request, res: Response\) => \{/,
  `app.post('/api/quant/basket/close', (req: Request, res: Response) => {\n${guards.close}`
);

code = code.replace(
  /app\.post\('\/api\/quant\/basket\/action', \(req: Request, res: Response\) => \{\n  const \{ basket_id, action \} = req\.body;/,
  `app.post('/api/quant/basket/action', (req: Request, res: Response) => {\n  const { basket_id, action } = req.body;\n${guards.action.replace('const { action } = req.body;', '')}`
);

fs.writeFileSync('server.ts', code);
