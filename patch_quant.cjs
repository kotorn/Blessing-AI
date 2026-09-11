const fs = require('fs');
let code = fs.readFileSync('src/api/quant.ts', 'utf-8');

const interfaceTarget = `  alerts?: SystemAlert[];`;
const interfaceReplacement = `  alerts?: SystemAlert[];\n  strategy_intents?: any[];\n  meta_allocations?: any[];`;

code = code.replace(interfaceTarget, interfaceReplacement);
fs.writeFileSync('src/api/quant.ts', code);
