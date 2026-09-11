const fs = require('fs');
let code = fs.readFileSync('server.ts', 'utf-8');

code = code.replace(/      strategy: 'Structural Grid',/g, "      source: 'SIMULATED',\n      strategy: 'Structural Grid',");
code = code.replace(/      strategy: 'Trend Breakout',/g, "      source: 'SIMULATED',\n      strategy: 'Trend Breakout',");

fs.writeFileSync('server.ts', code);
