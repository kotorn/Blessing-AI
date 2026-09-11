const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

code = code.replace(
  /    return res\.json\(\{\n      success: true,\n      message: \`Successfully synchronized funds from Binance \(\$\{active\.name\}\)\`,\n      account: quantEngineState\.account,\n      liveResult,\n    \}\);\n  \} else \{/,
  `    tradingSystemState.reconciliationStatus = 'IN_SYNC';
    
    return res.json({
      success: true,
      message: \`Successfully synchronized funds from Binance (\${active.name})\`,
      account: quantEngineState.account,
      liveResult,
    });
  } else {
    tradingSystemState.accountSynchronized = false;
    tradingSystemState.reconciliationStatus = 'UNKNOWN';
    tradingSystemState.updatedAt = new Date().toISOString();`
);

fs.writeFileSync('server.ts', code);
