const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

code = code.replace(
  /require\('\.\/src\/backend\/system\.js'\)\.EXECUTION_CAPABILITIES/g,
  `EXECUTION_CAPABILITIES`
);

code = code.replace(
  /import \{ evaluatePreflight, validateStateTransition, RISK_PROFILES, canExecuteAction \} from '\.\/src\/backend\/system\.js';/,
  `import { evaluatePreflight, validateStateTransition, RISK_PROFILES, canExecuteAction, EXECUTION_CAPABILITIES } from './src/backend/system.js';`
);

fs.writeFileSync('server.ts', code);
