const fs = require('fs');
let code = fs.readFileSync('src/backend/system.ts', 'utf-8');

code = code.replace(
  /testnet: false, \/\/ NOT READY YET/,
  `testnet: true,`
);

fs.writeFileSync('src/backend/system.ts', code);
