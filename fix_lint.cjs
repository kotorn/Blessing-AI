const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

code = code.replace(
  /active\.isTestnet \? 'TESTNET' : 'LIVE'/g,
  `active.isTestnet ? 'BINANCE_TESTNET' : 'BINANCE_MAINNET'`
);

code = code.replace(
  /const prevState = tradingSystemState\.engineState;\n  const prevState = tradingSystemState\.engineState;/g,
  `const prevState = tradingSystemState.engineState;`
);

fs.writeFileSync('server.ts', code);
