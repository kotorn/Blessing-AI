const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

code = code.replace(
  /import \{ auditRepository \} from '\.\/src\/backend\/audit\.js';/,
  `import { auditRepository } from './src/backend/audit.js';
import { executionManager } from './src/backend/execution/manager.js';`
);

const armRegex = /tradingSystemState\.executionMode = requestedConfig\.executionMode as any;\n  tradingSystemState\.engineState = 'ARMED';/;
code = code.replace(armRegex, `tradingSystemState.executionMode = requestedConfig.executionMode as any;
  tradingSystemState.engineState = 'ARMED';

  // Initialize testnet adapter if we are going into testnet mode
  if (tradingSystemState.executionMode === 'TESTNET') {
    try {
      const active = getActiveBinanceCredentials();
      if (active && active.isTestnet) {
        executionManager.setTestnetCredentials(active.apiKey, active.apiSecret);
      }
    } catch (err) {
      console.warn("No active testnet credentials found during arm.");
    }
  }`);

fs.writeFileSync('server.ts', code);
